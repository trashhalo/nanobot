"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import weakref
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.context import ContextBuilder
from nanobot.agent.memory import MemoryStore
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider
from nanobot.session.manager import Session, SessionManager
from nanobot.utils.helpers import ensure_dir, safe_filename

if TYPE_CHECKING:
    from nanobot.config.schema import ChannelsConfig, ExecToolConfig
    from nanobot.cron.service import CronService


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    _TOOL_RESULT_MAX_CHARS = 500

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 40,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        memory_window: int = 100,
        reasoning_effort: str | None = None,
        web_search_options: dict | None = None,
        search_model: str | None = None,
        model_fallbacks: list[str] | None = None,
        brave_api_key: str | None = None,
        web_proxy: str | None = None,
        exec_config: ExecToolConfig | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
    ):
        from nanobot.config.schema import ExecToolConfig
        self.bus = bus
        self.channels_config = channels_config
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.memory_window = memory_window
        self.reasoning_effort = reasoning_effort
        self.web_search_options = web_search_options
        self.search_model = search_model
        self.model_fallbacks = model_fallbacks or []
        self.brave_api_key = brave_api_key
        self.web_proxy = web_proxy
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace

        self.context = ContextBuilder(workspace)
        self.sessions = session_manager or SessionManager(workspace)
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            reasoning_effort=reasoning_effort,
            web_search_options=web_search_options,
            search_model=search_model,
            model_fallbacks=self.model_fallbacks,
            brave_api_key=brave_api_key,
            web_proxy=web_proxy,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
        )

        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._mcp_connecting = False
        self._consolidating: set[str] = set()  # Session keys with consolidation in progress
        self._consolidation_tasks: set[asyncio.Task] = set()  # Strong refs to in-flight tasks
        self._consolidation_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._current_session_key: str | None = None  # set before each _run_agent_loop call
        self._processing_lock = asyncio.Lock()
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        for cls in (ReadFileTool, WriteFileTool, EditFileTool, ListDirTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dir=allowed_dir))
        self.tools.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            restrict_to_workspace=self.restrict_to_workspace,
            path_append=self.exec_config.path_append,
        ))
        self.tools.register(WebSearchTool(api_key=self.brave_api_key, proxy=self.web_proxy, provider=self.provider, search_model=self.search_model))
        self.tools.register(WebFetchTool(proxy=self.web_proxy))
        self.tools.register(MessageTool(
            send_callback=self.bus.publish_outbound,
            suppress_patterns=self.channels_config.suppress_patterns if self.channels_config else [],
            retrieval_fn=lambda content: self._run_pre_context_hooks(content, "intent", "intent", "intent"),
        ))
        self.tools.register(SpawnTool(manager=self.subagents))
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from nanobot.agent.tools.mcp import connect_mcp_servers
        try:
            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            await connect_mcp_servers(self._mcp_servers, self.tools, self._mcp_stack)
            self._mcp_connected = True
        except Exception as e:
            logger.error("Failed to connect MCP servers (will retry next message): {}", e)
            if self._mcp_stack:
                try:
                    await self._mcp_stack.aclose()
                except Exception:
                    pass
                self._mcp_stack = None
        finally:
            self._mcp_connecting = False

    def _set_tool_context(
        self, channel: str, chat_id: str,
        message_id: str | None = None,
        session_key: str | None = None,
    ) -> None:
        """Update context for all tools that need routing info."""
        for name in ("message", "spawn", "cron"):
            if tool := self.tools.get(name):
                if hasattr(tool, "set_context"):
                    if name == "message":
                        tool.set_context(channel, chat_id, message_id)
                    elif name == "spawn":
                        tool.set_context(channel, chat_id, session_key)
                    else:
                        tool.set_context(channel, chat_id)

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        if not text:
            return None
        return re.sub(r"<think>[\s\S]*?</think>", "", text).strip() or None

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hint, e.g. 'web_search("query")'."""
        def _fmt(tc):
            args = (tc.arguments[0] if isinstance(tc.arguments, list) else tc.arguments) or {}
            val = next(iter(args.values()), None) if isinstance(args, dict) else None
            if not isinstance(val, str):
                return tc.name
            return f'{tc.name}("{val[:40]}…")' if len(val) > 40 else f'{tc.name}("{val}")'
        return ", ".join(_fmt(tc) for tc in tool_calls)

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        extra_tools: dict[str, tuple[dict, Callable[..., Awaitable[str]]]] | None = None,
        on_max_iterations: Callable[[], Awaitable[None]] | None = None,
    ) -> tuple[str | None, list[str], list[dict]]:
        """Run the agent iteration loop. Returns (final_content, tools_used, messages)."""
        messages = initial_messages
        iteration = 0
        final_content = None
        tools_used: list[str] = []

        tool_defs = self.tools.get_definitions()
        if extra_tools:
            tool_defs = tool_defs + [defn for defn, _ in extra_tools.values()]

        while iteration < self.max_iterations:
            iteration += 1

            response = await self.provider.chat(
                messages=messages,
                tools=tool_defs,
                model=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                reasoning_effort=self.reasoning_effort,
                web_search_options=self.web_search_options,
                fallbacks=self.model_fallbacks or None,
            )

            if response.has_tool_calls:
                if on_progress:
                    thought = self._strip_think(response.content)
                    if thought:
                        await on_progress(thought)
                    await on_progress(self._tool_hint(response.tool_calls), tool_hint=True)

                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False)
                        }
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )

                for tool_call in response.tool_calls:
                    tools_used.append(tool_call.name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info("Tool call: {}({})", tool_call.name, args_str[:200])
                    rejection = await self._run_pre_tool_hooks(
                        tool_call.name, tool_call.arguments, response.content or ""
                    )
                    if rejection:
                        logger.info("pre_tool hook rejected tool call {}: {}", tool_call.name, rejection[:100])
                        result = rejection
                    elif extra_tools and tool_call.name in extra_tools:
                        _, handler = extra_tools[tool_call.name]
                        result = await handler(tool_call.arguments)
                    else:
                        result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
            else:
                clean = self._strip_think(response.content)
                # Don't persist error responses to session history — they can
                # poison the context and cause permanent 400 loops (#1303).
                if response.finish_reason == "error":
                    logger.error("LLM returned error: {}", (clean or "")[:200])
                    final_content = clean or "Sorry, I encountered an error calling the AI model."
                    break
                messages = self.context.add_assistant_message(
                    messages, clean, reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                final_content = clean
                break

        if final_content is None and iteration >= self.max_iterations:
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            final_content = (
                f"I reached the maximum number of tool call iterations ({self.max_iterations}) "
                "without completing the task. You can try breaking the task into smaller steps."
            )
            if on_max_iterations:
                await on_max_iterations()

        return final_content, tools_used, messages

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        await self._connect_mcp()
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            if msg.content.strip().lower() == "/stop":
                await self._handle_stop(msg)
            else:
                task = asyncio.create_task(self._dispatch(msg))
                self._active_tasks.setdefault(msg.session_key, []).append(task)
                task.add_done_callback(lambda t, k=msg.session_key: self._active_tasks.get(k, []) and self._active_tasks[k].remove(t) if t in self._active_tasks.get(k, []) else None)

    async def _handle_stop(self, msg: InboundMessage) -> None:
        """Cancel all active tasks and subagents for the session."""
        tasks = self._active_tasks.pop(msg.session_key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        sub_cancelled = await self.subagents.cancel_by_session(msg.session_key)
        total = cancelled + sub_cancelled
        content = f"⏹ Stopped {total} task(s)." if total else "No active task to stop."
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
        ))

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message under the global lock."""
        async with self._processing_lock:
            try:
                stateless = bool((msg.metadata or {}).get("stateless", False))
                response = await self._process_message(msg, stateless=stateless)
                if response is not None:
                    await self.bus.publish_outbound(response)
                elif msg.channel == "cli":
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="", metadata=msg.metadata or {},
                    ))
            except asyncio.CancelledError:
                logger.info("Task cancelled for session {}", msg.session_key)
                raise
            except Exception:
                logger.exception("Error processing message for session {}", msg.session_key)
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="Sorry, I encountered an error.",
                ))

    async def close_mcp(self) -> None:
        """Close MCP connections."""
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            self._mcp_stack = None

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        skill_names: list[str] | None = None,
        stateless: bool = False,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        # System messages: parse origin from chat_id ("channel:chat_id")
        if msg.channel == "system":
            channel, chat_id = (msg.chat_id.split(":", 1) if ":" in msg.chat_id
                                else ("cli", msg.chat_id))
            logger.info("Processing system message from {}", msg.sender_id)
            # Use session_key_override so subagent results land in the same session
            # (e.g. a thread session) that originally spawned the subagent.
            key = msg.session_key_override or f"{channel}:{chat_id}"
            session = self.sessions.get_or_create(key)
            self._set_tool_context(channel, chat_id, msg.metadata.get("message_id"))
            history = session.get_history(max_messages=self.memory_window)
            messages = self.context.build_messages(
                history=history,
                current_message=msg.content, channel=channel, chat_id=chat_id,
            )
            self._current_session_key = key
            final_content, _, all_msgs = await self._run_agent_loop(messages)
            # Only persist if the LLM produced a real assistant response.
            # On LLM error, all_msgs ends with the user message — saving it would
            # leave an orphaned user turn that corrupts future context.
            if all_msgs and all_msgs[-1].get("role") == "assistant":
                self._save_turn(session, all_msgs, 1 + len(history))
                self.sessions.save(session)
            return OutboundMessage(channel=channel, chat_id=chat_id,
                                  content=final_content or "Background task completed.")

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        key = session_key or msg.session_key
        session = self.sessions.get_or_create(key)

        # Slash commands
        cmd = msg.content.strip().lower()
        if cmd == "/new":
            lock = self._consolidation_locks.setdefault(session.key, asyncio.Lock())
            self._consolidating.add(session.key)
            try:
                async with lock:
                    snapshot = session.messages[session.last_consolidated:]
                    if snapshot:
                        temp = Session(key=session.key)
                        temp.messages = list(snapshot)
                        if not await self._consolidate_memory(temp, archive_all=True):
                            return OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content="Memory archival failed, session not cleared. Please try again.",
                            )
            except Exception:
                logger.exception("/new archival failed for {}", session.key)
                return OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="Memory archival failed, session not cleared. Please try again.",
                )
            finally:
                self._consolidating.discard(session.key)

            session.clear()
            self.sessions.save(session)
            self.sessions.invalidate(session.key)
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="New session started.")
        if cmd == "/help":
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="🐈 nanobot commands:\n/new — Start a new conversation\n/stop — Stop the current task\n/help — Show available commands")

        unconsolidated = len(session.messages) - session.last_consolidated
        if (unconsolidated >= self.memory_window and session.key not in self._consolidating):
            self._consolidating.add(session.key)
            lock = self._consolidation_locks.setdefault(session.key, asyncio.Lock())

            async def _consolidate_and_unlock():
                try:
                    async with lock:
                        await self._consolidate_memory(session)
                finally:
                    self._consolidating.discard(session.key)
                    _task = asyncio.current_task()
                    if _task is not None:
                        self._consolidation_tasks.discard(_task)

            _task = asyncio.create_task(_consolidate_and_unlock())
            self._consolidation_tasks.add(_task)

        self._set_tool_context(msg.channel, msg.chat_id, msg.metadata.get("message_id"), session_key=key)
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        history = [] if stateless else session.get_history(max_messages=self.memory_window)
        pre_context = await self._run_pre_context_hooks(msg.content, key, msg.channel, msg.chat_id)
        initial_messages = self.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel, chat_id=msg.chat_id,
            skill_names=skill_names,
            extra_context=pre_context or None,
        )

        async def _bus_progress(content: str, *, tool_hint: bool = False) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content=content, metadata=meta,
            ))

        async def _noop_progress(content: str, *, tool_hint: bool = False) -> None:
            pass

        self._current_session_key = key
        final_content, _, all_msgs = await self._run_agent_loop(
            initial_messages,
            on_progress=on_progress or (_noop_progress if stateless else _bus_progress),
            on_max_iterations=lambda: self._fire_max_iterations_hooks(key, msg.channel, msg.chat_id),
        )

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        if not stateless:
            self._save_turn(session, all_msgs, 1 + len(history))
            self.sessions.save(session)
            new_msgs = all_msgs[1 + len(history):]
            if new_msgs:
                asyncio.create_task(self._fire_post_turn_hooks(new_msgs, key, msg.channel, msg.chat_id))

        if self.channels_config and self.channels_config.suppress_patterns and final_content:
            from nanobot.agent.tools.message import filter_suppressed
            filtered = filter_suppressed(final_content, self.channels_config.suppress_patterns)
            if filtered is None:
                logger.info("Suppressed channel output (all lines matched suppress_patterns): {}...", final_content[:80])
                return None
            final_content = filtered

        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=final_content,
            metadata={**(msg.metadata or {}), "session_key": key},
        )

    def _save_turn(self, session: Session, messages: list[dict], skip: int) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        from datetime import datetime
        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if role == "tool" and isinstance(content, str) and len(content) > self._TOOL_RESULT_MAX_CHARS:
                entry["content"] = content[:self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
            elif role == "user":
                if isinstance(content, str) and content.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                    # Strip the runtime-context prefix, keep only the user text.
                    parts = content.split("\n\n", 1)
                    if len(parts) > 1 and parts[1].strip():
                        entry["content"] = parts[1]
                    else:
                        continue
                if isinstance(content, list):
                    filtered = []
                    for c in content:
                        if c.get("type") == "text" and isinstance(c.get("text"), str) and c["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                            continue  # Strip runtime context from multimodal messages
                        if (c.get("type") == "image_url"
                                and c.get("image_url", {}).get("url", "").startswith("data:image/")):
                            filtered.append({"type": "text", "text": "[image]"})
                        else:
                            filtered.append(c)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
        session.updated_at = datetime.now()

    async def _consolidate_memory(self, session, archive_all: bool = False) -> bool:
        """Delegate to MemoryStore.consolidate(). Returns True on success."""
        return await MemoryStore(self.workspace).consolidate(
            session, self.provider, self.model,
            archive_all=archive_all, memory_window=self.memory_window,
        )

    async def _run_pre_context_hooks(
        self, message: str, session_key: str, channel: str, chat_id: str
    ) -> str:
        """Run pre_context hook scripts from all skills. Returns combined markdown output."""
        scripts = self.context.skills.get_skill_hook_scripts("pre_context")
        if not scripts:
            return ""
        logger.info("pre_context hooks: firing {} script(s): {}", len(scripts), [s.name for s in scripts])
        timeout = (self.channels_config.hook_timeout if self.channels_config else 10)
        payload = json.dumps({"message": message, "session_key": session_key,
                              "channel": channel, "chat_id": chat_id})
        parts = []
        for script in scripts:
            try:
                proc = await asyncio.create_subprocess_exec(
                    str(script),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(payload.encode()), timeout=timeout
                )
                if stderr:
                    logger.info("pre_context hook {} stderr: {}", script.name, stderr.decode().strip())
                if stdout:
                    output = stdout.decode().strip()
                    logger.info("pre_context hook {} returned {} chars of context", script.name, len(output))
                    parts.append(output)
                else:
                    logger.info("pre_context hook {} returned no output", script.name)
            except asyncio.TimeoutError:
                logger.warning("pre_context hook {} timed out after {}s", script.name, timeout)
            except Exception as e:
                logger.warning("pre_context hook {} failed: {}", script.name, e)
        return "\n\n".join(parts)

    async def _run_pre_tool_hooks(
        self, tool_name: str, tool_args: dict, assistant_message: str
    ) -> str | None:
        """Run pre_tool hook scripts from all skills before each tool call.

        Hooks receive JSON on stdin:
          {"tool_name": "...", "tool_args": {...}, "assistant_message": "..."}

        If a hook writes to stdout, the tool call is rejected and the stdout
        is returned as the tool result (the LLM sees it as an error to correct).
        If stdout is empty, the tool call proceeds normally.
        """
        scripts = self.context.skills.get_skill_hook_scripts("pre_tool")
        if not scripts:
            return None
        timeout = (self.channels_config.hook_timeout if self.channels_config else 10)
        payload = json.dumps({
            "tool_name": tool_name,
            "tool_args": tool_args,
            "assistant_message": assistant_message,
            "session_key": self._current_session_key,
        }).encode()
        for script in scripts:
            try:
                proc = await asyncio.create_subprocess_exec(
                    str(script),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(payload), timeout=timeout
                )
                if stderr:
                    logger.info("pre_tool hook {} stderr: {}", script.name, stderr.decode().strip())
                if stdout:
                    return stdout.decode().strip()
            except asyncio.TimeoutError:
                logger.warning("pre_tool hook {} timed out after {}s", script.name, timeout)
            except Exception as e:
                logger.warning("pre_tool hook {} failed: {}", script.name, e)
        return None

    async def _fire_max_iterations_hooks(
        self, session_key: str, channel: str, chat_id: str
    ) -> None:
        """Fire max_iterations hook scripts from all skills when the tool call limit is reached."""
        scripts = self.context.skills.get_skill_hook_scripts("max_iterations")
        if not scripts:
            return
        logger.info("max_iterations hooks: firing {} script(s): {}", len(scripts), [s.name for s in scripts])
        timeout = (self.channels_config.hook_timeout if self.channels_config else 30)
        payload = json.dumps({
            "session_key": session_key,
            "channel": channel,
            "chat_id": chat_id,
            "max_iterations": self.max_iterations,
        }).encode()
        for script in scripts:
            try:
                proc = await asyncio.create_subprocess_exec(
                    str(script),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(
                    proc.communicate(payload), timeout=timeout
                )
                if stderr:
                    logger.info("max_iterations hook {} stderr: {}", script.name, stderr.decode().strip())
            except asyncio.TimeoutError:
                logger.warning("max_iterations hook {} timed out after {}s", script.name, timeout)
            except Exception as e:
                logger.warning("max_iterations hook {} failed: {}", script.name, e)

    async def _fire_post_turn_hooks(
        self, messages: list[dict], session_key: str, channel: str, chat_id: str
    ) -> None:
        """Fire post_turn hook scripts from all skills (non-blocking, best-effort)."""
        scripts = self.context.skills.get_skill_hook_scripts("post_turn")
        if not scripts:
            return
        logger.info("post_turn hooks: firing {} script(s): {}", len(scripts), [s.name for s in scripts])
        timeout = (self.channels_config.hook_timeout if self.channels_config else 10)
        payload = json.dumps({"session_key": session_key, "channel": channel,
                              "chat_id": chat_id, "messages": messages})
        for script in scripts:
            try:
                proc = await asyncio.create_subprocess_exec(
                    str(script),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(
                    proc.communicate(payload.encode()), timeout=timeout
                )
                if stderr:
                    logger.info("post_turn hook {} stderr: {}", script.name, stderr.decode().strip())
                else:
                    logger.info("post_turn hook {} completed with no output", script.name)
            except asyncio.TimeoutError:
                logger.warning("post_turn hook {} timed out after {}s", script.name, timeout)
            except Exception as e:
                logger.warning("post_turn hook {} failed: {}", script.name, e)

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        skill_names: list[str] | None = None,
        stateless: bool = False,
    ) -> str:
        """Process a message directly (for CLI or cron usage)."""
        await self._connect_mcp()
        msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content)
        response = await self._process_message(msg, session_key=session_key, on_progress=on_progress, skill_names=skill_names, stateless=stateless)
        return response.content if response else ""

    async def process_event_batch(
        self,
        topic: str,
        events: list[dict],
        skill_names: list[str] | None = None,
    ) -> None:
        """Process an event batch with no session history and a per-topic scratchpad."""
        await self._connect_mcp()

        scratchpad_dir = ensure_dir(self.workspace / "ipc")
        scratchpad_path = scratchpad_dir / f"{safe_filename(topic)}.json"

        scratchpad: dict = {}
        if scratchpad_path.exists():
            try:
                scratchpad = json.loads(scratchpad_path.read_text())
            except Exception:
                pass

        count = len(events)
        lines = [f"Topic: {topic} ({count} event{'s' if count != 1 else ''})"]
        for e in events:
            ts = e.get("ts", "")[:19].replace("T", " ")
            data = json.dumps(e.get("data"), ensure_ascii=False) if e.get("data") is not None else "(no data)"
            lines.append(f"{ts} {data}")
        content = "\n".join(lines)

        if scratchpad:
            content += f"\n\n[Scratchpad — your persistent notes for topic '{topic}':\n{json.dumps(scratchpad, indent=2)}\n]"

        saved: list[dict] = []

        save_scratchpad_def = {
            "type": "function",
            "function": {
                "name": "save_scratchpad",
                "description": "Persist notes for this topic across future event batches.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "notes": {
                            "type": "string",
                            "description": "Free-form notes to save (replaces current scratchpad).",
                        }
                    },
                    "required": ["notes"],
                },
            },
        }

        async def _handle_save_scratchpad(args: dict) -> str:
            saved.append({"notes": args.get("notes", "")})
            return "Scratchpad saved."

        self._set_tool_context("ipc", topic, None)
        messages = self.context.build_messages(
            history=[],
            current_message=content,
            channel="ipc",
            chat_id=topic,
            skill_names=skill_names or [],
        )

        logger.info("IPC: processing {} event(s) on topic '{}'", count, topic)
        self._current_session_key = f"ipc:{topic}"
        await self._run_agent_loop(
            messages,
            extra_tools={"save_scratchpad": (save_scratchpad_def, _handle_save_scratchpad)},
        )

        if saved:
            scratchpad_path.write_text(json.dumps(saved[-1], indent=2, ensure_ascii=False))
            logger.debug("IPC: scratchpad updated for topic '{}'", topic)

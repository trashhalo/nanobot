"""OpenAI Responses API compatible HTTP server channel (POST /v1/responses)."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Awaitable, Callable

from loguru import logger

from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import ResponsesApiConfig

_HTTP_400 = b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_HTTP_401 = b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_HTTP_404 = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_HTTP_405 = b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


def _json_response(status: int, reason: str, body: dict) -> bytes:
    data = json.dumps(body, ensure_ascii=False).encode()
    return (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(data)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode() + data


def _sse_header() -> bytes:
    return (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/event-stream\r\n"
        b"Cache-Control: no-cache\r\n"
        b"Connection: keep-alive\r\n"
        b"\r\n"
    )


def _sse_event(event_type: str, data: dict) -> bytes:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _extract_user_text(input_val: str | list | None) -> str:
    """Extract the user message text from a Responses API input value."""
    if input_val is None:
        return ""
    if isinstance(input_val, str):
        return input_val
    if isinstance(input_val, list):
        parts = []
        for item in input_val:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            role = item.get("role", "")
            content = item.get("content", "")
            if isinstance(content, str):
                if role in ("user", "system"):
                    parts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "input_text":
                        if role in ("user", "system"):
                            parts.append(part.get("text", ""))
        return "\n".join(p for p in parts if p)
    return str(input_val)


class ResponsesApiChannel(BaseChannel):
    """
    OpenAI Responses API compatible HTTP server.

    Listens on a dedicated port and implements POST /v1/responses,
    supporting both synchronous JSON and SSE streaming responses.

    Conversation threading is handled via previous_response_id, which maps
    to nanobot's internal session keys so memory is preserved across turns.
    """

    name = "responses_api"

    def __init__(
        self,
        config: ResponsesApiConfig,
        bus: MessageBus,
        on_process_direct: Callable[..., Awaitable[str]] | None = None,
    ):
        super().__init__(config, bus)
        self.config: ResponsesApiConfig = config
        self._on_process_direct = on_process_direct
        self._server: asyncio.Server | None = None
        # Maps response_id -> session_key for conversation threading
        self._session_map: dict[str, str] = {}

    async def start(self) -> None:
        self._running = True
        self._server = await asyncio.start_server(
            self._handle_connection,
            host=self.config.host,
            port=self.config.port,
        )
        logger.info(
            "Responses API listening on {}:{} (auth: {})",
            self.config.host,
            self.config.port,
            "yes" if self.config.auth_token else "no",
        )
        await self._server.serve_forever()

    async def stop(self) -> None:
        self._running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def send(self, msg) -> None:
        pass  # Inbound-only; responses go directly back over the HTTP connection.

    # ------------------------------------------------------------------
    # Connection handler
    # ------------------------------------------------------------------

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await self._process_request(reader, writer)
        except Exception as e:
            logger.warning("Responses API: unhandled error: {}", e)
            try:
                writer.write(_HTTP_400)
                await writer.drain()
            except Exception:
                pass
        finally:
            writer.close()

    async def _process_request(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            header_bytes = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=10.0
            )
        except (asyncio.TimeoutError, asyncio.LimitOverrunError):
            writer.write(_HTTP_400)
            await writer.drain()
            return

        header_text = header_bytes.decode("utf-8", errors="replace")
        lines = header_text.split("\r\n")

        parts = lines[0].split(" ", 2) if lines else []
        if len(parts) < 2:
            writer.write(_HTTP_400)
            await writer.drain()
            return
        method, path = parts[0], parts[1].split("?")[0]

        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()

        if path not in ("/v1/responses", "/v1/chat/completions"):
            writer.write(_HTTP_404)
            await writer.drain()
            return

        if method != "POST":
            writer.write(_HTTP_405)
            await writer.drain()
            return

        if self.config.auth_token:
            bearer = headers.get("authorization", "").removeprefix("Bearer ").strip()
            if bearer != self.config.auth_token:
                logger.warning("Responses API: rejected request — invalid auth token")
                writer.write(_HTTP_401)
                await writer.drain()
                return

        content_length = int(headers.get("content-length", "0"))
        if content_length > 1_048_576:
            writer.write(_HTTP_400)
            await writer.drain()
            return

        try:
            body = await asyncio.wait_for(reader.read(content_length), timeout=30.0)
        except asyncio.TimeoutError:
            writer.write(_HTTP_400)
            await writer.drain()
            return

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            writer.write(_HTTP_400)
            await writer.drain()
            return

        if path == "/v1/chat/completions":
            session_id = headers.get("x-session-id")
            await self._handle_completions_endpoint(payload, writer, session_id=session_id)
        else:
            await self._handle_responses_endpoint(payload, writer)

    # ------------------------------------------------------------------
    # Responses API logic
    # ------------------------------------------------------------------

    async def _handle_responses_endpoint(
        self, payload: dict, writer: asyncio.StreamWriter
    ) -> None:
        if not self._on_process_direct:
            err = {"error": {"message": "No agent configured", "type": "server_error"}}
            writer.write(_json_response(503, "Service Unavailable", err))
            await writer.drain()
            return

        resp_id = _new_id("resp")
        msg_id = _new_id("msg")
        created_at = int(time.time())
        model_name = payload.get("model", "nanobot")
        stream = bool(payload.get("stream", False))

        # Resolve session key — previous_response_id threads the conversation
        prev_id = payload.get("previous_response_id")
        if prev_id and prev_id in self._session_map:
            session_key = self._session_map[prev_id]
        else:
            session_key = f"responses_api:{resp_id}"

        # Record this response so future turns can continue the session
        self._session_map[resp_id] = session_key
        if len(self._session_map) > 10_000:
            for old_key in list(self._session_map)[:1_000]:
                del self._session_map[old_key]

        user_text = _extract_user_text(payload.get("input"))

        if stream:
            await self._stream_response(
                writer, resp_id, msg_id, created_at, model_name, session_key, user_text
            )
        else:
            await self._sync_response(
                writer, resp_id, msg_id, created_at, model_name, session_key, user_text
            )

    async def _sync_response(
        self,
        writer: asyncio.StreamWriter,
        resp_id: str,
        msg_id: str,
        created_at: int,
        model_name: str,
        session_key: str,
        user_text: str,
    ) -> None:
        full_text = await self._on_process_direct(
            user_text,
            session_key=session_key,
            channel="responses_api",
            chat_id=resp_id,
        )
        body = {
            "id": resp_id,
            "object": "response",
            "created_at": created_at,
            "status": "completed",
            "model": model_name,
            "output": [
                {
                    "id": msg_id,
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": full_text or ""}],
                }
            ],
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }
        writer.write(_json_response(200, "OK", body))
        await writer.drain()

    async def _stream_response(
        self,
        writer: asyncio.StreamWriter,
        resp_id: str,
        msg_id: str,
        created_at: int,
        model_name: str,
        session_key: str,
        user_text: str,
    ) -> None:
        writer.write(_sse_header())
        await writer.drain()

        # response.created
        writer.write(_sse_event("response.created", {
            "type": "response.created",
            "response": {
                "id": resp_id,
                "object": "response",
                "created_at": created_at,
                "status": "in_progress",
                "model": model_name,
                "output": [],
            },
        }))

        # response.output_item.added
        writer.write(_sse_event("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "id": msg_id,
                "type": "message",
                "status": "in_progress",
                "role": "assistant",
                "content": [],
            },
        }))

        # response.content_part.added
        writer.write(_sse_event("response.content_part.added", {
            "type": "response.content_part.added",
            "item_id": msg_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": ""},
        }))
        await writer.drain()

        # Stream text deltas via on_progress
        delta_queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def on_progress(chunk: str, *, tool_hint: bool = False, final: bool = False) -> None:
            if not tool_hint:
                await delta_queue.put(chunk)

        async def run_agent() -> str:
            try:
                return await self._on_process_direct(
                    user_text,
                    session_key=session_key,
                    channel="responses_api",
                    chat_id=resp_id,
                    on_progress=on_progress,
                )
            finally:
                await delta_queue.put(None)  # sentinel

        agent_task = asyncio.create_task(run_agent())

        while True:
            chunk = await delta_queue.get()
            if chunk is None:
                break
            writer.write(_sse_event("response.output_text.delta", {
                "type": "response.output_text.delta",
                "item_id": msg_id,
                "output_index": 0,
                "content_index": 0,
                "delta": chunk,
            }))
            await writer.drain()

        # Agent is done; get the authoritative full response text
        full_text = await agent_task or ""

        completed_item = {
            "id": msg_id,
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": full_text}],
        }

        writer.write(_sse_event("response.output_text.done", {
            "type": "response.output_text.done",
            "item_id": msg_id,
            "output_index": 0,
            "content_index": 0,
            "text": full_text,
        }))

        writer.write(_sse_event("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": completed_item,
        }))

        writer.write(_sse_event("response.completed", {
            "type": "response.completed",
            "response": {
                "id": resp_id,
                "object": "response",
                "created_at": created_at,
                "status": "completed",
                "model": model_name,
                "output": [completed_item],
                "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            },
        }))

        writer.write(b"data: [DONE]\n\n")
        await writer.drain()

    # ------------------------------------------------------------------
    # Chat Completions API  (POST /v1/chat/completions)
    # ------------------------------------------------------------------

    async def _handle_completions_endpoint(
        self, payload: dict, writer: asyncio.StreamWriter, session_id: str | None = None
    ) -> None:
        if not self._on_process_direct:
            err = {"error": {"message": "No agent configured", "type": "server_error"}}
            writer.write(_json_response(503, "Service Unavailable", err))
            await writer.drain()
            return

        cmpl_id = _new_id("chatcmpl")
        created_at = int(time.time())
        model_name = payload.get("model", "nanobot")
        stream = bool(payload.get("stream", False))

        # Extract user text from messages array — last user message wins
        messages = payload.get("messages", [])
        user_text = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                user_text = content if isinstance(content, str) else str(content)
                break

        # Session key — clients can pass X-Session-ID header to thread conversations
        if session_id:
            session_key = f"responses_api:{session_id}"
        else:
            session_key = f"responses_api:{cmpl_id}"

        if stream:
            await self._completions_stream(writer, cmpl_id, created_at, model_name, session_key, user_text)
        else:
            await self._completions_sync(writer, cmpl_id, created_at, model_name, session_key, user_text)

    async def _completions_sync(
        self,
        writer: asyncio.StreamWriter,
        cmpl_id: str,
        created_at: int,
        model_name: str,
        session_key: str,
        user_text: str,
    ) -> None:
        full_text = await self._on_process_direct(
            user_text,
            session_key=session_key,
            channel="responses_api",
            chat_id=cmpl_id,
        )
        body = {
            "id": cmpl_id,
            "object": "chat.completion",
            "created": created_at,
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": full_text or ""},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        writer.write(_json_response(200, "OK", body))
        await writer.drain()

    async def _completions_stream(
        self,
        writer: asyncio.StreamWriter,
        cmpl_id: str,
        created_at: int,
        model_name: str,
        session_key: str,
        user_text: str,
    ) -> None:
        writer.write(_sse_header())

        # Opening chunk — role announcement
        def _chunk(delta: dict, finish_reason: str | None = None) -> bytes:
            return (
                "data: "
                + json.dumps({
                    "id": cmpl_id,
                    "object": "chat.completion.chunk",
                    "created": created_at,
                    "model": model_name,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
                }, ensure_ascii=False)
                + "\n\n"
            ).encode()

        writer.write(_chunk({"role": "assistant", "content": ""}))
        await writer.drain()

        delta_queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def on_progress(chunk: str, *, tool_hint: bool = False, final: bool = False) -> None:
            if not tool_hint:
                await delta_queue.put(chunk)

        async def run_agent() -> str:
            try:
                return await self._on_process_direct(
                    user_text,
                    session_key=session_key,
                    channel="responses_api",
                    chat_id=cmpl_id,
                    on_progress=on_progress,
                )
            finally:
                await delta_queue.put(None)

        agent_task = asyncio.create_task(run_agent())

        while True:
            chunk = await delta_queue.get()
            if chunk is None:
                break
            writer.write(_chunk({"content": chunk}))
            await writer.drain()

        await agent_task

        writer.write(_chunk({}, finish_reason="stop"))
        writer.write(b"data: [DONE]\n\n")
        await writer.drain()

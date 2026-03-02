"""IPC channel: localhost TCP HTTP (no auth) + optional external TCP HTTP webhook (auth enforced)."""

from __future__ import annotations

import asyncio
import fnmatch
import json
from datetime import datetime, timezone
from typing import Awaitable, Callable

from loguru import logger

from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import IpcConfig, IpcRouteConfig

_HTTP_200 = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_HTTP_400 = b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_HTTP_401 = b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_HTTP_404 = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_HTTP_405 = b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


def _http_429(retry_after: int) -> bytes:
    body = b""
    return (
        b"HTTP/1.1 429 Too Many Requests\r\n"
        b"Content-Length: 0\r\n"
        b"Connection: close\r\n"
        + f"Retry-After: {retry_after}\r\n".encode()
        + b"\r\n"
        + body
    )


class IpcChannel(BaseChannel):
    """
    IPC channel for external event injection.

    Both transports speak HTTP/1.1 and share one handler:

      Localhost TCP — always on, bound to 127.0.0.1, auth never enforced
      External TCP  — optional, configurable host/port, auth enforced when authToken is configured

    Event payload (JSON or NDJSON body):
        {"topic": "sensor.temperature", "data": {...}}

    Topic routing (first match wins):
        - Matched route with chatId       → send to that session
        - Matched route without chatId    → broadcast to N most-recently-active sessions
        - No match + defaultChatId set    → send to that session
        - No match + no default           → broadcast to N most-recently-active sessions
    """

    name = "ipc"

    def __init__(
        self,
        config: IpcConfig,
        bus: MessageBus,
        on_batch: Callable[[str, list[dict], str | None], Awaitable[None]] | None = None,
    ):
        super().__init__(config, bus)
        self.config: IpcConfig = config
        self._on_batch = on_batch
        self._buffers: dict[str, list[dict]] = {}
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._local_server: asyncio.Server | None = None
        self._http_server: asyncio.Server | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        self._running = True
        self._loop = asyncio.get_running_loop()

        tasks: list[asyncio.Task] = []

        # Localhost TCP — always on, no auth
        self._local_server = await asyncio.start_server(
            lambda r, w: self._handle_http(r, w, check_auth=False),
            host=self.config.host,
            port=self.config.port,
        )
        logger.info("IPC local HTTP listening on {}:{}{}", self.config.host, self.config.port, self.config.http.path)
        tasks.append(asyncio.create_task(self._local_server.serve_forever()))

        # External TCP HTTP — optional, auth enforced
        if self.config.http.enabled:
            self._http_server = await asyncio.start_server(
                lambda r, w: self._handle_http(r, w, check_auth=True),
                host=self.config.http.host,
                port=self.config.http.port,
            )
            logger.info(
                "IPC external HTTP listening on {}:{}{} (auth: {})",
                self.config.http.host,
                self.config.http.port,
                self.config.http.path,
                "yes" if self.config.http.auth_token else "no",
            )
            tasks.append(asyncio.create_task(self._http_server.serve_forever()))

        await asyncio.gather(*tasks)

    async def stop(self) -> None:
        self._running = False

        for handle in self._timers.values():
            handle.cancel()
        self._timers.clear()

        for server in (self._local_server, self._http_server):
            if server:
                server.close()
                await server.wait_closed()
        self._local_server = None
        self._http_server = None

    async def send(self, msg: OutboundMessage) -> None:
        # Inbound-only; responses route back through the originating channel.
        pass

    # ------------------------------------------------------------------
    # Unified HTTP handler
    # ------------------------------------------------------------------

    async def _handle_http(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        check_auth: bool,
    ) -> None:
        try:
            await self._process_http(reader, writer, check_auth=check_auth)
        except Exception as e:
            logger.warning("IPC HTTP: error handling request: {}", e)
            try:
                writer.write(_HTTP_400)
                await writer.drain()
            except Exception:
                pass
        finally:
            writer.close()

    async def _process_http(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        check_auth: bool,
    ) -> None:
        # Read headers (up to 8 KB)
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

        # Parse request line
        parts = lines[0].split(" ", 2) if lines else []
        if len(parts) < 2:
            writer.write(_HTTP_400)
            await writer.drain()
            return
        method, path = parts[0], parts[1].split("?")[0]

        # Parse headers
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()

        if path != self.config.http.path:
            writer.write(_HTTP_404)
            await writer.drain()
            return

        if method != "POST":
            writer.write(_HTTP_405)
            await writer.drain()
            return

        # Auth — only when check_auth=True (TCP connections)
        if check_auth and self.config.http.auth_token:
            bearer = headers.get("authorization", "").removeprefix("Bearer ").strip()
            x_auth = headers.get("x-auth-token", "")
            if bearer != self.config.http.auth_token and x_auth != self.config.http.auth_token:
                logger.warning("IPC HTTP: rejected request — invalid auth token")
                writer.write(_HTTP_401)
                await writer.drain()
                return

        # Read body
        content_length = int(headers.get("content-length", "0"))
        if content_length > 1_048_576:
            writer.write(_HTTP_400)
            await writer.drain()
            return

        try:
            body = await asyncio.wait_for(reader.read(content_length), timeout=10.0)
        except asyncio.TimeoutError:
            writer.write(_HTTP_400)
            await writer.drain()
            return

        # Check high-water mark before ingesting
        total_buffered = sum(len(v) for v in self._buffers.values())
        if self.config.max_topic_buffer and total_buffered >= self.config.max_topic_buffer:
            logger.warning("IPC: high-water mark reached ({} events buffered), returning 429", total_buffered)
            writer.write(_http_429(int(self.config.debounce_seconds)))
            await writer.drain()
            return

        self._ingest_lines(body)
        writer.write(_HTTP_200)
        await writer.drain()

    # ------------------------------------------------------------------
    # Ingestion + buffering
    # ------------------------------------------------------------------

    def _ingest_lines(self, data: bytes) -> None:
        for raw_line in data.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning("IPC: invalid JSON: {} — {}", line[:120], e)
                continue

            topic = event.get("topic", "")
            if not topic:
                logger.warning("IPC: event missing 'topic', dropping")
                continue

            self._buffer_event(topic, {
                "ts": datetime.now(tz=timezone.utc).isoformat(),
                "data": event.get("data"),
            })

    def _buffer_event(self, topic: str, event: dict) -> None:
        if topic not in self._buffers:
            self._buffers[topic] = []
        self._buffers[topic].append(event)

        if topic in self._timers:
            self._timers[topic].cancel()

        loop = self._loop or asyncio.get_running_loop()
        self._timers[topic] = loop.call_later(
            self.config.debounce_seconds,
            lambda t=topic: asyncio.ensure_future(self._flush(t)),
        )

    async def _flush(self, topic: str) -> None:
        self._timers.pop(topic, None)
        all_events = self._buffers.pop(topic, [])
        if not all_events:
            return

        batch = all_events[: self.config.max_batch_size]
        overflow = all_events[self.config.max_batch_size :]

        if overflow:
            self._buffers[topic] = overflow
            loop = self._loop or asyncio.get_running_loop()
            self._timers[topic] = loop.call_soon(
                lambda t=topic: asyncio.ensure_future(self._flush(t))
            )
            logger.debug("IPC: topic '{}' overflow: {} event(s) queued for next batch", topic, len(overflow))

        route = self._match_route(topic)

        if route and route.handler == "script":
            await self._run_script(topic, batch, route)
        else:
            await self._dispatch_to_agent(topic, batch, route)

    async def _dispatch_to_agent(
        self, topic: str, events: list[dict], route: IpcRouteConfig | None
    ) -> None:
        if not self._on_batch:
            logger.warning("IPC: no on_batch handler configured, dropping {} event(s) on '{}'", len(events), topic)
            return
        skill_hint = route.skill if route else None
        logger.info("IPC: flushing {} event(s) on '{}'", len(events), topic)
        await self._on_batch(topic, events, skill_hint)

    async def _run_script(
        self, topic: str, events: list[dict], route: IpcRouteConfig
    ) -> None:
        if not route.script:
            logger.warning("IPC: route for '{}' has handler=script but no script set, dropping", topic)
            return

        # Build NDJSON stdin — one line per event
        stdin_lines = [
            json.dumps({"topic": topic, "ts": e.get("ts"), "data": e.get("data")}, ensure_ascii=False)
            for e in events
        ]
        stdin_payload = "\n".join(stdin_lines).encode()

        env_extra = {
            "IPC_TOPIC": topic,
            "IPC_EVENT_COUNT": str(len(events)),
        }

        logger.info("IPC: running script for topic '{}' ({} event(s)): {}", topic, len(events), route.script)
        asyncio.create_task(
            self._exec_script(route.script, stdin_payload, env_extra, timeout=route.script_timeout)
        )

    @staticmethod
    async def _exec_script(command: str, stdin: bytes, env_extra: dict[str, str], timeout: int) -> None:
        import os
        env = {**os.environ, **env_extra}
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                logger.warning("IPC script timed out after {}s: {}", timeout, command)
                return

            if proc.returncode != 0:
                logger.warning(
                    "IPC script exited {} — stderr: {}",
                    proc.returncode,
                    stderr.decode(errors="replace")[:500],
                )
            else:
                logger.debug("IPC script ok — stdout: {}", stdout.decode(errors="replace")[:500])
        except Exception as e:
            logger.error("IPC script failed to start: {} — {}", command, e)

    # ------------------------------------------------------------------
    # Routing helpers
    # ------------------------------------------------------------------

    def _match_route(self, topic: str) -> IpcRouteConfig | None:
        for route in self.config.routes:
            if fnmatch.fnmatch(topic, route.pattern):
                return route
        return None


"""Tests for the IPC channel (localhost HTTP + optional external HTTP)."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from nanobot.bus.queue import MessageBus
from nanobot.channels.ipc import IpcChannel, _http_429
from nanobot.config.schema import IpcConfig, IpcHttpConfig, IpcRouteConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_channel(
    routes: list[IpcRouteConfig] | None = None,
    max_batch_size: int = 100,
    max_topic_buffer: int = 1000,
    debounce_seconds: float = 999.0,  # large — tests control flushing manually
    auth_token: str = "secret",
    on_batch=None,
) -> tuple[IpcChannel, MessageBus]:
    config = IpcConfig(
        enabled=True,
        port=0,  # not bound in unit tests
        debounce_seconds=debounce_seconds,
        max_batch_size=max_batch_size,
        max_topic_buffer=max_topic_buffer,
        routes=routes or [],
        http=IpcHttpConfig(enabled=False, auth_token=auth_token),
    )
    bus = MessageBus()
    channel = IpcChannel(config, bus, on_batch=on_batch)
    return channel, bus


async def _start_http_server(
    channel: IpcChannel,
    check_auth: bool = False,
) -> tuple[asyncio.Server, int]:
    """Start a temporary test server on a random port."""
    server = await asyncio.start_server(
        lambda r, w: channel._handle_http(r, w, check_auth=check_auth),
        host="127.0.0.1",
        port=0,
    )
    port = server.sockets[0].getsockname()[1]
    return server, port


async def _post(port: int, body: str | bytes, *, token: str = "", path: str = "/events") -> httpx.Response:
    if isinstance(body, str):
        body = body.encode()
    async with httpx.AsyncClient() as client:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return await client.post(f"http://127.0.0.1:{port}{path}", content=body, headers=headers)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def test_match_route_first_match_wins():
    routes = [
        IpcRouteConfig(pattern="sensor.*"),
        IpcRouteConfig(pattern="sensor.temp"),
    ]
    channel, _ = _make_channel(routes=routes)
    assert channel._match_route("sensor.temp").pattern == "sensor.*"


def test_match_route_no_match():
    channel, _ = _make_channel(routes=[IpcRouteConfig(pattern="sensor.*")])
    assert channel._match_route("alert.disk") is None


def test_match_route_wildcard():
    channel, _ = _make_channel(routes=[IpcRouteConfig(pattern="deploy.*")])
    assert channel._match_route("deploy.api.finished") is not None


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

async def test_ingest_lines_buffers_valid_events():
    channel, _ = _make_channel()
    channel._ingest_lines(b'{"topic":"sensor.temp","data":{"v":72}}\n')
    assert "sensor.temp" in channel._buffers
    assert len(channel._buffers["sensor.temp"]) == 1
    assert channel._buffers["sensor.temp"][0]["data"] == {"v": 72}


async def test_ingest_lines_skips_invalid_json():
    channel, _ = _make_channel()
    channel._ingest_lines(b"not-json\n")
    assert channel._buffers == {}


async def test_ingest_lines_skips_missing_topic():
    channel, _ = _make_channel()
    channel._ingest_lines(b'{"data":{"v":1}}\n')
    assert channel._buffers == {}


async def test_ingest_lines_multiple_events_same_topic():
    channel, _ = _make_channel()
    payload = b'{"topic":"t","data":1}\n{"topic":"t","data":2}\n'
    channel._ingest_lines(payload)
    assert len(channel._buffers["t"]) == 2


async def test_ingest_lines_multiple_topics():
    channel, _ = _make_channel()
    channel._ingest_lines(b'{"topic":"a","data":1}\n{"topic":"b","data":2}\n')
    assert "a" in channel._buffers
    assert "b" in channel._buffers


# ---------------------------------------------------------------------------
# Flush — agent dispatch via on_batch callback
# ---------------------------------------------------------------------------

async def test_flush_calls_on_batch():
    route = IpcRouteConfig(pattern="sensor.*")
    calls = []

    async def on_batch(topic, events, skills):
        calls.append({"topic": topic, "count": len(events), "skills": skills})

    channel, _ = _make_channel(routes=[route], on_batch=on_batch)
    channel._buffers["sensor.temp"] = [{"ts": "2026-01-01T00:00:00+00:00", "data": {"v": 1}}]

    await channel._flush("sensor.temp")

    assert len(calls) == 1
    assert calls[0]["topic"] == "sensor.temp"
    assert calls[0]["count"] == 1


async def test_flush_no_on_batch_drops_events():
    channel, _ = _make_channel()
    channel._buffers["orphan.topic"] = [{"ts": "t", "data": None}]
    # No on_batch set — should log warning and drop without error
    await channel._flush("orphan.topic")
    assert channel._buffers.get("orphan.topic") is None


async def test_flush_empty_buffer_is_noop():
    channel, _ = _make_channel()
    await channel._flush("nonexistent.topic")


async def test_flush_passes_skills_to_on_batch():
    route = IpcRouteConfig(pattern="*", skills=["my-skill"])
    calls = []

    async def on_batch(topic, events, skills):
        calls.append(skills)

    channel, _ = _make_channel(routes=[route], on_batch=on_batch)
    channel._buffers["foo"] = [{"ts": "t", "data": None}]
    await channel._flush("foo")
    assert calls[0] == ["my-skill"]


# ---------------------------------------------------------------------------
# Flush — batch overflow
# ---------------------------------------------------------------------------

async def test_flush_overflow_held_for_next_batch():
    route = IpcRouteConfig(pattern="*")
    batches = []

    async def on_batch(topic, events, skills):
        batches.append(len(events))

    channel, _ = _make_channel(routes=[route], max_batch_size=2, on_batch=on_batch)
    channel._buffers["t"] = [{"ts": "x", "data": i} for i in range(5)]

    await channel._flush("t")

    # First batch: 2 events dispatched
    assert len(batches) == 1
    assert batches[0] == 2

    # Remaining 3 events re-buffered
    assert len(channel._buffers["t"]) == 3
    # A follow-up timer should be scheduled
    assert "t" in channel._timers


async def test_flush_overflow_processes_remainder():
    route = IpcRouteConfig(pattern="*")
    batches = []

    async def on_batch(topic, events, skills):
        batches.append(len(events))

    channel, _ = _make_channel(routes=[route], max_batch_size=2, on_batch=on_batch)
    channel._buffers["t"] = [{"ts": "x", "data": i} for i in range(3)]

    await channel._flush("t")
    # Allow the follow-up call_soon task to run
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert sum(batches) == 3


# ---------------------------------------------------------------------------
# Flush — script handler
# ---------------------------------------------------------------------------

async def test_flush_script_handler_calls_exec(monkeypatch):
    calls: list[dict] = []

    async def fake_exec(command, stdin, env_extra, timeout):
        calls.append({"command": command, "stdin": stdin, "env": env_extra, "timeout": timeout})

    monkeypatch.setattr(IpcChannel, "_exec_script", staticmethod(fake_exec))

    route = IpcRouteConfig(pattern="*", handler="script", script="echo hello", script_timeout=10)
    channel, _ = _make_channel(routes=[route])
    channel._buffers["backup.done"] = [{"ts": "t", "data": {"path": "/db"}}]

    await channel._flush("backup.done")
    await asyncio.sleep(0)  # let the create_task fire

    assert len(calls) == 1
    assert calls[0]["command"] == "echo hello"
    assert calls[0]["timeout"] == 10
    assert calls[0]["env"]["IPC_TOPIC"] == "backup.done"
    assert calls[0]["env"]["IPC_EVENT_COUNT"] == "1"
    # stdin should be valid NDJSON
    line = json.loads(calls[0]["stdin"].split(b"\n")[0])
    assert line["topic"] == "backup.done"
    assert line["data"] == {"path": "/db"}


async def test_flush_script_handler_no_script_set_drops():
    route = IpcRouteConfig(pattern="*", handler="script", script=None)
    channel, _ = _make_channel(routes=[route])
    channel._buffers["t"] = [{"ts": "x", "data": None}]
    await channel._flush("t")
    # No error, events dropped


# ---------------------------------------------------------------------------
# HTTP handler — local (no auth)
# ---------------------------------------------------------------------------

async def test_http_local_accepts_valid_event():
    channel, _ = _make_channel()
    server, port = await _start_http_server(channel, check_auth=False)

    try:
        resp = await _post(port, '{"topic":"sensor.temp","data":{"v":72}}')
        assert resp.status_code == 200
        assert "sensor.temp" in channel._buffers
    finally:
        server.close()
        await server.wait_closed()


async def test_http_local_accepts_ndjson():
    channel, _ = _make_channel()
    server, port = await _start_http_server(channel, check_auth=False)

    try:
        body = '{"topic":"a","data":1}\n{"topic":"b","data":2}\n'
        resp = await _post(port, body)
        assert resp.status_code == 200
        assert "a" in channel._buffers
        assert "b" in channel._buffers
    finally:
        server.close()
        await server.wait_closed()


async def test_http_local_ignores_auth_header_even_when_token_configured():
    """Local server must accept requests without a token even if auth_token is set."""
    channel, _ = _make_channel(auth_token="secret")
    server, port = await _start_http_server(channel, check_auth=False)

    try:
        resp = await _post(port, '{"topic":"t","data":null}')
        assert resp.status_code == 200
    finally:
        server.close()
        await server.wait_closed()


async def test_http_wrong_path_returns_404():
    channel, _ = _make_channel()
    server, port = await _start_http_server(channel, check_auth=False)

    try:
        resp = await _post(port, '{"topic":"t"}', path="/wrong")
        assert resp.status_code == 404
    finally:
        server.close()
        await server.wait_closed()


async def test_http_wrong_method_returns_405():
    channel, _ = _make_channel()
    server, port = await _start_http_server(channel, check_auth=False)

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://127.0.0.1:{port}/events")
        assert resp.status_code == 405
    finally:
        server.close()
        await server.wait_closed()


async def test_http_bad_json_body_is_skipped_gracefully():
    """Bad JSON lines are logged and skipped; request still returns 200."""
    channel, _ = _make_channel()
    server, port = await _start_http_server(channel, check_auth=False)

    try:
        resp = await _post(port, "not-json")
        assert resp.status_code == 200
        assert channel._buffers == {}
    finally:
        server.close()
        await server.wait_closed()


# ---------------------------------------------------------------------------
# HTTP handler — external (auth enforced)
# ---------------------------------------------------------------------------

async def test_http_external_correct_token_accepted():
    channel, _ = _make_channel(auth_token="secret")
    server, port = await _start_http_server(channel, check_auth=True)

    try:
        resp = await _post(port, '{"topic":"t","data":null}', token="secret")
        assert resp.status_code == 200
    finally:
        server.close()
        await server.wait_closed()


async def test_http_external_wrong_token_returns_401():
    channel, _ = _make_channel(auth_token="secret")
    server, port = await _start_http_server(channel, check_auth=True)

    try:
        resp = await _post(port, '{"topic":"t","data":null}', token="wrong")
        assert resp.status_code == 401
        assert channel._buffers == {}
    finally:
        server.close()
        await server.wait_closed()


async def test_http_external_missing_token_returns_401():
    channel, _ = _make_channel(auth_token="secret")
    server, port = await _start_http_server(channel, check_auth=True)

    try:
        resp = await _post(port, '{"topic":"t","data":null}', token="")
        assert resp.status_code == 401
    finally:
        server.close()
        await server.wait_closed()


async def test_http_external_x_auth_token_header_accepted():
    channel, _ = _make_channel(auth_token="secret")
    server, port = await _start_http_server(channel, check_auth=True)

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"http://127.0.0.1:{port}/events",
                content=b'{"topic":"t","data":null}',
                headers={"X-Auth-Token": "secret", "Content-Type": "application/json"},
            )
        assert resp.status_code == 200
    finally:
        server.close()
        await server.wait_closed()


async def test_http_external_no_auth_token_configured_accepts_all():
    """When authToken is empty, external endpoint accepts without credentials."""
    channel, _ = _make_channel(auth_token="")
    server, port = await _start_http_server(channel, check_auth=True)

    try:
        resp = await _post(port, '{"topic":"t","data":null}', token="")
        assert resp.status_code == 200
    finally:
        server.close()
        await server.wait_closed()


# ---------------------------------------------------------------------------
# High-water mark → 429
# ---------------------------------------------------------------------------

async def test_http_returns_429_when_hwm_exceeded():
    channel, _ = _make_channel(max_topic_buffer=2)
    # Pre-fill the buffer to the limit
    channel._buffers["t"] = [{"ts": "x", "data": i} for i in range(2)]
    server, port = await _start_http_server(channel, check_auth=False)

    try:
        resp = await _post(port, '{"topic":"t","data":99}')
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers
    finally:
        server.close()
        await server.wait_closed()


async def test_http_accepts_when_below_hwm():
    channel, _ = _make_channel(max_topic_buffer=10)
    channel._buffers["t"] = [{"ts": "x", "data": i} for i in range(5)]
    server, port = await _start_http_server(channel, check_auth=False)

    try:
        resp = await _post(port, '{"topic":"t","data":99}')
        assert resp.status_code == 200
    finally:
        server.close()
        await server.wait_closed()


def test_http_429_response_includes_retry_after():
    response = _http_429(retry_after=5)
    assert b"429" in response
    assert b"Retry-After: 5" in response


# ---------------------------------------------------------------------------
# Full pipeline integration
# ---------------------------------------------------------------------------

async def test_full_pipeline_event_reaches_on_batch():
    """POST event → debounce skipped (manual flush) → on_batch called."""
    route = IpcRouteConfig(pattern="sensor.*")
    calls = []

    async def on_batch(topic, events, skills):
        calls.append({"topic": topic, "events": events})

    channel, _ = _make_channel(routes=[route], on_batch=on_batch)
    server, port = await _start_http_server(channel, check_auth=False)

    try:
        resp = await _post(port, '{"topic":"sensor.temp","data":{"v":72.1}}')
        assert resp.status_code == 200

        # Manually flush instead of waiting for debounce timer
        await channel._flush("sensor.temp")

        assert len(calls) == 1
        assert calls[0]["topic"] == "sensor.temp"
        assert calls[0]["events"][0]["data"]["v"] == 72.1
    finally:
        server.close()
        await server.wait_closed()


async def test_full_pipeline_ndjson_batch_reaches_on_batch():
    """Multiple NDJSON events in one request arrive as a single flush."""
    route = IpcRouteConfig(pattern="sensor.*")
    calls = []

    async def on_batch(topic, events, skills):
        calls.append(len(events))

    channel, _ = _make_channel(routes=[route], on_batch=on_batch)
    server, port = await _start_http_server(channel, check_auth=False)

    body = (
        '{"topic":"sensor.temp","data":{"v":70}}\n'
        '{"topic":"sensor.temp","data":{"v":71}}\n'
        '{"topic":"sensor.temp","data":{"v":72}}\n'
    )

    try:
        resp = await _post(port, body)
        assert resp.status_code == 200
        assert len(channel._buffers["sensor.temp"]) == 3

        await channel._flush("sensor.temp")

        assert len(calls) == 1
        assert calls[0] == 3
    finally:
        server.close()
        await server.wait_closed()

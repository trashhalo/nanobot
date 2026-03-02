---
name: ipc-events
description: "Send async fire-and-forget events to nanobot from shell scripts, Python, or any external process via localhost HTTP or an optional external HTTP webhook."
---

# IPC Events Skill

Two HTTP transports, one handler, same event format and debounce buffers:

- **Localhost TCP** (`127.0.0.1:18790`) — always on, no auth, for local scripts and in-process callers
- **External TCP** (optional, configurable host/port) — auth enforced, for remote callers and CI systems

Both return `429 Too Many Requests` with a `Retry-After` header when the high-water mark is reached.

## Event format

Request body is a single JSON object or NDJSON (newline-delimited) for multiple events:

```json
{"topic": "your.topic", "data": {...}}
```

- `topic` — dot-separated string matched against route patterns (e.g. `sensor.temperature`)
- `data` — any JSON value; omit or use `null` if no payload needed

## Localhost (no auth)

```bash
# Single event
curl -sf -X POST http://127.0.0.1:18790/events \
  -H "Content-Type: application/json" \
  -d '{"topic": "deploy.finished", "data": {"service": "api", "sha": "abc123"}}'

# NDJSON — multiple events in one request
printf '%s\n%s\n' \
  '{"topic":"sensor.temperature","data":{"value":72.1}}' \
  '{"topic":"sensor.humidity","data":{"value":45}}' \
  | curl -sf -X POST http://127.0.0.1:18790/events --data-binary @-
```

Shell helper for scripts:

```bash
nanobot_event() {
  local topic="$1"
  local data="${2:-null}"
  curl -sf -X POST http://127.0.0.1:18790/events \
    -H "Content-Type: application/json" \
    -d "{\"topic\":\"$topic\",\"data\":$data}"
}

nanobot_event "deploy.finished" '{"service":"api","sha":"abc123"}'
nanobot_event "backup.complete"
```

Python:

```python
import httpx

def send_event(topic: str, data=None):
    httpx.post("http://127.0.0.1:18790/events", json={"topic": topic, "data": data})

send_event("deploy.finished", {"service": "api", "sha": "abc123"})
send_event("backup.complete")
```

## External HTTP (optional, with auth)

```bash
curl -sf -X POST http://your-host:18791/events \
  -H "Authorization: Bearer mysecrettoken" \
  -H "Content-Type: application/json" \
  -d '{"topic": "alert.disk", "data": {"pct": 95}}'

# X-Auth-Token header also accepted
curl -sf -X POST http://your-host:18791/events \
  -H "X-Auth-Token: mysecrettoken" \
  -d '{"topic": "alert.disk", "data": {"pct": 95}}'
```

Python:

```python
import httpx

def send_event(topic: str, data=None, token: str = ""):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    httpx.post("http://your-host:18791/events",
               json={"topic": topic, "data": data}, headers=headers)
```

## HTTP status codes

| Code | Meaning |
|---|---|
| `200` | Accepted |
| `400` | Malformed request |
| `401` | Missing or invalid auth token (external endpoint only) |
| `404` | Wrong path |
| `405` | Non-POST method |
| `429` | High-water mark reached — back off for `Retry-After` seconds |

## Topic naming

Use dot-separated namespaces matched with fnmatch patterns in config:

| Topic | Matches |
|---|---|
| `sensor.temperature` | `sensor.*` |
| `alert.disk` | `alert.*` |
| `deploy.api.finished` | `deploy.*` or `deploy.api.*` |

## Batching behavior

- Events on the same topic within `debounceSeconds` (default 5s) are grouped into one agent message
- If buffered events exceed `maxBatchSize` (default 100), the flush delivers the first batch immediately and schedules the remainder — no extra debounce wait
- If total buffered events exceed `maxTopicBuffer` (default 1000), both endpoints return `429` with `Retry-After: <debounceSeconds>`

## Configuration

```json
"channels": {
  "ipc": {
    "enabled": true,
    "port": 18790,
    "debounceSeconds": 5,
    "maxBatchSize": 100,
    "maxTopicBuffer": 1000,
    "routes": [
      {
        "pattern": "sensor.*",
        "handler": "agent",
        "chatId": "telegram:123456",
        "skill": "sensor-monitor"
      },
      {
        "pattern": "backup.*",
        "handler": "script",
        "script": "~/scripts/on-backup.sh",
        "scriptTimeout": 30
      },
      {
        "pattern": "alert.*",
        "handler": "agent",
        "skill": "alerting"
      },
      {
        "pattern": "deploy.*",
        "handler": "script",
        "script": "/usr/local/bin/deploy-hook"
      }
    ],
    "http": {
      "enabled": true,
      "host": "0.0.0.0",
      "port": 18791,
      "path": "/events",
      "authToken": "mysecrettoken"
    }
  }
}
```

### Route fields

| Field | Default | Description |
|---|---|---|
| `pattern` | required | fnmatch pattern matched against topic |
| `handler` | `"agent"` | `"agent"` or `"script"` |
| `chatId` | null | agent only — target session (`channel:chat_id`); null = active sessions |
| `skill` | null | agent only — skill hint prepended to message |
| `script` | null | script only — shell command or path to execute |
| `scriptTimeout` | `30` | script only — seconds before process is killed |

Routes are evaluated top-down; first match wins. Unmatched topics with `handler=agent` fall back to `defaultChatId` if set, otherwise most-recently-active sessions.

## Script handler

When `handler=script`, the batch is passed to the script as **NDJSON on stdin** — one JSON line per event:

```json
{"topic": "backup.complete", "ts": "2026-03-02T10:00:01+00:00", "data": {"path": "/var/db"}}
{"topic": "backup.complete", "ts": "2026-03-02T10:00:03+00:00", "data": {"path": "/var/log"}}
```

Environment variables also set:

| Variable | Value |
|---|---|
| `IPC_TOPIC` | the topic string |
| `IPC_EVENT_COUNT` | number of events in this batch |

Example script:

```bash
#!/bin/bash
# ~/scripts/on-backup.sh
while IFS= read -r line; do
  path=$(echo "$line" | jq -r '.data.path')
  echo "Backup completed: $path" >> /var/log/backups.log
done
```

Scripts run fire-and-forget — the IPC channel does not wait for them. Non-zero exit codes and stderr are logged as warnings.

## Writing scripts that emit events

1. Use `curl` against `http://127.0.0.1:18790/events` for local scripts — no auth, no dependencies
2. Choose a topic matching an existing route pattern, or ask the user to add one
3. Put meaningful structured data in `data` — the agent sees it verbatim
4. Handle `429` if reliability matters: back off for the `Retry-After` duration and retry

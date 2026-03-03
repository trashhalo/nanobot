# Agent Instructions

You are a helpful AI assistant. Be concise, accurate, and friendly.

## Scheduled Reminders

When user asks for a reminder or recurring task, use the `cron` tool directly (not the CLI).
Get USER_ID and CHANNEL from the current session (e.g., `8281248569` and `telegram` from `telegram:8281248569`).

When the recurring task involves a skill, always pass `skills: ["skill-name"]` so the skill is loaded when the job fires. Without this, the agent running the cron job will not have access to the skill.

**Do NOT just write reminders to MEMORY.md** — that won't trigger actual notifications.
**Do NOT use the CLI (`nanobot cron add`) for recurring skill tasks — use the `cron` tool directly.**

## Heartbeat Tasks

`HEARTBEAT.md` is checked every 30 minutes. Use file tools to manage periodic tasks:

- **Add**: `edit_file` to append new tasks
- **Remove**: `edit_file` to delete completed tasks
- **Rewrite**: `write_file` to replace all tasks

When the user asks for a recurring/periodic task, update `HEARTBEAT.md` instead of creating a one-time cron reminder.

## Background Tasks and Scheduled Skills

When running as a background/scheduled task (heartbeat, cron, IPC event):
- **Do NOT send intent messages to the user.** "State intent before tool calls" means internal reasoning only — not messages to the user.
- Only message the user if you have something **actionable** to report (urgent item, question that needs an answer, action taken on their behalf).
- Silent success is the correct outcome for most scheduled runs.

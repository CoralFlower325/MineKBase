#!/usr/bin/env python3
"""Send one privacy-preserving macOS reminder for due review tasks.

The reminder is intentionally read-only: it opens the SQLite database in
read-only mode, selects currently open tasks whose due time has arrived, and
delegates one aggregated notification to ``osascript``.  No question text or
answer data is read from the database.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB = PROJECT_ROOT / "library.sqlite"
WEB_URL = "http://127.0.0.1:8765/"


def utc_now_text(value: str | None) -> str:
    """Return an ISO-8601 UTC timestamp matching the app's stored format."""

    if value is None:
        current = dt.datetime.now(dt.timezone.utc)
    else:
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            current = dt.datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError("--now must be an ISO-8601 timestamp") from exc
        if current.tzinfo is None:
            current = current.replace(tzinfo=dt.timezone.utc)
        else:
            current = current.astimezone(dt.timezone.utc)
    return current.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def due_tasks(db_path: Path, now_text: str) -> list[sqlite3.Row]:
    """Read due open tasks without allowing SQLite writes."""

    resolved = db_path.expanduser().resolve()
    uri = f"file:{resolved}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            "SELECT review_task_id, due_at "
            "FROM ReviewTask "
            "WHERE status='open' AND julianday(due_at) <= julianday(?) "
            "ORDER BY due_at ASC",
            (now_text,),
        ).fetchall()
    finally:
        connection.close()


def apple_script_message(message: str) -> str:
    escaped = message.replace("\\", "\\\\").replace('"', '\\"')
    return f'display notification "{escaped}" with title "错题回测提醒"'


def send_notification(count: int, earliest_due: str) -> None:
    earliest_date = earliest_due[:10] or "待确认"
    message = (
        f"有 {count} 条回测到期，最早到期 {earliest_date}。"
        f" 请打开 WebUI：{WEB_URL}"
    )
    subprocess.run(
        ["osascript", "-e", apple_script_message(message)],
        check=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="提醒到期的错题回测任务")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    parser.add_argument("--now", help="用于验证的 ISO-8601 当前时间（默认当前 UTC）")
    args = parser.parse_args(argv)

    try:
        now_text = utc_now_text(args.now)
        rows = due_tasks(Path(args.db), now_text)
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"notify_due: {exc}", file=sys.stderr)
        return 1

    if not rows:
        return 0

    try:
        send_notification(len(rows), rows[0]["due_at"])
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"notify_due: unable to send notification: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

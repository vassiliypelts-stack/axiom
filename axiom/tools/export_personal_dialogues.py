"""Read-only export of meaningful personal Telegram dialogues for style analysis.

The exporter intentionally does *not* download media, send messages, mark messages
as read, or alter folders.  It accepts only one account at a time and exports chats
with Telegram ``User`` entities (not groups/channels/bots).  A chat is retained only
when it contains at least one authored outgoing message in the selected period; this
filters out Telegram service notifications such as "a contact joined Telegram".

Run this where the authorised account session and its usual proxy live:
    python tools/export_personal_dialogues.py --account-id 5 --since 2025-09-20

The JSONL output contains private data.  Keep it outside git and transfer it only to
the approved analysis workspace.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, time, timezone
from pathlib import Path

from telethon.tl.types import Message, User

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from channels.telegram import client_for_account  # noqa: E402


def _parse_day(value: str) -> datetime:
    try:
        return datetime.combine(datetime.fromisoformat(value).date(), time.min, tzinfo=timezone.utc)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use YYYY-MM-DD") from exc


def _text(message: Message) -> str | None:
    """Return actual authored text/caption; ignore service events and empty media."""
    if getattr(message, "action", None) is not None:
        return None
    text = (getattr(message, "raw_text", None) or "").strip()
    return text or None


async def export(account_id: int, since: datetime, output: Path) -> dict:
    client, _ = client_for_account(account_id)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError(f"account #{account_id} session is not authorised")

    output.parent.mkdir(parents=True, exist_ok=True)
    kept = skipped_kind = skipped_empty = messages = 0
    try:
        with output.open("w", encoding="utf-8") as fh:
            async for dialog in client.iter_dialogs(limit=None):
                person = dialog.entity
                # Bots, groups, channels, Saved Messages and service dialogs are excluded.
                if not isinstance(person, User) or person.bot or getattr(person, "self", False):
                    skipped_kind += 1
                    continue

                rows: list[dict] = []
                outgoing = 0
                async for item in client.iter_messages(person, limit=None):
                    if not isinstance(item, Message):
                        continue
                    stamp = item.date.astimezone(timezone.utc)
                    if stamp < since:
                        break
                    body = _text(item)
                    if body is None:
                        continue
                    row = {
                        "id": item.id,
                        "date": stamp.isoformat(),
                        "direction": "out" if item.out else "in",
                        "text": body,
                    }
                    rows.append(row)
                    outgoing += int(item.out)

                # At least one genuine authored message from the account is required.
                # This rejects empty chats and Telegram's contact/system notices.
                if not outgoing:
                    skipped_empty += 1
                    continue
                rows.reverse()  # chronological order makes later analysis deterministic
                fh.write(json.dumps({
                    "account_id": account_id,
                    "peer": {
                        "id": person.id,
                        "name": " ".join(x for x in [person.first_name, person.last_name] if x),
                        "username": person.username,
                        "deleted": bool(getattr(person, "deleted", False)),
                    },
                    "messages": rows,
                }, ensure_ascii=False) + "\n")
                kept += 1
                messages += len(rows)
    finally:
        await client.disconnect()

    return {"account_id": account_id, "since": since.date().isoformat(), "output": str(output),
            "personal_dialogues": kept, "messages": messages,
            "skipped_nonpersonal": skipped_kind, "skipped_without_your_text": skipped_empty}


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only export of personal Telegram dialogues")
    parser.add_argument("--account-id", type=int, required=True)
    parser.add_argument("--since", type=_parse_day, required=True, help="inclusive UTC date: YYYY-MM-DD")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    output = args.output or Path(__file__).resolve().parent.parent / "data" / f"personal_dialogues_{args.account_id}.jsonl"
    result = asyncio.run(export(args.account_id, args.since, output))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

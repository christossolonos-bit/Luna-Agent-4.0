"""
Daily medicine reminder: adds a recurring 7pm reminder to Luna's reminder store.
Run this once (e.g. python dailymedreminder.py from project root) to register.
Luna will then DM you at 19:00 every day with a text + voice note: "Hey, remember you need to take your medicine."
Requires LINKED_DISCORD_USER_ID in .env (or set in environment).
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from datetime import datetime, timezone

# Bot's data folder: this script lives in Luna projects/agents/ -> go up to Luna 4.0 then data/
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent.parent
_REMINDERS_FILE = _PROJECT_ROOT / "data" / "reminders.json"

REMINDER_TIME = "19:00"
MESSAGE = "take your medicine"


def main() -> None:
    user_id = os.environ.get("LINKED_DISCORD_USER_ID", "").strip()
    if not user_id:
        print("Set LINKED_DISCORD_USER_ID in .env (your Discord user ID). Luna needs it to DM you.")
        return

    reminders = []
    if _REMINDERS_FILE.is_file():
        try:
            with open(_REMINDERS_FILE, encoding="utf-8") as f:
                reminders = json.load(f)
            if not isinstance(reminders, list):
                reminders = []
        except Exception:
            reminders = []

    # Avoid duplicate daily medicine reminder
    for r in reminders:
        if r.get("message") == MESSAGE and r.get("time") == REMINDER_TIME and r.get("recurring") == "daily":
            print("Daily medicine reminder at 7pm is already registered. Luna will DM you.")
            return

    entry = {
        "id": str(uuid.uuid4())[:8],
        "time": REMINDER_TIME,
        "message": MESSAGE,
        "discord_user_id": user_id,
        "recurring": "daily",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    reminders.append(entry)
    _REMINDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_REMINDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(reminders, f, indent=2, ensure_ascii=False)

    print("Daily medicine reminder at 7pm registered. Luna will DM you with a voice note every day.")


if __name__ == "__main__":
    main()

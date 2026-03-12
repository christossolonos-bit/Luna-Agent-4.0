"""
George — Shadow's sub-agent for scheduling share-to-X and share-to-Facebook (YouTube channel songs).
Runs at fixed times (Cyprus/local): X at 10:00 and 18:00; Facebook at 11:00 and 19:00.
Same share commands as manual (!share_song / Shadow share on X, etc.); George just runs them on a schedule.
"""
from __future__ import annotations

import os
import time
import threading
from typing import Callable

schedule = None
try:
    import schedule as _sched
    schedule = _sched
except ImportError:
    pass

# Times in local time (set PC to Cyprus/EET or override via env). X and Facebook each have two times per day.
GEORGE_SCHEDULE_X_TIMES = (os.environ.get("GEORGE_SCHEDULE_X_TIMES") or "10:00,18:00").strip().split(",")
GEORGE_SCHEDULE_X_TIMES = [t.strip() for t in GEORGE_SCHEDULE_X_TIMES if t.strip()] or ["10:00", "18:00"]

GEORGE_SCHEDULE_FACEBOOK_TIMES = (os.environ.get("GEORGE_SCHEDULE_FACEBOOK_TIMES") or "11:00,19:00").strip().split(",")
GEORGE_SCHEDULE_FACEBOOK_TIMES = [t.strip() for t in GEORGE_SCHEDULE_FACEBOOK_TIMES if t.strip()] or ["11:00", "19:00"]


def start_george(share_x_fn: Callable[[], None], share_facebook_fn: Callable[[], None]) -> None:
    """
    Start George's scheduler: run share_x_fn at X times, share_facebook_fn at Facebook times.
    Callbacks are called with no arguments (they run the same logic as manual share-to-X and share-to-Facebook).
    """
    if not schedule:
        print("[George] 'schedule' not installed; pip install schedule for automatic X/Facebook posts.", flush=True)
        return
    for t in GEORGE_SCHEDULE_X_TIMES:
        if t and ":" in t:
            schedule.every().day.at(t).do(share_x_fn)
    for t in GEORGE_SCHEDULE_FACEBOOK_TIMES:
        if t and ":" in t:
            schedule.every().day.at(t).do(share_facebook_fn)

    def _loop() -> None:
        while True:
            schedule.run_pending()
            time.sleep(60)

    threading.Thread(target=_loop, daemon=True).start()
    print(
        f"[George] X at {', '.join(GEORGE_SCHEDULE_X_TIMES)}; Facebook at {', '.join(GEORGE_SCHEDULE_FACEBOOK_TIMES)} (Shadow's scheduler for YouTube shares).",
        flush=True,
    )

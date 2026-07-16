"""The application clock.

Everything that scores against "now" — the board, the dashboard, availability —
goes through default_now() so the demo can be pinned to a shop-open moment.

In production DEMO_NOW is unset and this is simply datetime.now(UTC).  For the
demo, seed.py writes a stable anchor (the nearest weekday at 9 AM local) into
.env so the board looks alive no matter what hour the reviewer opens it — and,
critically, so the same anchor produces the same Match Scores every time.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .config import get_settings


def default_now() -> datetime:
    s = get_settings()
    if s.demo_now:
        dt = datetime.fromisoformat(s.demo_now)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return datetime.now(timezone.utc)

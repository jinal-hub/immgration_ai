"""Stop hitting a host after it has refused us.

A refusal is an HTTP 403 or 429 — the host telling us *don't*, as opposed to a
timeout, DNS failure or 5xx, which are ordinary faults that say nothing about
our standing. When a host refuses, every source on that host is paused for
``FETCH_COOLDOWN_HOURS`` (default 0.25 / 15 minutes) and the sync layer skips them until the
pause expires.

Deliberate non-goals — see DESIGN.md. A refusal is never answered by changing
*how* we fetch: the IP address, the User-Agent and the choice between plain
HTTP and the browser stay exactly what they were. There is no override flag to
force a paused host either, because a forced request is still a request the
host asked us not to make.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import HostCooldown
from fetch.politeness import host_of
from logging_config import get_logger

logger = get_logger(__name__)

DEFAULT_COOLDOWN_HOURS = 0.25

#: The statuses that mean "refused", not merely "failed".
REFUSAL_HTTP_STATUSES = frozenset({403, 429})


def cooldown_hours() -> float:
    """``FETCH_COOLDOWN_HOURS``, default 0.25 (15 minutes)."""
    raw = (os.environ.get("FETCH_COOLDOWN_HOURS") or "").strip()
    if not raw:
        return DEFAULT_COOLDOWN_HOURS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "FETCH_COOLDOWN_HOURS is not a number; using the default",
            extra={"raw": raw, "default": DEFAULT_COOLDOWN_HOURS},
        )
        return DEFAULT_COOLDOWN_HOURS
    if value < 0:
        logger.warning(
            "FETCH_COOLDOWN_HOURS is negative; using 0 (no pause)",
            extra={"raw": raw},
        )
        return 0.0
    return value


def is_refusal(http_status) -> bool:
    """True when ``http_status`` is a refusal (403/429) rather than a fault."""
    try:
        return int(http_status) in REFUSAL_HTTP_STATUSES
    except (TypeError, ValueError):
        return False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; treat them as UTC like PostgreSQL does."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def active_cooldown(session: Session, host: str,
                    now: datetime | None = None) -> HostCooldown | None:
    """The still-running cooldown row for ``host``, else None.

    An expired row reads as "not cooling" but is left in place: it is harmless,
    and the next success or refusal overwrites it.
    """
    if not host:
        return None
    now = now or _utcnow()
    row = session.get(HostCooldown, host)
    if row is None:
        return None
    until = _as_utc(row.cooldown_until)
    if until is None or until <= now:
        return None
    return row


def blocked_until(session: Session, url: str,
                  now: datetime | None = None) -> datetime | None:
    """When the host behind ``url`` will talk to us again, or None if now."""
    row = active_cooldown(session, host_of(url), now)
    return _as_utc(row.cooldown_until) if row is not None else None


def is_cooling(session: Session, url: str, now: datetime | None = None) -> bool:
    return active_cooldown(session, host_of(url), now) is not None


def blocked_until_map(session: Session, urls: list[str],
                      now: datetime | None = None) -> dict[str, datetime | None]:
    """One query for a whole page of sources: ``{url: cooldown_until | None}``."""
    urls = [u for u in urls if u]
    if not urls:
        return {}
    now = now or _utcnow()
    hosts = {host_of(u) for u in urls}
    rows = session.execute(
        select(HostCooldown).where(HostCooldown.host.in_(hosts))
    ).scalars().all()

    active: dict[str, datetime] = {}
    for row in rows:
        until = _as_utc(row.cooldown_until)
        if until is not None and until > now:
            active[row.host] = until
    return {url: active.get(host_of(url)) for url in urls}


def start_cooldown(session: Session, url: str, http_status: int | None,
                   source_id: str | None = None,
                   now: datetime | None = None) -> datetime:
    """Record that the host behind ``url`` refused us, pausing it.

    Keyed by host: one refusal pauses every source on that host. A repeat
    refusal while still paused extends the window and increments ``hits``; a
    refusal after the window has closed starts a fresh one.
    """
    now = now or _utcnow()
    hours = cooldown_hours()
    until = now + timedelta(hours=hours)
    host = host_of(url)

    row = session.get(HostCooldown, host)
    if row is None:
        row = HostCooldown(
            host=host,
            cooldown_until=until,
            http_status=http_status,
            hits=1,
            source_id=source_id,
        )
        session.add(row)
    else:
        row.hits = 1 if (_as_utc(row.cooldown_until) or now) <= now else (row.hits or 1) + 1
        row.cooldown_until = until
        row.http_status = http_status
        row.source_id = source_id or row.source_id

    session.flush()
    logger.warning(
        "Host refused a fetch — pausing all sources on that host",
        extra={
            "host": host,
            "http_status": http_status,
            "cooldown_hours": hours,
            "cooldown_until": until.isoformat(),
            "hits": row.hits,
            "source_id": source_id,
            # Nothing about *how* we fetch changes in response to a refusal:
            # no new IP, no new User-Agent, no switch of fetch method.
            "response": "pause only",
        },
    )
    return until


def clear_cooldown(session: Session, url: str) -> bool:
    """Drop the host's pause once a fetch from it has succeeded.

    Self-healing for a stale row (one whose window has long expired) and for a
    pause that another source on the same host has since disproved.
    """
    host = host_of(url)
    if not host:
        return False
    row = session.get(HostCooldown, host)
    if row is None:
        return False
    session.delete(row)
    session.flush()
    logger.info("Host reachable again — clearing its cooldown", extra={"host": host})
    return True

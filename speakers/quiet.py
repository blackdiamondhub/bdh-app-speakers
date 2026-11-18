"""Quiet hours: a volume limit for chosen rooms between two times of day.

Configured on the app's Settings page (``quiet_*``) and computed in the hub's
time zone. An admin can lift the limit for the rest of the current window; it
comes back on its own the next time quiet hours start.
"""
import datetime as dt
import logging
import threading
import time

from bdh_sdk.location import tzinfo
from bdh_sdk.plugins import as_list
from bdh_sdk.settings import as_bool
from bdh_sdk.storage import app_state

from .conf import APP_ID, setting

log = logging.getLogger(__name__)

CACHE_TTL = 5
OVERRIDE_KEY = 'quiet_lifted_until'

_lock = threading.Lock()
_cached = None
_cached_at = 0.0


class QuietError(Exception):
    pass


def _hm(value, default):
    try:
        h, m = str(value).split(':')[:2]
        return dt.time(int(h), int(m))
    except (TypeError, ValueError):
        return default


def window(now, start, end):
    """(inside, end_at): whether ``now`` is inside start..end and when it ends.

    The window may cross midnight (22:00-07:00). start == end is an empty window.
    """
    t = now.time()
    if start == end:
        return False, None
    if start < end:
        inside = start <= t < end
        end_day = now.date()
    else:
        inside = t >= start or t < end
        end_day = now.date() + dt.timedelta(days=1) if t >= start else now.date()
    if not inside:
        return False, None
    return True, dt.datetime.combine(end_day, end, tzinfo=now.tzinfo)


def _config():
    rooms = setting('quiet_rooms', None, cast=as_list) or []
    return {
        'enabled': bool(setting('quiet_enabled', False, cast=as_bool)),
        'start': _hm(setting('quiet_start', '22:00'), dt.time(22, 0)),
        'end': _hm(setting('quiet_end', '07:00'), dt.time(7, 0)),
        'limit': max(0, min(100, int(setting('quiet_limit', 25, cast=int) or 0))),
        'rooms': [str(r) for r in rooms],
    }


def compute(now=None):
    """The quiet-hours status, or None when it is off or covers no rooms."""
    cfg = _config()
    if not cfg['enabled'] or not cfg['rooms']:
        return None
    now = now or dt.datetime.now(tzinfo())
    inside, end_at = window(now, cfg['start'], cfg['end'])
    lifted = False
    if inside:
        until = app_state(APP_ID).get(OVERRIDE_KEY)
        lifted = bool(until) and float(until) > now.timestamp()
    return {
        'active': inside and not lifted,
        'lifted': lifted,
        'limit': cfg['limit'],
        'start': cfg['start'].strftime('%H:%M'),
        'end': cfg['end'].strftime('%H:%M'),
        'rooms': cfg['rooms'],
    }


def status(force=False):
    """:func:`compute`, cached for a few seconds (settings are an RPC away)."""
    global _cached, _cached_at
    with _lock:
        if not force and time.monotonic() - _cached_at < CACHE_TTL:
            return _cached
    try:
        found = compute()
    except Exception:  # noqa: BLE001 - a bad setting or the broker being away
        log.warning('quiet hours status failed', exc_info=True)
        found = None
    with _lock:
        _cached, _cached_at = found, time.monotonic()
    return found


def invalidate():
    global _cached_at
    with _lock:
        _cached_at = 0.0


def lift(on, now=None):
    """Lift the limit until the current window ends (on), or put it back (off)."""
    state = app_state(APP_ID)
    if not on:
        state.delete(OVERRIDE_KEY)
        invalidate()
        return
    cfg = _config()
    now = now or dt.datetime.now(tzinfo())
    inside, end_at = window(now, cfg['start'], cfg['end'])
    if not (cfg['enabled'] and inside):
        raise QuietError("Quiet hours aren't on right now")
    state.set(OVERRIDE_KEY, end_at.timestamp())
    invalidate()


def room_choices():
    """Sonos rooms by name for the Settings page, plus saved ones now offline."""
    from .backends import sonos
    names = {z.player_name for z in sonos._discover_all().values()}
    saved = setting('quiet_rooms', None, cast=as_list) or []
    choices = [(n, n) for n in names]
    choices += [(n, f'{n} \u2013 offline') for n in saved if n not in names]
    return sorted(choices, key=lambda c: c[1].lower())


def cap_for(names):
    """The limit if quiet hours are on and every one of these rooms is limited, else None."""
    q = status()
    if not q or not q['active'] or not names:
        return None
    rooms = set(q['rooms'])
    return q['limit'] if all(n in rooms for n in names) else None

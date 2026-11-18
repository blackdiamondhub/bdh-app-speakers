"""Keeps quiet-hours rooms at or below the limit, whoever changes the volume.

Subscribes to each limited room's RenderingControl events so a change from the
Sonos app (or a wall button) is turned back down within a moment, and sweeps
every ``SWEEP_S`` seconds in case an event was missed or a subscription lapsed
(and to notice quiet hours starting). Holds subscriptions only while the limit
is in force.
"""
import logging
import os
import sys
import threading

from . import quiet
from .conf import setting

log = logging.getLogger(__name__)

SWEEP_S = 30

_wake = threading.Event()
_subs = {}            # room name -> soco Subscription
_thread = None


def _is_runtime(path):
    """``python -m bdh_sdk.runtime`` (the app's container) runs ``bdh_sdk/runtime/__main__.py``."""
    parts = path.replace('\\', '/').split('/')
    return parts[-3:] == ['bdh_sdk', 'runtime', '__main__.py']


def should_start(argv=None):
    """Start only in the server process (the app's container, Daphne, or runserver's reloaded child)."""
    if os.environ.get('BDH_SPEAKERS_QUIET_ENFORCER', '').lower() in ('0', 'false', 'off', 'no'):
        return False
    argv = sys.argv if argv is None else argv
    if argv and _is_runtime(argv[0]):
        return True
    prog = os.path.basename(argv[0]) if argv else ''
    if 'daphne' in prog:
        return True
    if len(argv) > 1 and prog in ('manage.py', 'django-admin', '__main__.py') and argv[1] == 'runserver':
        return os.environ.get('RUN_MAIN') == 'true' or '--noreload' in argv
    return False


def wake():
    """Check again now (settings saved, limit restored)."""
    _wake.set()


def start():
    global _thread
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_run, name='quiet-hours', daemon=True)
    _thread.start()


def _run():
    while True:
        try:
            sweep()
        except Exception:  # noqa: BLE001 - keep enforcing next time
            log.warning('quiet hours sweep failed', exc_info=True)
        _wake.wait(SWEEP_S)
        _wake.clear()


def _limit_zone(zone, limit):
    """Turn ``zone`` down to ``limit`` if it is above it."""
    try:
        if zone.volume > limit:
            log.info('quiet hours: %s turned down to %s%%', zone.player_name, limit)
            zone.volume = limit
    except Exception:  # noqa: BLE001 - speaker unreachable
        log.info('quiet hours: could not limit %s', zone.player_name, exc_info=True)


def _on_event(name, zone):
    def callback(event):
        try:
            vol = int(event.variables.get('volume', {}).get('Master'))
        except (TypeError, ValueError, AttributeError):
            return
        q = quiet.status()
        if q and q['active'] and name in q['rooms'] and vol > q['limit']:
            from .backends import sonos
            sonos._pool.submit(_limit_zone, zone, q['limit'])
    return callback


def _unsubscribe(name):
    sub = _subs.pop(name, None)
    if sub is None:
        return
    try:
        sub.unsubscribe()
    except Exception:  # noqa: BLE001 - already gone
        log.debug('unsubscribe failed for %s', name, exc_info=True)


def _subscribe(name, zone):
    sub = _subs.get(name)
    if sub is not None and sub.is_subscribed and sub.service.soco is zone:
        return
    _unsubscribe(name)
    import soco
    interface = setting('interface', '')
    if interface:
        soco.config.EVENT_LISTENER_IP = interface
    try:
        sub = zone.renderingControl.subscribe(auto_renew=True)
    except Exception:  # noqa: BLE001 - the sweep still covers this room
        log.info('quiet hours: could not subscribe to %s', name, exc_info=True)
        return
    sub.callback = _on_event(name, zone)
    _subs[name] = sub


def sweep():
    q = quiet.status(force=True)
    rooms = set(q['rooms']) if q and q['active'] else set()
    for name in list(_subs):
        if name not in rooms:
            _unsubscribe(name)
    if not rooms:
        return
    from .backends import sonos
    by_name = {z.player_name: z for z in sonos._discover_all().values()}
    for name in rooms:
        zone = by_name.get(name)
        if zone is None:
            _unsubscribe(name)
            continue
        _subscribe(name, zone)
        _limit_zone(zone, q['limit'])

"""Settings, room layout and quick groups shared by every backend.

Settings live under the ``speakers`` app id (``BDH_SPEAKERS_<KEY>``). Until a
site has moved them, the old ``sonos_control`` names are read as a fallback
(Settings page values, ``BDH_SONOS_CONTROL_<KEY>``, ``[apps.sonos_control]``).
"""
import json
import logging

from bdh_sdk.settings import app_setting

log = logging.getLogger(__name__)

APP_ID = 'speakers'
LEGACY_APP_ID = 'sonos_control'

# Rooms and quick groups come from the site's settings (see README):
#   rooms:   {room name: {label, floor, icon}}; floor is upper / main / lower
#            (inside the house diagram) or outside_main / outside_lower. With no
#            floors set, the room picker shows a plain grid instead.
#   presets: [{id, label, icon, rooms: [room names] or "*"}]
DEFAULT_PRESETS = [{'id': 'all', 'label': 'Everywhere', 'icon': 'fa-house', 'rooms': '*'}]

FLOOR_ORDER = ['upper', 'main', 'outside_main', 'lower', 'outside_lower', 'other']

_MISSING = object()


def setting(key, default=None, *, legacy=(), cast=None):
    """``app_setting('speakers', key)``, then the old ``sonos_control`` names."""
    value = app_setting(APP_ID, key, _MISSING, cast=cast)
    if value is _MISSING:
        try:
            value = app_setting(LEGACY_APP_ID, key, _MISSING, legacy=legacy, cast=cast)
        except Exception:  # noqa: BLE001 - e.g. the broker refuses another app's settings
            log.debug('legacy setting %s unavailable', key, exc_info=True)
            value = _MISSING
    return default if value is _MISSING else value


def _structured(key, default):
    """A dict/list setting; environment values are JSON."""
    value = setting(key, default)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            log.warning('speakers %s is not valid JSON; using the default', key)
            return default
    return value


def room_layout():
    return _structured('rooms', {})


def presets():
    return _structured('presets', DEFAULT_PRESETS)


def _meta(name):
    m = room_layout().get(name) or {}
    return {'label': m.get('label') or name, 'floor': m.get('floor') or 'other',
            'icon': m.get('icon') or 'fa-volume-high'}


def room_rank(rooms):
    """Sort rooms in place (shown first, then by floor and label); {uid: rank}."""
    order = {f: i for i, f in enumerate(FLOOR_ORDER)}
    rooms.sort(key=lambda r: (not r['shown'], order.get(r['floor'], 99), r['label']))
    return {r['uid']: i for i, r in enumerate(rooms)}

"""One view of every enabled speaker system.

Ids handed to the browser are ``<backend key>:<local id>``; ids without a known
prefix are Sonos uids (saved before 1.0). Every browser polls the same cached
snapshot, and a backend that fails is logged and left out instead of taking
the others down.
"""
import logging
import threading
import time

from bdh_sdk.plugins import as_list

from . import backends
from .backends import SpeakerError, split_id
from .conf import presets, room_rank, setting

log = logging.getLogger(__name__)

SNAPSHOT_TTL = 1.5
DEFAULT_BACKENDS = ('sonos', 'ha')

_lock = threading.Lock()
_backends = {}
_snapshot = None
_snapshot_at = 0.0


def enabled():
    """Keys of the backends switched on in the ``backends`` setting."""
    keys = setting('backends', None, cast=as_list)
    if keys is None:
        keys = DEFAULT_BACKENDS
    return [k for k in backends.KEYS if k in keys]


def backend(key):
    with _lock:
        if key not in _backends:
            _backends[key] = backends.get(key)
        return _backends[key]


def active():
    return [backend(k) for k in enabled()]


def _resolve(full_id):
    """``(backend, local id)`` for an id from the browser."""
    key, local = split_id(full_id)
    if key not in enabled():
        raise SpeakerError('Unknown room')
    return backend(key), local


def _pre(key, local):
    return f'{key}:{local}'


def _norm(name):
    return str(name or '').strip().lower()


def _sonos_names():
    if 'sonos' not in enabled():
        return set()
    try:
        return {_norm(n) for n in backend('sonos').names()}
    except Exception:  # noqa: BLE001
        log.warning('Sonos room names unavailable for de-duplication', exc_info=True)
        return set()


def _build():
    rooms, groups, extra, ok, errors = [], [], {}, 0, []
    dupes = None
    for b in active():
        try:
            snap = b.snapshot()
            extra.update(b.extra())
        except Exception as exc:  # noqa: BLE001
            log.warning('%s snapshot failed', b.label, exc_info=True)
            errors.append(exc)
            continue
        ok += 1
        b_rooms = [{**r, 'uid': _pre(b.key, r['uid']), 'group': _pre(b.key, r['group']),
                    'backend': b.key} for r in snap['rooms']]
        b_groups = [{**g, 'id': _pre(b.key, g['id']),
                     'members': [_pre(b.key, m) for m in g['members']],
                     'backend': b.key, 'can_group': 'grouping' in b.supports}
                    for g in snap['groups']]
        if b.key != 'sonos':
            # HA's Sonos integration would list every Sonos room a second time.
            if dupes is None:
                dupes = _sonos_names()
            hidden = {r['uid'] for r in b_rooms if _norm(r['name']) in dupes}
            if hidden:
                b_rooms = [r for r in b_rooms if r['uid'] not in hidden]
                b_groups = _drop_members(b_groups, b_rooms, hidden)
        rooms += b_rooms
        groups += b_groups
    if not ok and errors:
        raise errors[0]
    rank = room_rank(rooms)
    by_uid = {r['uid']: r for r in rooms}
    for g in groups:
        g['members'] = sorted(g['members'], key=lambda u: rank[u])
        g['can_group'] = g['can_group'] and any(by_uid[m].get('can_group', True) for m in g['members'])
    groups.sort(key=lambda g: rank[g['members'][0]])
    return {'rooms': rooms, 'groups': groups, 'presets': presets(),
            'night': extra.get('night'), 'ts': time.time()}


def _drop_members(groups, rooms, hidden):
    group_of = {r['uid']: r['group'] for r in rooms}
    out = []
    for g in groups:
        members = [m for m in g['members'] if m not in hidden]
        if not members:
            continue
        if g['id'] in hidden:
            # The leader is hidden; the group is named after its first remaining room.
            new_id = members[0]
            for m in members:
                if group_of.get(m) == g['id']:
                    for r in rooms:
                        if r['uid'] == m:
                            r['group'] = new_id
            g = {**g, 'id': new_id}
        out.append({**g, 'members': members})
    return out


def snapshot(force=False):
    global _snapshot, _snapshot_at
    with _lock:
        fresh = _snapshot and time.monotonic() - _snapshot_at < SNAPSHOT_TTL
        if fresh and not force:
            return _snapshot
    snap = _build()
    with _lock:
        _snapshot, _snapshot_at = snap, time.monotonic()
    return snap


def invalidate():
    global _snapshot_at
    with _lock:
        _snapshot_at = 0.0
    for b in active():
        try:
            b.invalidate()
        except Exception:  # noqa: BLE001
            log.debug('%s invalidate failed', b.label, exc_info=True)


def reset():
    """Forget every cache and backend instance (tests, settings changes)."""
    global _snapshot, _snapshot_at
    with _lock:
        _backends.clear()
        _snapshot, _snapshot_at = None, 0.0


# ---------------------------------------------------------------- settings

def speaker_choices():
    """Every room of every enabled system for the Settings page.

    Sonos values stay bare uids so lists saved before 1.0 keep working.
    """
    out = []
    dupes = None
    for b in active():
        try:
            choices = b.choices()
        except Exception:  # noqa: BLE001
            log.warning('%s choices failed', b.label, exc_info=True)
            continue
        if b.key == 'sonos':
            out += [(uid, f'{label} \u00b7 Sonos') for uid, label in choices]
            continue
        if dupes is None:
            dupes = _sonos_names()
        for local, label in choices:
            name = label.rsplit(' \u00b7 ', 1)[0]
            if _norm(name) not in dupes:
                out.append((_pre(b.key, local), label))
    return sorted(out, key=lambda c: c[1].lower())


# ---------------------------------------------------------------- actions

def transport(group, op):
    b, local = _resolve(group)
    b.transport(local, op)


def play_item(group, item):
    b, local = _resolve(group)
    b.play_item(local, item)


def set_group_volume(group, volume):
    b, local = _resolve(group)
    b.set_group_volume(local, volume)


def set_room_volume(room, volume):
    b, local = _resolve(room)
    b.set_room_volume(local, volume)


def set_room_mute(room, muted):
    b, local = _resolve(room)
    b.set_room_mute(local, muted)


def set_rooms(room, rooms):
    """Make the group ``room`` belongs to play exactly ``rooms`` (one system only)."""
    b, local = _resolve(room)
    locals_ = []
    for r in rooms or []:
        key, rest = split_id(r)
        if key != b.key:
            raise SpeakerError('Rooms from different systems can\u2019t be grouped')
        locals_.append(rest)
    result = b.set_members(local, locals_)
    return _pre(b.key, result) if isinstance(result, str) else result


def set_group(names):
    """Quick group: group these rooms (all when '*') within each system."""
    result, matched, failure = None, False, None
    for b in active():
        if 'grouping' not in b.supports:
            continue
        try:
            r = b.set_group(names)
        except Exception as exc:  # noqa: BLE001
            if not (isinstance(exc, SpeakerError) and str(exc) == 'No matching rooms'):
                log.warning('%s quick group failed', b.label, exc_info=True)
                failure = failure or exc
            continue
        matched = True
        if result is None and isinstance(r, str):
            result = _pre(b.key, r)
    if not matched:
        raise failure or SpeakerError('No matching rooms')
    return result


def ungroup(group):
    b, local = _resolve(group)
    b.ungroup(local)


def _each(method):
    for b in active():
        try:
            getattr(b, method)()
        except Exception:  # noqa: BLE001
            log.warning('%s %s failed', b.label, method, exc_info=True)


def ungroup_all():
    _each('ungroup_all')


def pause_all():
    _each('pause_all')


def art(room):
    """``(bytes, content type)`` of a room's album art, or None."""
    try:
        b, local = _resolve(room)
    except SpeakerError:
        return None
    return b.art(local)

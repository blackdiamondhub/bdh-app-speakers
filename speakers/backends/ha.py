"""Home Assistant backend: ``media_player`` entities on every HA instance.

Local room ids are ``<instance id>:<entity_id>`` (so ``ha:<instance>:<entity>``
once :mod:`speakers.service` prefixes them). Players that support grouping
(``group_members``, HA's join/unjoin services) can play together; others are
always on their own. Album art is fetched server-side through
``bdh_sdk.ha.get_static`` so the browser never sees the HA address.
"""
import hashlib
import logging
import threading
import time
from collections import OrderedDict
from urllib.parse import quote

from bdh_sdk import ha
from bdh_sdk.plugins import as_list

from ..conf import _meta, setting
from .base import Backend, SpeakerError, split_id

log = logging.getLogger(__name__)

STATES_TTL = 1.5
ART_TTL = 300
ART_MAX = 32

# MediaPlayerEntityFeature bits.
PAUSE = 1
VOLUME_SET = 4
VOLUME_MUTE = 8
PREVIOUS_TRACK = 16
NEXT_TRACK = 32
PLAY = 16384
GROUPING = 524288

STATE_MAP = {'playing': 'PLAYING', 'paused': 'PAUSED_PLAYBACK', 'buffering': 'TRANSITIONING'}
OFFLINE = {'unavailable', 'unknown'}
INPUTS = {'tv', 'tv audio', 'line-in', 'line in', 'linein', 'aux', 'analog', 'optical',
          'hdmi', 'hdmi arc', 'hdmi earc', 'arc', 'earc', 'coaxial', 'phono'}
OPS = {'play': ('media_play', PLAY), 'pause': ('media_pause', PAUSE),
       'next': ('media_next_track', NEXT_TRACK), 'previous': ('media_previous_track', PREVIOUS_TRACK)}

_lock = threading.Lock()
_states = {}       # instance id -> (monotonic time, [media_player states])
_art = OrderedDict()  # (instance, path) -> (monotonic time, bytes, content type)


def local_id(instance, entity_id):
    return f'{instance}:{entity_id}'


def parse_id(local):
    """``'lodge:media_player.x'`` -> ``('lodge', 'media_player.x')``."""
    instance, sep, entity = str(local).rpartition(':')
    if not sep or not instance or not entity.startswith('media_player.'):
        raise SpeakerError('Unknown room')
    return instance, entity


def picked():
    """Local ids of HA players ticked on the Settings page, or None if never picked."""
    values = setting('speakers', None, cast=as_list)
    if values is None:
        return None
    out = set()
    for value in values:
        key, rest = split_id(value)
        if key == 'ha':
            out.add(rest)
    return out


def _features(st):
    try:
        return int((st.get('attributes') or {}).get('supported_features') or 0)
    except (TypeError, ValueError):
        return 0


def _source(attrs):
    name = attrs.get('app_name') or attrs.get('source') or ''
    if not name:
        return None
    low = name.lower()
    if 'spotify' in low:
        return {'name': name, 'icon': 'fa-spotify', 'brand': True}
    if 'airplay' in low or 'apple' in low:
        return {'name': name, 'icon': 'fa-apple', 'brand': True}
    if 'youtube' in low:
        return {'name': name, 'icon': 'fa-youtube', 'brand': True}
    if _is_input(name):
        icon = 'fa-tv' if ('tv' in low or 'hdmi' in low or 'arc' in low) else 'fa-plug'
        return {'name': name, 'icon': icon, 'brand': False}
    if 'radio' in low or 'tunein' in low:
        return {'name': name, 'icon': 'fa-radio', 'brand': False}
    return {'name': name, 'icon': 'fa-music', 'brand': False}


def _is_input(source):
    low = (source or '').strip().lower()
    return low in INPUTS or low.startswith('hdmi')


def _safe_path(path):
    """True for a path on the HA host (no scheme, host, traversal or backslash)."""
    if not isinstance(path, str) or not path.startswith('/') or path.startswith('//'):
        return False
    head = path.split('?', 1)[0]
    return '\\' not in path and ':' not in head and '..' not in head.split('/') and \
        all(ord(c) > 31 for c in path)


def _content_type(data):
    if data[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if data[:4] == b'GIF8':
        return 'image/gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    # Anything else (SVG included: it can carry script) is not served.
    return 'application/octet-stream'


class HomeAssistantBackend(Backend):
    key = 'ha'
    label = 'Home Assistant'
    supports = frozenset({'grouping'})

    # ---------------------------------------------------------- state

    def instances(self):
        try:
            return list(ha.instances())
        except Exception as exc:  # noqa: BLE001
            log.warning('Home Assistant instances unavailable: %s', exc)
            return []

    def _players(self, instance, force=False):
        """media_player states of one instance (cached), [] when unreachable."""
        with _lock:
            hit = _states.get(instance)
            if hit and not force and time.monotonic() - hit[0] < STATES_TTL:
                return hit[1]
        try:
            states = ha.get_all_states(instance=instance) or []
        except Exception as exc:  # noqa: BLE001
            log.warning('Home Assistant %s states failed: %s', instance, exc)
            states = None
        if states is None:
            with _lock:
                return _states.get(instance, (0, []))[1]
        players = [s for s in states if str(s.get('entity_id', '')).startswith('media_player.')]
        with _lock:
            _states[instance] = (time.monotonic(), players)
        return players

    def _all(self, force=False):
        """[(instance dict, {entity_id: state})] for every instance."""
        out = []
        for inst in self.instances():
            players = self._players(inst['id'], force)
            out.append((inst, {s['entity_id']: s for s in players}))
        return out

    def invalidate(self):
        with _lock:
            _states.clear()

    def _origin(self, inst):
        return f"{self.label} ({inst.get('name') or inst['id']})"

    def _art_url(self, local, picture):
        if not picture:
            return ''
        if picture.startswith(('http://', 'https://')):
            return picture
        if not _safe_path(picture):
            return ''
        from django.urls import reverse
        version = hashlib.sha1(picture.encode()).hexdigest()[:12]
        return f"{reverse('speakers_art')}?room={quote('ha:' + local, safe='')}&v={version}"

    def snapshot(self, force=False):
        wanted = picked()
        rooms, groups = [], []
        for inst, players in self._all(force):
            iid = inst['id']

            def lid(eid, iid=iid):
                return local_id(iid, eid)

            shown = {eid for eid in players if wanted is None or lid(eid) in wanted}
            leader_of = {}
            for eid in shown:
                st = players[eid]
                attrs = st.get('attributes') or {}
                members = [m for m in as_list(attrs.get('group_members') or []) if m in players]
                if _features(st) & GROUPING and len(members) > 1 and eid in members:
                    leader = members[0]
                    for m in members:
                        if _features(players[m]) & GROUPING:
                            leader_of.setdefault(m, leader)
                leader_of.setdefault(eid, eid)
            # A leader always leads itself, even if listed by a member only.
            for leader in set(leader_of.values()):
                leader_of[leader] = leader

            members_of = {}
            for eid, leader in leader_of.items():
                members_of.setdefault(leader, []).append(eid)

            for eid, leader in leader_of.items():
                st = players[eid]
                attrs = st.get('attributes') or {}
                name = attrs.get('friendly_name') or eid
                meta = _meta(name)
                state = st.get('state', '')
                vol = attrs.get('volume_level')
                feats = _features(st)
                rooms.append({
                    'uid': lid(eid), 'name': name, 'label': meta['label'],
                    'floor': meta['floor'], 'icon': meta['icon'], 'group': lid(leader),
                    'shown': eid in shown,
                    'volume': round(float(vol) * 100) if isinstance(vol, (int, float)) else 0,
                    'muted': bool(attrs.get('is_volume_muted')),
                    'online': state not in OFFLINE,
                    'can_group': bool(feats & GROUPING),
                    'can_volume': bool(feats & VOLUME_SET),
                    'origin': self._origin(inst),
                })

            vols = {r['uid']: r['volume'] for r in rooms}
            for leader, members in members_of.items():
                st = players[leader]
                attrs = st.get('attributes') or {}
                feats = _features(st)
                source = _source(attrs)
                tv = _is_input(attrs.get('source')) or _is_input(attrs.get('app_name'))
                mvols = [vols[lid(m)] for m in members if lid(m) in vols]
                groups.append({
                    'id': lid(leader), 'members': [lid(m) for m in members],
                    'state': STATE_MAP.get(st.get('state', ''), 'STOPPED'),
                    'title': attrs.get('media_title') or ('TV audio' if tv and st.get('state') == 'playing' else ''),
                    'artist': attrs.get('media_artist') or '',
                    'album': attrs.get('media_album_name') or '',
                    'art': self._art_url(lid(leader), attrs.get('entity_picture') or ''),
                    'source': source,
                    'volume': round(sum(mvols) / len(mvols)) if mvols else 0,
                    'tv': tv,
                    'can_pause': bool(feats & PAUSE) and not tv,
                })
        return {'rooms': rooms, 'groups': groups}

    def choices(self):
        wanted = picked() or set()
        out, seen = [], set()
        for inst, players in self._all():
            for eid, st in players.items():
                name = (st.get('attributes') or {}).get('friendly_name') or eid
                local = local_id(inst['id'], eid)
                seen.add(local)
                out.append((local, f'{name} \u00b7 {self._origin(inst)}'))
        for local in sorted(wanted - seen):
            out.append((local, f'{local} \u00b7 {self.label} \u2013 offline'))
        return out

    def names(self):
        return {(st.get('attributes') or {}).get('friendly_name') or eid
                for _, players in self._all() for eid, st in players.items()}

    # ---------------------------------------------------------- actions

    def _state(self, local):
        instance, eid = parse_id(local)
        st = {s['entity_id']: s for s in self._players(instance)}.get(eid)
        if st is None:
            raise SpeakerError('Unknown room')
        return instance, eid, st

    def _call(self, service, instance, eid, extra=None):
        ok, err = ha.call_service('media_player', service, eid, extra, instance=instance)
        if not ok:
            log.warning('media_player.%s on %s/%s failed: %s', service, instance, eid, err)
            raise SpeakerError('Home Assistant didn\u2019t accept that')

    def _members(self, instance, eid, st):
        attrs = st.get('attributes') or {}
        members = as_list(attrs.get('group_members') or [])
        if _features(st) & GROUPING and eid in members and len(members) > 1:
            return members
        return [eid]

    def transport(self, group, op):
        if op not in OPS:
            raise SpeakerError('Unknown transport op')
        instance, eid, st = self._state(group)
        service, bit = OPS[op]
        if op in ('play', 'pause') and _is_input((st.get('attributes') or {}).get('source')):
            raise SpeakerError('This speaker is playing the TV')
        if not _features(st) & bit:
            raise SpeakerError('This speaker can\u2019t do that')
        self._call(service, instance, eid)

    def set_room_volume(self, room, volume):
        instance, eid, st = self._state(room)
        if not _features(st) & VOLUME_SET:
            raise SpeakerError('This speaker\u2019s volume can\u2019t be set')
        level = max(0, min(100, int(volume))) / 100
        self._call('volume_set', instance, eid, {'volume_level': level})

    def set_group_volume(self, group, volume):
        instance, eid, st = self._state(group)
        players = {s['entity_id']: s for s in self._players(instance)}
        level = max(0, min(100, int(volume))) / 100
        done = False
        for m in self._members(instance, eid, st):
            if m in players and _features(players[m]) & VOLUME_SET:
                self._call('volume_set', instance, m, {'volume_level': level})
                done = True
        if not done:
            raise SpeakerError('This speaker\u2019s volume can\u2019t be set')

    def set_room_mute(self, room, muted):
        instance, eid, st = self._state(room)
        if not _features(st) & VOLUME_MUTE:
            raise SpeakerError('This speaker can\u2019t be muted')
        self._call('volume_mute', instance, eid, {'is_volume_muted': bool(muted)})

    def pause_all(self):
        snap = self.snapshot()
        shown = {r['uid'] for r in snap['rooms'] if r['shown']}
        for g in snap['groups']:
            if g['state'] != 'PLAYING' or not g['can_pause'] or not shown & set(g['members']):
                continue
            instance, eid = parse_id(g['id'])
            try:
                self._call('media_pause', instance, eid)
            except SpeakerError:
                pass
        self.invalidate()

    def set_members(self, group, rooms):
        instance, eid, st = self._state(group)
        players = {s['entity_id']: s for s in self._players(instance)}
        wanted = []
        for r in dict.fromkeys(rooms or []):
            inst, e = parse_id(r)
            if inst != instance:
                raise SpeakerError('Rooms from different systems can\u2019t be grouped')
            if e in players:
                wanted.append(e)
        if not wanted:
            raise SpeakerError('Pick at least one room')
        current = self._members(instance, eid, st)
        if len(wanted) == 1 and wanted == current:
            return local_id(instance, wanted[0])
        if any(not _features(players[e]) & GROUPING for e in set(wanted) | set(current)):
            raise SpeakerError('These speakers can\u2019t be grouped')
        leader = current[0]
        anchor = leader if leader in wanted else next((e for e in current if e in wanted), wanted[0])
        if anchor != leader:
            for e in current:
                self._call('unjoin', instance, e)
        else:
            for e in current:
                if e not in wanted:
                    self._call('unjoin', instance, e)
        others = [e for e in wanted if e != anchor]
        if anchor not in current and len(self._members(instance, anchor, players[anchor])) > 1:
            self._call('unjoin', instance, anchor)
        if others:
            self._call('join', instance, anchor, {'group_members': others})
        self.invalidate()
        return local_id(instance, anchor)

    def set_group(self, names):
        snap = self.snapshot()
        by_instance = {}
        for r in snap['rooms']:
            if r['shown'] and r['can_group'] and (names == '*' or r['name'] in names):
                by_instance.setdefault(parse_id(r['uid'])[0], []).append(r['uid'])
        if not by_instance:
            raise SpeakerError('No matching rooms')
        playing = {m for g in snap['groups'] if g['state'] == 'PLAYING' for m in g['members']}
        result = None
        for targets in by_instance.values():
            lead = next((u for u in targets if u in playing), targets[0])
            result = self.set_members(lead, [lead] + [u for u in targets if u != lead])
        return result

    def ungroup(self, group):
        instance, eid, st = self._state(group)
        members = self._members(instance, eid, st)
        for e in members[1:]:
            self._call('unjoin', instance, e)
        self.invalidate()

    def ungroup_all(self):
        for g in self.snapshot()['groups']:
            if len(g['members']) > 1:
                try:
                    self.ungroup(g['id'])
                except SpeakerError:
                    pass

    # ---------------------------------------------------------- art

    def art(self, room):
        instance, eid, st = self._state(room)
        path = (st.get('attributes') or {}).get('entity_picture') or ''
        if not _safe_path(path):
            return None
        key = (instance, path)
        with _lock:
            hit = _art.get(key)
            if hit and time.monotonic() - hit[0] < ART_TTL:
                _art.move_to_end(key)
                return hit[1], hit[2]
        try:
            data = ha.get_static(path, instance=instance)
        except Exception as exc:  # noqa: BLE001
            log.info('album art %s failed: %s', eid, exc)
            return None
        if not data:
            return None
        ctype = _content_type(data)
        if not ctype.startswith('image/'):
            return None
        with _lock:
            _art[key] = (time.monotonic(), data, ctype)
            while len(_art) > ART_MAX:
                _art.popitem(last=False)
        return data, ctype


def clear_caches():
    with _lock:
        _states.clear()
        _art.clear()

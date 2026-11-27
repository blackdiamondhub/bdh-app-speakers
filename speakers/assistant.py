"""Assistant tools: what's playing, put music on, pause/skip, volume and grouping.

They mirror the Speakers page, which every mode (guest, dashboard, owner)
can use, and act only on the rooms ticked under "Speakers to show". Rooms are
named loosely (see :mod:`speakers.matching`). Outside owner mode the
assistant never sets a room louder than ``assistant_max_volume``, and quiet
hours still apply on top.
"""
import logging
import time

from bdh_sdk import ALL_MODES
from bdh_sdk.plugins import OWNER
from bdh_sdk.tools import Tool, ToolError

from . import homepage, music, quiet, service
from .backends import SpeakerError, split_id
from .conf import setting
from .matching import Ambiguous, NoMatch, Rooms, join

log = logging.getLogger(__name__)

SPEAKER_STATUS = 'speaker_status'
PLAY_MUSIC = 'play_music'
MEDIA_CONTROL = 'media_control'
SET_VOLUME = 'set_volume'
GROUP_SPEAKERS = 'group_speakers'

PLAYING = frozenset({'PLAYING', 'TRANSITIONING'})
CONTROL_ACTIONS = ('pause', 'resume', 'stop', 'next', 'previous')
OPS = {'pause': 'pause', 'stop': 'pause', 'resume': 'play', 'next': 'next', 'previous': 'previous'}
DEFAULT_MAX_VOLUME = 60
DEFAULT_MUSIC = 'Smooth Jazz'
STEP = 10
# Grouping changes take a moment to show up before playing on the new group.
SETTLE_SECONDS = 0.6

DOWN = "The speakers aren't answering right now. Try again in a minute."
FAILED = "The speakers didn't respond."
NO_LEVEL = "Say a volume from 0 to 100, or up or down."


def max_volume():
    try:
        value = int(setting('assistant_max_volume', DEFAULT_MAX_VOLUME, cast=int))
    except (TypeError, ValueError):
        value = DEFAULT_MAX_VOLUME
    return max(0, min(100, value))


def _snap(fresh=True):
    try:
        if fresh:
            service.invalidate()
        return service.snapshot(force=fresh)
    except Exception:  # noqa: BLE001
        log.warning('speakers snapshot for the assistant failed', exc_info=True)
        raise ToolError(DOWN) from None


def _shown(snap):
    return [r for r in snap['rooms'] if r.get('shown')]


def _catalog(snap):
    rooms = [{'uid': r['uid'], 'label': r['label'], 'name': r.get('name') or r['label'],
              'floor': r.get('floor')} for r in _shown(snap)]
    return Rooms(rooms, snap.get('presets') or ())


def _targets(snap, text, strict=True):
    try:
        found = _catalog(snap).resolve(text, strict=strict)
    except (NoMatch, Ambiguous) as exc:
        raise ToolError(str(exc)) from None
    by_uid = {r['uid']: r for r in snap['rooms']}
    return [by_uid[r['uid']] for r in found]


def _labels(snap, uids):
    by_uid = {r['uid']: r['label'] for r in snap['rooms']}
    return [by_uid.get(u, u) for u in uids]


def _the(labels):
    return 'the ' + join(labels)


def _cap(text):
    return text[:1].upper() + text[1:]


def _verb(labels, one, many):
    return many if len(labels) > 1 else one


def _groups(snap):
    return {g['id']: g for g in snap['groups']}


def _playing_music(snap):
    return [g for g in snap['groups'] if g['state'] in PLAYING and not g.get('tv')]


def _what(g):
    return ' by '.join(x for x in (g.get('title'), g.get('artist')) if x)


def _rooms_param(required, extra=''):
    return {'type': 'string',
            'description': "Which rooms, in the guest's words: a room, several separated by commas, "
                           "a floor (upstairs, main floor, downstairs, outside) or 'everywhere'. Map "
                           "'here' or 'this room' to the room this screen is in; if it could mean "
                           "different rooms, ask first."
                           + ('' if required else ' Leave it out for the music that is playing.') + extra}


def _room_names():
    try:
        return [r['label'] for r in _shown(service.snapshot())]
    except Exception:  # noqa: BLE001 - still offer the tools; running them says what's wrong
        return []


def tools(mode=None):
    names = _room_names()
    extra = f" The rooms are: {', '.join(names)}." if names else ''
    return [
        Tool(
            id=SPEAKER_STATUS,
            title="What's playing",
            description="What's playing on the lodge's speakers, in which rooms, and each room's "
                        "volume. Use it to answer what song or station is on, whether music is "
                        "playing somewhere, and to learn the room names.",
            params={'type': 'object', 'properties': {'speakers': _rooms_param(False, extra)}},
            modes=ALL_MODES,
            readonly=True,
        ),
        Tool(
            id=PLAY_MUSIC,
            title='Play music',
            description="Put music on in rooms: a genre, mood, artist or radio station (played from "
                        "Sonos favorites or TuneIn radio). Leave what out to resume what was there, "
                        "or for some music. Several rooms play together. Not for the guest's own "
                        "phone music (they cast that).",
            params={'type': 'object', 'properties': {
                'speakers': _rooms_param(True, extra),
                'what': {'type': 'string',
                         'description': "What to play, e.g. 'jazz', 'classical', 'country radio', "
                                        "'CBC Radio One'. Leave it out to resume or for any music."},
            }, 'required': ['speakers']},
            modes=ALL_MODES,
        ),
        Tool(
            id=MEDIA_CONTROL,
            title='Pause, resume or skip',
            description="Pause, resume, stop, skip to the next track or go back on the speakers. "
                        "Without speakers it pauses or stops everything that's playing, resumes "
                        "what was paused, or skips the one room playing.",
            params={'type': 'object', 'properties': {
                'action': {'type': 'string', 'enum': list(CONTROL_ACTIONS),
                           'description': 'pause, resume, stop, next (skip) or previous'},
                'speakers': _rooms_param(False, extra),
            }, 'required': ['action']},
            modes=ALL_MODES,
        ),
        Tool(
            id=SET_VOLUME,
            title='Set volume',
            description="Set the speakers' volume to a level (0-100) or turn it up or down. "
                        "Without speakers it changes the one room playing. 'A little' is a step "
                        "of 5, 'a lot' 20. Mute is level 0.",
            params={'type': 'object', 'properties': {
                'level': {'type': 'integer', 'minimum': 0, 'maximum': 100,
                          'description': 'The volume to set, 0 to 100'},
                'change': {'type': 'string', 'enum': ['up', 'down'],
                           'description': 'Turn it up or down from where it is, instead of level'},
                'by': {'type': 'integer', 'minimum': 1, 'maximum': 50,
                       'description': f'How much to turn it up or down (default {STEP})'},
                'speakers': _rooms_param(False, extra),
            }},
            modes=ALL_MODES,
        ),
        Tool(
            id=GROUP_SPEAKERS,
            title='Group speakers',
            description="Make rooms play the same music together (join: the others pick up what's "
                        "playing in one of them), or take rooms out of their group so they play on "
                        "their own (separate).",
            params={'type': 'object', 'properties': {
                'action': {'type': 'string', 'enum': ['join', 'separate'],
                           'description': 'join to play together, separate to split them up'},
                'speakers': _rooms_param(True, extra),
            }, 'required': ['action', 'speakers']},
            modes=ALL_MODES,
        ),
    ]


# ---------------------------------------------------------------- status

def _line(g, labels, detail):
    who = ' + '.join(labels)
    vol = f"{g.get('volume') or 0}%"
    if g['state'] in PLAYING:
        if g.get('tv'):
            return f'{who}: TV audio, {vol}'
        text = f"{who}: playing {_what(g) or 'music'}"
        src = (g.get('source') or {}).get('name')
        if src and src != 'Sonos':
            text += f' ({src})'
        return f'{text}, {vol}'
    if detail and g['state'] == 'PAUSED_PLAYBACK' and _what(g) and not g.get('tv'):
        return f'{who}: paused ({_what(g)}), {vol}'
    return f'{who}: off, {vol}' if detail else f'{who}: off'


def _quiet_note():
    q = quiet.status()
    if q and q['active']:
        return f" Quiet hours: {join(q['rooms'])} stay at or below {q['limit']}% until {q['end']}."
    return ''


def _status(args):
    snap = _snap()
    groups = snap['groups']
    by_uid = {r['uid']: r for r in snap['rooms']}
    if args.get('speakers'):
        wanted = {r['uid'] for r in _targets(snap, args['speakers'], strict=False)}
        lines = []
        for g in groups:
            if not wanted & set(g['members']):
                continue
            line = _line(g, _labels(snap, g['members']), True)
            muted = [by_uid[m]['label'] for m in g['members'] if by_uid.get(m, {}).get('muted')]
            offline = [by_uid[m]['label'] for m in g['members'] if not by_uid.get(m, {}).get('online', True)]
            if muted:
                line += f" ({join(muted)} muted)"
            if offline:
                line += f" ({join(offline)} offline)"
            lines.append(line)
        return '. '.join(lines) + '.' + _quiet_note()
    playing = [g for g in groups if g['state'] in PLAYING]
    busy = {m for g in playing for m in g['members']}
    idle = [r['label'] for r in _shown(snap) if r['uid'] not in busy]
    if not playing:
        return f"Nothing is playing. The speakers are in {join([r['label'] for r in _shown(snap)])}." + _quiet_note()
    text = '. '.join(_line(g, _labels(snap, g['members']), False) for g in playing) + '.'
    if idle:
        text += f" Off: {', '.join(idle)}."
    return text + _quiet_note()


# ---------------------------------------------------------------- play

def _exact_group(snap, uids):
    for g in snap['groups']:
        if set(g['members']) == set(uids):
            return g
    return None


def _find(what):
    try:
        return music.find(what)
    except music.NotFound:
        raise ToolError(f"I couldn't find any music or radio station for {what!r}. "
                        "Try a genre like jazz or rock, or a station name.") from None


def _limit_for_guests(rooms, mode):
    """Turn rooms above the assistant's limit down before music starts; the rooms turned down."""
    if mode == OWNER:
        return []
    cap = max_volume()
    turned = []
    for r in rooms:
        if r.get('can_volume', True) and r.get('volume', 0) > cap:
            service.set_room_volume(r['uid'], cap)
            turned.append(r['label'])
    return turned


def _play(args, mode):
    snap = _snap()
    targets = _targets(snap, args['speakers'])
    online = [r for r in targets if r.get('online', True)]
    if not online:
        labels = [r['label'] for r in targets]
        raise ToolError(f"{_cap(_the(labels))} {_verb(labels, 'is', 'are')} offline.")
    if len({split_id(r['uid'])[0] for r in online}) > 1:
        raise ToolError("Those rooms are on different speaker systems, so they can't play together.")
    labels = [r['label'] for r in online]
    where = _the(labels)
    uids = [r['uid'] for r in online]
    what = (args.get('what') or '').strip()
    item = _find(what) if music.query(what) else None
    group = _exact_group(snap, uids)
    if item is None and group is not None and not group.get('tv'):
        if group['state'] in PLAYING:
            return f"{_cap(where)} {_verb(labels, 'is', 'are')} already playing {_what(group) or 'music'}."
        if group['state'] == 'PAUSED_PLAYBACK' and group.get('title'):
            turned = _limit_for_guests(online, mode)
            service.transport(group['id'], 'play')
            homepage.resumed([group['id']])
            service.invalidate()
            return f"Resuming {_what(group)} in {where}." + _turned(turned)
    if item is None:
        item = _find(setting('default_music', DEFAULT_MUSIC) or DEFAULT_MUSIC)
    if group is None:
        groups = _groups(snap)
        group_of = {r['uid']: r['group'] for r in online}
        anchor = next((u for u in uids if groups.get(group_of[u], {}).get('state') in PLAYING), uids[0])
        gid = service.set_rooms(anchor, uids) or anchor
        time.sleep(SETTLE_SECONDS)
    else:
        gid = group['id']
    turned = _limit_for_guests(online, mode)
    service.play_item(gid, item)
    homepage.resumed([gid])
    service.invalidate()
    text = f'Playing {item.title} in {where}.' + _turned(turned)
    quiet_ones = [r for r in online if r.get('muted') or (r.get('volume') == 0)]
    if quiet_ones:
        text += f" {_cap(_the([r['label'] for r in quiet_ones]))} {_verb(quiet_ones, 'is', 'are')} muted or at 0."
    return text


def _turned(labels):
    if not labels:
        return ''
    return f" I turned {_the(labels)} down to {max_volume()}% first."


# ---------------------------------------------------------------- transport

def _ask_which(groups, snap, verb):
    names = [' + '.join(_labels(snap, g['members'])) for g in groups]
    raise ToolError(f"Music is {verb} in {join(names)}. Which one?")


def _control(args, mode):
    action = args['action']
    op = OPS[action]
    snap = _snap()
    groups = _groups(snap)
    if args.get('speakers'):
        targets = _targets(snap, args['speakers'], strict=False)
        chosen = [groups[g] for g in dict.fromkeys(r['group'] for r in targets) if g in groups]
    elif op == 'pause':
        chosen = _playing_music(snap)
        if not chosen:
            return 'Nothing is playing.'
    elif op == 'play':
        recent = [groups[g] for g in homepage.to_resume() if g in groups and groups[g]['state'] not in PLAYING]
        paused = [g for g in snap['groups'] if g['state'] == 'PAUSED_PLAYBACK' and g.get('title')
                  and not g.get('tv')]
        chosen = recent or paused
        if not chosen:
            raise ToolError('Nothing is paused. Say which room, or what to play.')
        if not recent and len(chosen) > 1:
            _ask_which(chosen, snap, 'paused')
    else:
        chosen = _playing_music(snap)
        if not chosen:
            raise ToolError('Nothing is playing.')
        if len(chosen) > 1:
            _ask_which(chosen, snap, 'playing')
    done, already, failed = [], [], []
    for g in chosen:
        if ((op == 'pause' and g['state'] not in PLAYING) or (op == 'play' and g['state'] in PLAYING)
                or (op in ('next', 'previous') and g['state'] not in PLAYING)):
            already.append(g)
            continue
        try:
            service.transport(g['id'], op)
            done.append(g)
        except SpeakerError as exc:
            failed.append((g, str(exc)))
        except Exception:  # noqa: BLE001 - e.g. a radio station can't skip
            log.info('assistant %s on %s failed', op, g['id'], exc_info=True)
            failed.append((g, "can't skip this one" if op in ('next', 'previous') else "didn't respond"))
    if op == 'pause':
        homepage.remember_paused([g['id'] for g in done])
    elif op == 'play':
        homepage.resumed([g['id'] for g in done + already])
    service.invalidate()
    return _control_text(action, snap, done, already, failed)


DONE_TEXT = {'pause': 'Paused the music in {}.', 'stop': 'Stopped the music in {}.',
             'resume': 'Resumed the music in {}.', 'next': 'Skipped to the next track in {}.',
             'previous': 'Back to the previous track in {}.'}


def _control_text(action, snap, done, already, failed):
    def labels(gs):
        return [label for g in gs for label in _labels(snap, g['members'])]

    parts = []
    if done:
        parts.append(DONE_TEXT[action].format(_the(labels(done))))
    if already:
        names = labels(already)
        if action in ('next', 'previous'):
            parts.append(f"{_cap(_the(names))} {_verb(names, 'is', 'are')} not playing anything.")
            if not done:
                raise ToolError(' '.join(parts))
        else:
            state = 'playing' if action == 'resume' else 'not playing'
            parts.append(f"{_cap(_the(names))} {_verb(names, 'was', 'were')} already {state}.")
    for g, reason in failed:
        parts.append(f"{_cap(_the(labels([g])))}: {reason[:1].lower()}{reason[1:]}.")
    if not done and failed and not already:
        raise ToolError(' '.join(parts))
    return ' '.join(parts)


# ---------------------------------------------------------------- volume

def _volume(args, mode):
    level, change = args.get('level'), args.get('change')
    by = args.get('by') or STEP
    if level is None and change is None:
        raise ToolError(NO_LEVEL)
    snap = _snap()
    if args.get('speakers'):
        targets = _targets(snap, args['speakers'], strict=False)
    else:
        playing = [g for g in snap['groups'] if g['state'] in PLAYING]
        if not playing:
            raise ToolError('Nothing is playing. Say which room.')
        if len(playing) > 1:
            _ask_which(playing, snap, 'playing')
        by_uid = {r['uid']: r for r in snap['rooms']}
        targets = [by_uid[m] for m in playing[0]['members'] if by_uid.get(m, {}).get('shown')]
    cap = None if mode == OWNER else max_volume()
    set_to, capped, held, skipped = {}, [], [], []
    for r in targets:
        if not r.get('online', True) or r.get('can_volume') is False:
            skipped.append(r['label'])
            continue
        cur = int(r.get('volume') or 0)
        want = level if level is not None else (cur + by if change == 'up' else cur - by)
        want = max(0, min(100, int(want)))
        if cap is not None and want > cap and want > cur:
            want = max(cap, cur) if level is None else cap
            capped.append(r['label'])
        q = quiet.cap_for([r.get('name') or r['label']]) if split_id(r['uid'])[0] == 'sonos' else None
        if q is not None and want > q:
            want = q
            held.append(r['label'])
        if want != cur:
            service.set_room_volume(r['uid'], want)
        if r.get('muted') and want > 0 and (level is not None or change == 'up'):
            service.set_room_mute(r['uid'], False)
        set_to[r['label']] = want
    service.invalidate()
    if not set_to:
        raise ToolError(f"{_cap(_the(skipped))} can't change volume right now.")
    values = set(set_to.values())
    if len(values) == 1:
        labels = list(set_to)
        text = f"{_cap(_the(labels))} {_verb(labels, 'is', 'are')} at {values.pop()}%."
    else:
        text = ', '.join(f'{label} {v}%' for label, v in set_to.items()) + '.'
    if capped:
        text += f" {cap}% is as loud as I can set it."
    if held:
        q = quiet.status() or {}
        text += f" Quiet hours hold {_the(held)} to {q.get('limit')}% until {q.get('end')}."
    if skipped:
        text += f" {_cap(_the(skipped))} can't change volume right now."
    return text


# ---------------------------------------------------------------- grouping

def _group(args, mode):
    snap = _snap()
    targets = [r for r in _targets(snap, args['speakers']) if r.get('online', True)]
    groups = _groups(snap)
    if args['action'] == 'join':
        if len(targets) < 2:
            raise ToolError('Say at least two rooms to play together.')
        if len({split_id(r['uid'])[0] for r in targets}) > 1:
            raise ToolError("Those rooms are on different speaker systems, so they can't play together.")
        lone = [r['label'] for r in targets if r.get('can_group') is False]
        if lone:
            raise ToolError(f"{_cap(_the(lone))} can't be grouped.")
        lead = next((r for r in targets if groups.get(r['group'], {}).get('state') in PLAYING
                     and not groups[r['group']].get('tv')), targets[0])
        by_uid = {r['uid']: r for r in snap['rooms']}
        current = [m for m in groups.get(lead['group'], {}).get('members', [lead['uid']])
                   if by_uid.get(m, {}).get('shown')]
        members = list(dict.fromkeys(current + [r['uid'] for r in targets]))
        if set(members) == set(current) and len(current) > 1:
            return f"{_cap(_the(_labels(snap, members)))} already play together."
        service.set_rooms(lead['uid'], members)
        service.invalidate()
        g = groups.get(lead['group'], {})
        text = f"{_cap(_the(_labels(snap, members)))} now play together"
        if g.get('state') in PLAYING and _what(g):
            text += f", playing {_what(g)}"
        return text + '.'
    uids = {r['uid'] for r in targets}
    split = []
    for gid in dict.fromkeys(r['group'] for r in targets):
        g = groups.get(gid)
        if not g or len(g['members']) < 2:
            continue
        rest = [m for m in g['members'] if m not in uids]
        if not rest:
            service.ungroup(gid)
        else:
            service.set_rooms(rest[0], rest)
        split += [r['label'] for r in targets if r['group'] == gid]
    service.invalidate()
    if not split:
        labels = [r['label'] for r in targets]
        return f"{_cap(_the(labels))} {_verb(labels, 'is', 'are')} already on {_verb(labels, 'its', 'their')} own."
    return f"{_cap(_the(split))} {_verb(split, 'plays', 'play')} on {_verb(split, 'its', 'their')} own now."


RUNNERS = {SPEAKER_STATUS: lambda a, m: _status(a), PLAY_MUSIC: _play, MEDIA_CONTROL: _control,
           SET_VOLUME: _volume, GROUP_SPEAKERS: _group}


def run_tool(tool_id, args, mode=None):
    runner = RUNNERS.get(tool_id)
    if runner is None:
        raise ToolError(f'Unknown tool {tool_id!r}')
    try:
        return runner(args or {}, mode)
    except ToolError:
        raise
    except SpeakerError as exc:
        raise ToolError(str(exc)) from None
    except Exception:  # noqa: BLE001
        log.exception('assistant tool %s failed', tool_id)
        raise ToolError(FAILED) from None

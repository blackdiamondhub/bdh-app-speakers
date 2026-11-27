"""The interface every speaker system implements.

A backend works with its own *local* ids; :mod:`speakers.service` prefixes them
with ``<key>:`` so ids from different systems never clash. A group's id is the
id of the room that leads it.

``snapshot()`` returns ``{'rooms': [...], 'groups': [...]}``:

* room: ``uid``, ``name`` (the system's name, used for layout and presets),
  ``label``, ``floor``, ``icon``, ``group`` (id of its group), ``shown``
  (picked on the Settings page; unpicked rooms only appear as greyed-out group
  members), ``volume`` (0-100), ``muted``, ``online``, ``can_group``,
  ``origin`` (e.g. "Sonos" or "Home Assistant (Lodge)").
* group: ``id``, ``members`` (room ids), ``state`` (``PLAYING``,
  ``TRANSITIONING``, ``PAUSED_PLAYBACK`` or ``STOPPED``), ``title``,
  ``artist``, ``album``, ``art`` (URL the browser can load), ``source``
  (``{name, icon, brand}`` or None), ``volume`` (0-100), ``tv`` (on a TV or
  line-in input: volume only), ``can_pause``.
"""
from abc import ABC, abstractmethod

KEYS = ('sonos', 'ha')
LEGACY_KEY = 'sonos'


def split_id(value):
    """``'ha:lodge:media_player.x'`` -> ``('ha', 'lodge:media_player.x')``.

    Ids without a known backend prefix are Sonos uids saved before 1.0.
    """
    value = str(value)
    key, sep, rest = value.partition(':')
    if sep and key in KEYS:
        return key, rest
    return LEGACY_KEY, value


class SpeakerError(Exception):
    """A request the speakers can't carry out; the message is shown to the user."""


class Backend(ABC):
    key = ''
    label = ''
    # 'grouping': rooms can play together; 'night': the night volume limit applies.
    supports = frozenset()

    def available(self):
        return True

    @abstractmethod
    def snapshot(self, force=False):
        """``{'rooms': [...], 'groups': [...]}`` with local ids (see module docs)."""

    def rooms(self):
        return self.snapshot()['rooms']

    def groups(self):
        return self.snapshot()['groups']

    def invalidate(self):
        """Forget cached state so the next snapshot is fresh."""

    @abstractmethod
    def choices(self):
        """Every room this system knows, for the Settings page: [(local id, label)]."""

    def names(self):
        """Names of every room this system knows (shown or not)."""
        return {r['name'] for r in self.rooms()}

    def extra(self):
        """Extra top-level snapshot keys (e.g. the Sonos night limit)."""
        return {}

    @abstractmethod
    def transport(self, group, op):
        """``op`` is play / pause / next / previous."""

    @abstractmethod
    def set_group_volume(self, group, volume):
        pass

    @abstractmethod
    def set_room_volume(self, room, volume):
        pass

    @abstractmethod
    def set_room_mute(self, room, muted):
        pass

    @abstractmethod
    def pause_all(self):
        """Pause everything playing on shown rooms, except TV / line-in."""

    def set_members(self, group, rooms):
        """Make the group ``group`` (or room) belongs to play exactly ``rooms``.

        Returns the id of the resulting group.
        """
        raise SpeakerError('These speakers can\u2019t be grouped')

    def set_group(self, names):
        """Group the shown rooms with these names (all when ``'*'``); returns the group id."""
        raise SpeakerError('These speakers can\u2019t be grouped')

    def ungroup(self, group):
        raise SpeakerError('These speakers can\u2019t be grouped')

    def ungroup_all(self):
        pass

    def art(self, room):
        """``(bytes, content_type)`` of the album art for a room's group, or None."""
        return None

    def play_item(self, group, item):
        """Start ``item`` (a :class:`speakers.music.Item`) on the group ``group`` leads."""
        raise SpeakerError('These speakers can\u2019t play radio')

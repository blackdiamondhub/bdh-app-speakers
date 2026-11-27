"""What the assistant can put on: Sonos favorites and TuneIn radio stations.

Favorites that point at something playable (a station or a stream) are
matched by name first. Anything else is searched on TuneIn, which every Sonos
system can play without an account. Favorites that are only shortcuts into a
music service's browse pages (e.g. Sonos Radio's) can't be started over the
LAN and are skipped.
"""
import logging
import threading
import time
from dataclasses import dataclass
from xml.sax.saxutils import escape

from . import matching

log = logging.getLogger(__name__)

TUNEIN = 'TuneIn'
TUNEIN_SID = 254
FAVORITES_TTL = 300
SEARCH_COUNT = 5
# Words that say "music" without saying which.
VAGUE = frozenset({
    'some', 'music', 'song', 'songs', 'radio', 'station', 'stations', 'playlist', 'play', 'please',
    'the', 'a', 'an', 'channel', 'tunes', 'something', 'anything', 'stuff', 'good', 'nice', 'on',
    'me', 'us', 'background', 'any', 'kind', 'of', 'tune', 'track', 'tracks',
})

_lock = threading.Lock()
_favorites = None
_favorites_at = 0.0


@dataclass(frozen=True)
class Item:
    title: str
    uri: str
    meta: str = ''
    kind: str = 'station'


def _zone():
    from .backends import sonos
    zones = sonos.zones() or sonos._discover_all()
    if not zones:
        return None
    return next(iter(zones.values()))


def favorites(force=False):
    """The Sonos favorites that can be started directly, cached for a few minutes."""
    global _favorites, _favorites_at
    with _lock:
        if not force and _favorites is not None and time.monotonic() - _favorites_at < FAVORITES_TTL:
            return list(_favorites)
    zone = _zone()
    found = []
    if zone is not None:
        try:
            for fav in zone.music_library.get_sonos_favorites(complete_result=True):
                uri = fav.resources[0].uri if getattr(fav, 'resources', None) else ''
                if uri:
                    found.append(Item(fav.title, uri, getattr(fav, 'resource_meta_data', '') or '', 'favorite'))
        except Exception:  # noqa: BLE001
            log.info('Sonos favorites unavailable', exc_info=True)
    with _lock:
        _favorites, _favorites_at = found, time.monotonic()
    return list(found)


def query(what):
    """The search words in ``what`` ("some jazz music" -> "jazz"), or '' when it names nothing."""
    words = [w for w in str(what or '').casefold().replace("'", '').split()]
    words = [w.strip('.,!?;:"') for w in words]
    return ' '.join(w for w in words if w and w not in VAGUE)


def _favorite(q):
    want = matching.key(q)
    if not want:
        return None
    favs = favorites()
    for fav in favs:
        if matching.key(fav.title) == want:
            return fav
    for fav in favs:
        if want <= matching.key(fav.title):
            return fav
    return None


def station_meta(station_id, title):
    return ('<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" '
            'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
            f'<item id="F00092020{escape(station_id)}" parentID="L" restricted="true">'
            f'<dc:title>{escape(title)}</dc:title>'
            '<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>'
            '<desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">'
            'SA_RINCON65031_</desc></item></DIDL-Lite>')


def station(station_id, title):
    """A TuneIn station (``s249973``) as something a Sonos can play."""
    uri = f'x-sonosapi-stream:{station_id}?sid={TUNEIN_SID}&flags=8224&sn=0'
    return Item(title, uri, station_meta(station_id, title), 'station')


def _tunein():
    from soco.music_services import MusicService
    return MusicService(TUNEIN, device=_zone())


def search_station(q):
    """The best TuneIn station for ``q``, or None."""
    results = _tunein().search('stations', q, 0, SEARCH_COUNT)
    for r in results:
        item_id = getattr(r, 'item_id', '') or ''
        # TuneIn ids come back as "0fffffffs249973": flags, then the station.
        sid = item_id[8:] if item_id.startswith('0fffffff') else item_id
        if sid.startswith('s') and sid[1:].isdigit():
            return station(sid, r.title)
    return None


class NotFound(LookupError):
    pass


def find(what):
    """A favorite or station for ``what``; raises :class:`NotFound`."""
    q = query(what)
    if not q:
        raise NotFound(what)
    fav = _favorite(q)
    if fav is not None:
        return fav
    try:
        found = search_station(q)
    except Exception:  # noqa: BLE001 - TuneIn unreachable or changed
        log.warning('TuneIn search for %r failed', q, exc_info=True)
        found = None
    if found is None:
        raise NotFound(q)
    return found

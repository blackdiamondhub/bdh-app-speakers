"""Speaker systems the app can drive; see :mod:`speakers.backends.base`."""
from .base import KEYS, Backend, SpeakerError, split_id

__all__ = ['KEYS', 'Backend', 'SpeakerError', 'split_id', 'get']


def get(key):
    """A new backend instance for ``key`` ('sonos' or 'ha')."""
    if key == 'sonos':
        from .sonos import SonosBackend
        return SonosBackend()
    if key == 'ha':
        from .ha import HomeAssistantBackend
        return HomeAssistantBackend()
    raise KeyError(key)

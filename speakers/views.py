import json
import logging
import time

from django.http import HttpResponse, HttpResponseNotFound, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from bdh_sdk.auth import is_admin

from . import enforcer, quiet, service
from .backends import SpeakerError
from .conf import presets

log = logging.getLogger(__name__)

# Grouping changes take a moment to show up in the zone group state.
SETTLE_SECONDS = 0.6


def speakers_view(request):
    return render(request, 'speakers/speakers.html')


def _with_admin(request, snap):
    return {**snap, 'admin': is_admin(request)}


@require_GET
def state(request):
    try:
        return JsonResponse(_with_admin(request, service.snapshot()))
    except Exception:  # noqa: BLE001
        log.exception('speakers snapshot failed')
        return JsonResponse({'error': 'Could not reach the speakers'}, status=503)


@require_GET
def art(request):
    """Album art the browser can't fetch itself (e.g. Home Assistant's proxy)."""
    room = request.GET.get('room', '')
    try:
        found = service.art(room) if room else None
    except SpeakerError:
        found = None
    except Exception:  # noqa: BLE001
        log.info('album art for %s failed', room, exc_info=True)
        found = None
    if not found:
        return HttpResponseNotFound()
    data, content_type = found
    resp = HttpResponse(data, content_type=content_type)
    resp['Cache-Control'] = 'private, max-age=300'
    resp['X-Content-Type-Options'] = 'nosniff'
    return resp


ACTIONS = {
    'set_rooms': (lambda d: service.set_rooms(d['room'], d['rooms']), True),
    'preset': (lambda d: service.set_group(_preset_rooms(d['preset'])), True),
    'ungroup': (lambda d: service.ungroup(d['group']), True),
    'ungroup_all': (lambda d: service.ungroup_all(), True),
    'pause_all': (lambda d: service.pause_all(), False),
    'transport': (lambda d: service.transport(d['group'], d['op']), False),
    'group_volume': (lambda d: service.set_group_volume(d['group'], d['volume']), False),
    'room_volume': (lambda d: service.set_room_volume(d['room'], d['volume']), False),
    'room_mute': (lambda d: service.set_room_mute(d['room'], d['muted']), False),
}


def _preset_rooms(preset_id):
    for p in presets():
        if p['id'] == preset_id:
            return p['rooms']
    raise SpeakerError('Unknown preset')


def _quiet_override(request, data):
    """Lift quiet hours for the rest of tonight, or put the limit back (admins only)."""
    if not is_admin(request):
        return JsonResponse({'error': 'Only an admin can change quiet hours'}, status=403)
    lift = data.get('lift')
    if not isinstance(lift, bool):
        return JsonResponse({'error': 'Bad request'}, status=400)
    try:
        quiet.lift(lift)
    except quiet.QuietError as e:
        return JsonResponse({'error': str(e)}, status=400)
    if not lift:
        enforcer.wake()
    service.invalidate()
    return JsonResponse(_with_admin(request, service.snapshot(force=True)))


@require_POST
def action(request):
    try:
        data = json.loads(request.body or b'{}')
        if data['action'] == 'quiet_override':
            return _quiet_override(request, data)
        fn, regroups = ACTIONS[data['action']]
    except (ValueError, KeyError, TypeError):
        return JsonResponse({'error': 'Bad request'}, status=400)
    result = None
    try:
        result = fn(data)
    except (KeyError, TypeError, ValueError):
        return JsonResponse({'error': 'Bad request'}, status=400)
    except SpeakerError as e:
        return JsonResponse({'error': str(e)}, status=400)
    except Exception:  # noqa: BLE001
        log.exception('speakers action %s failed', data.get('action'))
        return JsonResponse({'error': 'The speakers did not respond'}, status=502)
    if regroups:
        time.sleep(SETTLE_SECONDS)
    service.invalidate()
    snap = service.snapshot(force=True)
    if isinstance(result, str):
        snap = {**snap, 'selected': result}
    return JsonResponse(_with_admin(request, snap))
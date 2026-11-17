import json
import os
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from speakers import service
from speakers.backends import SpeakerError
from speakers.backends import ha as ha_backend
from speakers.backends import sonos


class FakeZone:
    def __init__(self, name, uid, model='Sonos Amp', volume=30):
        self.player_name = name
        self.uid = uid
        self.model = model
        self.volume = volume
        self.mute = False
        self.group = None
        self.avTransport = MagicMock()
        self.zone_group_state = MagicMock()
        self.transport = 'STOPPED'
        self.unjoin = MagicMock(side_effect=self._unjoin)
        self.join = MagicMock(side_effect=self._join)

    def get_speaker_info(self):
        return {'model_name': self.model}

    @property
    def is_coordinator(self):
        return self.group.coordinator is self

    def get_current_transport_info(self):
        return {'current_transport_state': self.transport}

    def get_current_track_info(self):
        return {'title': 'Song', 'artist': 'Band', 'album': '', 'album_art': '',
                'uri': 'x-sonos-vli:RINCON_1:2,spotify:abc'}

    def get_current_media_info(self):
        return {'uri': ''}

    def _unjoin(self):
        rest = [m for m in self.group.members if m is not self]
        if rest and self.group.coordinator is self:
            FakeGroup(rest[0], rest)
        else:
            self.group.members.remove(self)
        FakeGroup(self, [self])

    @property
    def all_groups(self):
        return list({z.group for z in HOUSEHOLD})

    def _join(self, master):
        if self in self.group.members:
            self.group.members.remove(self)
        master.group.members.append(self)
        self.group = master.group


class FakeGroup:
    def __init__(self, coordinator, members):
        self.coordinator = coordinator
        self.members = members
        self.volume = 40
        for m in members:
            m.group = self


HOUSEHOLD = []


def reset():
    sonos._zones.clear()
    sonos._found.clear()
    sonos._zones_at = sonos._found_at = 0.0
    sonos._zones_for = None
    sonos.invalidate()
    service.reset()
    ha_backend.clear_caches()


def household():
    zs = [FakeZone('Dining Room', 'DR'), FakeZone('Top Deck', 'TD'),
          FakeZone('Hot Tub', 'HT'), FakeZone('Office', 'OF', model='Arc Ultra')]
    FakeGroup(zs[0], [zs[0], zs[1]])
    FakeGroup(zs[2], [zs[2]])
    FakeGroup(zs[3], [zs[3]])
    HOUSEHOLD[:] = zs
    return zs


class DiscoveryTests(TestCase):
    def tearDown(self):
        reset()

    @patch('speakers.backends.sonos.soco.discover', return_value=set())
    def test_namespaced_interface_wins(self, discover):
        env = {'BDH_SPEAKERS_INTERFACE': '10.0.0.5', 'SONOS_INTERFACE': '10.0.0.9'}
        with patch.dict(os.environ, env):
            sonos._discover_sonos(timeout=1)
        discover.assert_called_once_with(timeout=1, interface_addr='10.0.0.5')

    @patch('speakers.backends.sonos.soco.discover', return_value=set())
    def test_legacy_interface_still_works(self, discover):
        with patch.dict(os.environ, {'SONOS_INTERFACE': '10.0.0.9'}):
            os.environ.pop('BDH_SPEAKERS_INTERFACE', None)
            os.environ.pop('BDH_SONOS_CONTROL_INTERFACE', None)
            sonos._discover_sonos()
        discover.assert_called_once_with(interface_addr='10.0.0.9')

    def test_only_amps_are_controllable(self):
        with patch('speakers.backends.sonos._discover_sonos', return_value=set(household())):
            names = sorted(z.player_name for z in sonos.zones(force=True).values())
        self.assertEqual(names, ['Dining Room', 'Hot Tub', 'Top Deck'])

    @override_settings(BDH_SPEAKERS_MODELS='*')
    def test_star_controls_every_model(self):
        zs = household()
        zs[3].get_speaker_info = MagicMock(side_effect=OSError('unreachable'))
        with patch('speakers.backends.sonos._discover_sonos', return_value=set(zs)):
            names = sorted(z.player_name for z in sonos.zones(force=True).values())
        self.assertEqual(names, ['Dining Room', 'Hot Tub', 'Office', 'Top Deck'])


class SiteConfigTests(TestCase):
    def test_default_preset_is_everywhere(self):
        from django.conf import settings
        with override_settings():
            for name in ('BDH_SPEAKERS_PRESETS', 'BDH_SONOS_CONTROL_PRESETS'):
                if hasattr(settings, name):  # the hub's site.toml may set one
                    delattr(settings, name)
            self.assertEqual([p['rooms'] for p in sonos.presets()], ['*'])

    @override_settings(BDH_SPEAKERS_ROOMS={'Kitchen': {'label': 'Kitchen', 'icon': 'fa-utensils'}})
    def test_rooms_from_django_setting(self):
        self.assertEqual(sonos._meta('Kitchen'), {'label': 'Kitchen', 'floor': 'other', 'icon': 'fa-utensils'})
        self.assertEqual(sonos._meta('Den'), {'label': 'Den', 'floor': 'other', 'icon': 'fa-volume-high'})

    def test_presets_from_env_json(self):
        env = {'BDH_SPEAKERS_PRESETS': json.dumps([{'id': 'kd', 'label': 'K + D', 'rooms': ['Kitchen', 'Deck']}])}
        with patch.dict(os.environ, env):
            self.assertEqual(sonos.presets()[0]['rooms'], ['Kitchen', 'Deck'])

    def test_bad_env_json_falls_back(self):
        with patch.dict(os.environ, {'BDH_SPEAKERS_PRESETS': '[oops'}):
            self.assertEqual(sonos.presets(), sonos.DEFAULT_PRESETS)


class SnapshotTests(TestCase):
    def setUp(self):
        self.zs = household()
        self.zs[0].transport = 'PLAYING'
        sonos._zones.clear()
        sonos._zones.update({z.uid: z for z in self.zs[:3]})
        sonos._zones_at = float('inf')

    def tearDown(self):
        reset()

    def test_groups_rooms_and_source(self):
        snap = sonos.snapshot(force=True)
        groups = {g['id']: g for g in snap['groups']}
        self.assertEqual(set(groups), {'DR', 'HT'})
        self.assertEqual(sorted(groups['DR']['members']), ['DR', 'TD'])
        self.assertEqual(groups['DR']['state'], 'PLAYING')
        self.assertEqual(groups['DR']['source']['name'], 'Spotify Connect')
        labels = {r['label'] for r in snap['rooms']}
        self.assertEqual(labels, {'Dining Room', 'Top Deck', 'Hot Tub'})
        self.assertNotIn('OF', [r['uid'] for r in snap['rooms']])

    def test_coordinator_leaving_hands_off(self):
        sonos.leave('DR')
        self.zs[0].avTransport.DelegateGroupCoordinationTo.assert_called_once()
        args = self.zs[0].avTransport.DelegateGroupCoordinationTo.call_args[0][0]
        self.assertIn(('NewCoordinator', 'TD'), args)
        self.zs[0].unjoin.assert_not_called()

    def test_coordinator_leaving_falls_back_to_regrouping(self):
        self.zs[0].avTransport.DelegateGroupCoordinationTo.side_effect = Exception('nope')
        self.zs[2].join(self.zs[0])
        sonos.leave('DR')
        self.zs[0].unjoin.assert_called_once()
        self.assertIs(self.zs[2].group.coordinator, self.zs[1])

    def test_member_leaving_just_unjoins(self):
        sonos.leave('TD')
        self.zs[1].unjoin.assert_called_once()

    def test_preset_keeps_playing_coordinator(self):
        coord = sonos.set_group(['Top Deck', 'Hot Tub', 'Dining Room'])
        self.assertEqual(coord, 'DR')
        self.assertIs(self.zs[2].group.coordinator, self.zs[0])

    def test_preset_drops_rooms_not_in_it(self):
        sonos.set_group(['Dining Room', 'Hot Tub'])
        self.zs[1].unjoin.assert_called_once()
        self.assertIs(self.zs[2].group.coordinator, self.zs[0])

    def _lower_deck_with_hot_tub(self):
        ld = FakeZone('Lower Deck', 'LD')
        FakeGroup(self.zs[2], [self.zs[2], ld])
        HOUSEHOLD.append(ld)
        sonos._zones['LD'] = ld
        return ld

    def test_set_rooms_pulls_room_from_another_group(self):
        ld = self._lower_deck_with_hot_tub()
        self.assertEqual(sonos.set_rooms('DR', ['DR', 'TD', 'LD']), 'DR')
        self.assertIs(ld.group.coordinator, self.zs[0])
        self.assertEqual(self.zs[2].group.members, [self.zs[2]])

    @patch('speakers.backends.sonos.time.sleep')
    def test_set_rooms_unticking_coordinator_keeps_the_rest(self, sleep):
        self.zs[0].avTransport.DelegateGroupCoordinationTo.side_effect = Exception('nope')
        self.assertEqual(sonos.set_rooms('DR', ['TD', 'HT']), 'TD')
        self.assertIs(self.zs[2].group.coordinator, self.zs[1])
        self.assertEqual(self.zs[0].group.members, [self.zs[0]])

    def test_set_rooms_from_solo_player(self):
        self.assertEqual(sonos.set_rooms('HT', ['HT', 'TD']), 'HT')
        self.assertIs(self.zs[1].group.coordinator, self.zs[2])
        self.assertEqual(self.zs[0].group.members, [self.zs[0]])

    def test_set_rooms_needs_a_room(self):
        with self.assertRaises(sonos.SonosError):
            sonos.set_rooms('DR', [])

    def test_ungroup_all(self):
        sonos.ungroup_all()
        self.zs[1].unjoin.assert_called_once()
        self.zs[0].unjoin.assert_not_called()

    NIGHT = {'active': True, 'limit': 25, 'end': '07:00', 'rooms': ['Top Deck', 'Hot Tub']}

    def test_night_limit_caps_outdoor_room_volume(self):
        with patch('speakers.backends.sonos.night_status', return_value=self.NIGHT):
            sonos.set_room_volume('TD', 80)
            sonos.set_room_volume('DR', 80)
        self.assertEqual(self.zs[1].volume, 25)
        self.assertEqual(self.zs[0].volume, 80)

    def test_night_limit_caps_group_only_when_all_rooms_limited(self):
        with patch('speakers.backends.sonos.night_status', return_value=self.NIGHT):
            sonos.set_group_volume('HT', 70)
            sonos.set_group_volume('DR', 70)
        self.assertEqual(self.zs[2].group.volume, 25)
        self.assertEqual(self.zs[0].group.volume, 70)

    def test_inactive_night_limit_does_not_cap(self):
        with patch('speakers.backends.sonos.night_status', return_value=dict(self.NIGHT, active=False)):
            sonos.set_room_volume('TD', 80)
        self.assertEqual(self.zs[1].volume, 80)


class NightStatusTests(TestCase):
    def setUp(self):
        sonos._night_at = 0.0

    tearDown = setUp

    def test_off_without_url(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('BDH_SPEAKERS_NIGHT_URL', None)
            os.environ.pop('BDH_SONOS_CONTROL_NIGHT_URL', None)
            self.assertIsNone(sonos.night_status())

    @patch('speakers.backends.sonos.urllib.request.urlopen')
    def test_reads_appdaemon_status(self, urlopen):
        body = b'{"active": true, "limit": 25, "end": "07:00", "rooms": ["Top Deck"], "overridden": false}'
        urlopen.return_value.__enter__.return_value.read.return_value = body
        with patch.dict(os.environ, {'BDH_SPEAKERS_NIGHT_URL': 'http://ad/api/appdaemon/sonos_night'}):
            self.assertEqual(sonos.night_status(),
                             {'active': True, 'limit': 25, 'end': '07:00', 'rooms': ['Top Deck']})

    @patch('speakers.backends.sonos.urllib.request.urlopen', side_effect=OSError('down'))
    def test_unreachable_means_no_limit_shown(self, urlopen):
        with patch.dict(os.environ, {'BDH_SPEAKERS_NIGHT_URL': 'http://ad/x'}):
            self.assertIsNone(sonos.night_status())


class SourceTests(TestCase):
    def test_sources(self):
        self.assertEqual(sonos.source_for('x-sonos-vli:RINCON_1:2,spotify:x', {})['name'], 'Spotify Connect')
        self.assertEqual(sonos.source_for('x-sonos-vli:RINCON_1:1,airplay:x', {})['name'], 'AirPlay')
        self.assertEqual(sonos.source_for('x-sonos-htastream:RINCON_1:spdif', {})['name'], 'TV')
        self.assertIsNone(sonos.source_for('', {}))


class ViewTests(TestCase):
    def test_page_renders_without_spotify(self):
        res = self.client.get(reverse('speakers'))
        self.assertEqual(res.status_code, 200)
        content = res.content.decode()
        self.assertIn('id="picker"', content)
        self.assertNotIn('spotify_search', content)

    @patch('speakers.views.service.snapshot', return_value={'rooms': [], 'groups': [], 'presets': []})
    def test_state_json(self, snap):
        res = self.client.get(reverse('speakers_state'))
        self.assertEqual(res.json()['groups'], [])

    @patch('speakers.views.service.snapshot', side_effect=OSError('down'))
    def test_state_unreachable(self, snap):
        self.assertEqual(self.client.get(reverse('speakers_state')).status_code, 503)

    def post(self, body):
        return self.client.post(reverse('speakers_action'), json.dumps(body), content_type='application/json')

    def test_unknown_action_rejected(self):
        self.assertEqual(self.post({'action': 'format_disk'}).status_code, 400)

    @override_settings(BDH_SPEAKERS_PRESETS=[
        {'id': 'dining_deck', 'label': 'Dining + Top Deck', 'rooms': ['Dining Room', 'Top Deck']}])
    @patch('speakers.views.time.sleep')
    @patch('speakers.views.service.snapshot', return_value={'rooms': [], 'groups': [], 'presets': []})
    @patch('speakers.views.service.set_group', return_value='DR')
    def test_preset_action(self, set_group, snap, sleep):
        res = self.post({'action': 'preset', 'preset': 'dining_deck'})
        set_group.assert_called_once_with(['Dining Room', 'Top Deck'])
        self.assertEqual(res.json()['selected'], 'DR')

    @patch('speakers.views.service.snapshot', return_value={'rooms': [], 'groups': [], 'presets': []})
    @patch('speakers.views.service.set_room_volume')
    def test_room_volume_action(self, set_vol, snap):
        self.assertEqual(self.post({'action': 'room_volume', 'room': 'DR', 'volume': 20}).status_code, 200)
        set_vol.assert_called_once_with('DR', 20)

    @patch('speakers.views.time.sleep')
    @patch('speakers.views.service.snapshot', return_value={'rooms': [], 'groups': [], 'presets': []})
    @patch('speakers.views.service.set_rooms', return_value='DR')
    def test_set_rooms_action(self, set_rooms, snap, sleep):
        res = self.post({'action': 'set_rooms', 'room': 'DR', 'rooms': ['DR', 'HT']})
        self.assertEqual(res.status_code, 200)
        set_rooms.assert_called_once_with('DR', ['DR', 'HT'])

    @patch('speakers.views.service.set_rooms', side_effect=RuntimeError('timeout'))
    def test_speaker_failure_is_reported(self, set_rooms):
        self.assertEqual(self.post({'action': 'set_rooms', 'room': 'HT', 'rooms': ['HT', 'DR']}).status_code, 502)

class SpeakerPickerTests(TestCase):
    """The "Speakers to show" setting picks rooms by uid instead of by model."""

    def setUp(self):
        self.zs = household()
        p = patch('speakers.backends.sonos._discover_sonos', side_effect=lambda **kw: set(self.found))
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(reset)
        self.found = list(self.zs)

    def names(self, **kw):
        return sorted(z.player_name for z in sonos.zones(**kw).values())

    @override_settings(BDH_SPEAKERS_SPEAKERS=['OF', 'HT'])
    def test_picked_uids_override_the_model_filter(self):
        self.assertEqual(self.names(force=True), ['Hot Tub', 'Office'])

    @override_settings(BDH_SPEAKERS_SPEAKERS=[])
    def test_nothing_picked_shows_nothing(self):
        self.assertEqual(self.names(force=True), [])

    def test_changing_the_pick_skips_the_cache(self):
        with override_settings(BDH_SPEAKERS_SPEAKERS=['DR']):
            self.assertEqual(self.names(), ['Dining Room'])
        with override_settings(BDH_SPEAKERS_SPEAKERS=['TD']):
            self.assertEqual(self.names(), ['Top Deck'])

    def test_choices_list_every_room_and_offline_picks(self):
        self.assertEqual([label for _, label in sonos.speaker_choices()],
                         ['Dining Room (Sonos Amp)', 'Hot Tub (Sonos Amp)',
                          'Office (Arc Ultra)', 'Top Deck (Sonos Amp)'])
        self.found = self.zs[:3]
        sonos._found_at = 0.0
        with override_settings(BDH_SPEAKERS_SPEAKERS=['OF', 'GONE']):
            choices = dict(sonos.speaker_choices())
        self.assertEqual(choices['OF'], 'Office (Arc Ultra) \u2013 offline')
        self.assertEqual(choices['GONE'], 'GONE \u2013 offline')


class PickedGroupTests(TestCase):
    """Groups that mix picked and unpicked rooms."""

    def setUp(self):
        self.zs = household()
        for z in self.zs:
            z.pause = MagicMock()
            z.play = MagicMock()
        self.addCleanup(reset)

    def show(self, *uids):
        sonos._zones.clear()
        sonos._zones.update({z.uid: z for z in self.zs if z.uid in uids})
        sonos._zones_at = float('inf')

    def test_unpicked_members_are_greyed_guests(self):
        self.show('TD')
        snap = sonos.snapshot(force=True)
        self.assertEqual([g['id'] for g in snap['groups']], ['DR'])
        shown = {r['uid']: r['shown'] for r in snap['rooms']}
        self.assertEqual(shown, {'TD': True, 'DR': False})

    def test_ungroup_pauses_an_unpicked_coordinator(self):
        self.show('TD')
        self.zs[0].transport = 'PLAYING'
        sonos.ungroup('DR')
        self.zs[1].unjoin.assert_called_once()
        self.zs[0].pause.assert_called_once()

    def test_ungroup_keeps_a_picked_coordinator_playing(self):
        self.show('DR', 'TD')
        self.zs[0].transport = 'PLAYING'
        sonos.ungroup('DR')
        self.assertEqual(self.zs[1].group.members, [self.zs[1]])
        self.zs[0].pause.assert_not_called()

    def test_ungroup_all_leaves_unpicked_groups_alone(self):
        self.show('HT')
        sonos.ungroup_all()
        self.zs[1].unjoin.assert_not_called()
        self.assertEqual(len(self.zs[0].group.members), 2)

    def test_pause_all_skips_the_tv(self):
        self.show('DR', 'TD', 'HT')
        self.zs[0].transport = self.zs[2].transport = 'PLAYING'
        self.zs[0].get_current_media_info = lambda: {'uri': 'x-sonos-htastream:RINCON_DR:spdif'}
        sonos.pause_all()
        self.zs[0].pause.assert_not_called()
        self.zs[2].pause.assert_called_once()

    def test_play_pause_refused_on_tv(self):
        self.show('DR', 'TD')
        self.zs[0].get_current_media_info = lambda: {'uri': 'x-sonos-htastream:RINCON_DR:spdif'}
        with self.assertRaises(sonos.SonosError):
            sonos.transport('DR', 'pause')
        self.assertTrue(sonos.snapshot(force=True)['groups'][0]['tv'])

    @patch('speakers.views.time.sleep')
    @patch('speakers.views.service.snapshot', return_value={'rooms': [], 'groups': [], 'presets': []})
    @patch('speakers.views.service.ungroup')
    def test_ungroup_action(self, ungroup, snap, sleep):
        res = self.client.post(reverse('speakers_action'), json.dumps({'action': 'ungroup', 'group': 'DR'}),
                               content_type='application/json')
        self.assertEqual(res.status_code, 200)
        ungroup.assert_called_once_with('DR')


def snap(*states):
    """A snapshot with groups DR (Dining Room + Top Deck) and HT (Hot Tub)."""
    rooms = [{'uid': 'DR', 'label': 'Dining'}, {'uid': 'TD', 'label': 'Top Deck'},
             {'uid': 'HT', 'label': 'Hot Tub'}]
    groups = [{'id': 'DR', 'members': ['DR', 'TD'], 'state': states[0], 'title': 'Song',
               'artist': 'Band', 'art': 'http://10.0.0.5:1400/getaa?x'},
              {'id': 'HT', 'members': ['HT'], 'state': states[1], 'title': '', 'artist': '', 'art': ''}]
    return {'rooms': rooms, 'groups': groups}


class HomepageTileTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.now = 1000.0
        p = patch('speakers.homepage.time.time', side_effect=lambda: self.now)
        p.start()
        self.addCleanup(p.stop)

    def status(self, *states):
        from speakers import homepage
        with patch('speakers.homepage.service.snapshot', return_value=snap(*states)):
            return homepage.status()

    def test_lists_only_playing_groups(self):
        s = self.status('PLAYING', 'PAUSED_PLAYBACK')
        self.assertEqual(s.text, '1 playing')
        self.assertEqual(s.refresh, 3)
        (row,) = s.rows
        self.assertEqual((row.id, row.title, row.detail), ('DR', 'Dining + Top Deck', 'Song – Band'))
        self.assertEqual([a.id for a in row.actions], ['pause', 'next'])
        self.assertEqual([a.id for a in s.actions], ['pause_all'])

    def test_nothing_playing_shows_no_status(self):
        self.assertIsNone(self.status('PAUSED_PLAYBACK', 'STOPPED'))

    def test_paused_group_stays_dimmed_during_grace(self):
        self.status('PLAYING', 'STOPPED')
        self.now += 60
        s = self.status('PAUSED_PLAYBACK', 'STOPPED')
        self.assertEqual(s.text, 'Paused')
        self.assertTrue(s.rows[0].dimmed)
        self.assertEqual(s.rows[0].actions[0].id, 'play')
        self.now += 300
        self.assertIsNone(self.status('PAUSED_PLAYBACK', 'STOPPED'))

    @override_settings(BDH_SPEAKERS_PAUSE_GRACE=1)
    def test_grace_setting(self):
        self.status('PLAYING', 'STOPPED')
        self.now += 30
        self.assertIsNotNone(self.status('PAUSED_PLAYBACK', 'STOPPED'))
        self.now += 59
        self.assertIsNotNone(self.status('PAUSED_PLAYBACK', 'STOPPED'))
        self.now += 2
        self.assertIsNone(self.status('PAUSED_PLAYBACK', 'STOPPED'))

    @patch('speakers.homepage.service.transport')
    def test_row_pause_shows_at_once(self, transport):
        from speakers import homepage
        self.status('PLAYING', 'STOPPED')
        homepage.run_action('DR', 'pause')
        transport.assert_called_once_with('DR', 'pause')
        s = self.status('PLAYING', 'STOPPED')  # speakers haven't caught up yet
        self.assertTrue(s.rows[0].dimmed)

    @patch('speakers.homepage.service.transport')
    @patch('speakers.homepage.service.pause_all')
    def test_pause_all_then_resume(self, pause_all, transport):
        from speakers import homepage
        self.status('PLAYING', 'PLAYING')
        homepage.run_action('', 'pause_all')
        pause_all.assert_called_once()
        self.now += 10
        s = self.status('PAUSED_PLAYBACK', 'PAUSED_PLAYBACK')
        self.assertEqual([a.id for a in s.actions], ['resume'])
        homepage.run_action('', 'resume')
        self.assertEqual(sorted(c.args for c in transport.call_args_list), [('DR', 'play'), ('HT', 'play')])

    def test_unknown_action(self):
        from speakers import homepage
        with self.assertRaises(SpeakerError):
            homepage.run_action('DR', 'format_disk')

    def test_snapshot_failure_hides_status(self):
        from speakers import homepage
        with patch('speakers.homepage.service.snapshot', side_effect=OSError('down')):
            self.assertIsNone(homepage.status())

    def test_tv_groups_are_left_off(self):
        from speakers import homepage
        data = snap('PLAYING', 'PLAYING')
        data['groups'][0]['tv'] = True
        with patch('speakers.homepage.service.snapshot', return_value=data):
            s = homepage.status()
        self.assertEqual([r.id for r in s.rows], ['HT'])


# ---------------------------------------------------------------- Home Assistant

GROUPABLE = 1 | 4 | 8 | 16 | 32 | 16384 | 524288
BASIC = 1 | 4 | 8 | 16384


def player(eid, name, state='paused', features=GROUPABLE, **attrs):
    return {'entity_id': eid, 'state': state,
            'attributes': {'friendly_name': name, 'supported_features': features,
                           'volume_level': 0.3, 'is_volume_muted': False, **attrs}}


class FakeHA:
    """Stands in for bdh_sdk.ha with two instances; records every service call."""

    def __init__(self, states):
        self.states = states
        self.calls = []
        self.static = {}

    def instances(self):
        return [{'id': 'lodge', 'name': 'Lodge'}, {'id': 'cabin', 'name': 'Cabin'}]

    def get_all_states(self, instance=None):
        if isinstance(self.states.get(instance), Exception):
            raise self.states[instance]
        return self.states.get(instance, [])

    def call_service(self, domain, service, entity_id=None, extra_data=None, instance=None):
        self.calls.append((instance, service, entity_id, extra_data))
        return True, None

    def get_static(self, path, instance=None):
        self.calls.append((instance, 'get_static', path, None))
        return self.static[path]


class HABase(TestCase):
    def setUp(self):
        reset()
        self.addCleanup(reset)
        self.ha = FakeHA({
            'lodge': [
                player('media_player.kitchen', 'Kitchen', 'playing', media_title='Song',
                       media_artist='Band', media_album_name='LP', volume_level=0.42,
                       group_members=['media_player.kitchen', 'media_player.den'],
                       entity_picture='/api/media_player_proxy/media_player.kitchen?token=abc',
                       app_name='Spotify'),
                player('media_player.den', 'Den', 'playing',
                       group_members=['media_player.kitchen', 'media_player.den']),
                player('media_player.tv', 'Living TV', 'on', features=BASIC, source='HDMI 1'),
                player('media_player.gone', 'Gone', 'unavailable', features=0),
                {'entity_id': 'light.kitchen', 'state': 'on', 'attributes': {}},
            ],
            'cabin': [player('media_player.kitchen', 'Cabin Kitchen', 'idle', features=BASIC)],
        })
        p = patch.multiple('speakers.backends.ha.ha', instances=self.ha.instances,
                           get_all_states=self.ha.get_all_states,
                           call_service=self.ha.call_service, get_static=self.ha.get_static)
        p.start()
        self.addCleanup(p.stop)

    def snap(self):
        return service.snapshot(force=True)


@override_settings(BDH_SPEAKERS_BACKENDS=['ha'])
class HABackendTests(HABase):
    def test_state_mapping(self):
        snap = self.snap()
        rooms = {r['uid']: r for r in snap['rooms']}
        self.assertNotIn('ha:lodge:light.kitchen', rooms)
        kitchen = rooms['ha:lodge:media_player.kitchen']
        self.assertEqual((kitchen['volume'], kitchen['muted'], kitchen['online']), (42, False, True))
        self.assertEqual(kitchen['origin'], 'Home Assistant (Lodge)')
        self.assertFalse(rooms['ha:lodge:media_player.gone']['online'])
        groups = {g['id']: g for g in snap['groups']}
        g = groups['ha:lodge:media_player.kitchen']
        self.assertEqual((g['state'], g['title'], g['artist'], g['album']), ('PLAYING', 'Song', 'Band', 'LP'))
        self.assertEqual(g['source']['name'], 'Spotify')
        self.assertTrue(g['can_pause'])
        self.assertNotIn('token', g['art'])
        self.assertTrue(g['art'].startswith(reverse('speakers_art') + '?room=ha%3Alodge%3Amedia_player.kitchen'))
        tv = groups['ha:lodge:media_player.tv']
        self.assertEqual(tv['state'], 'STOPPED')
        self.assertTrue(tv['tv'])
        self.assertFalse(tv['can_pause'])

    def test_multi_instance_ids(self):
        uids = {r['uid'] for r in self.snap()['rooms']}
        self.assertIn('ha:lodge:media_player.kitchen', uids)
        self.assertIn('ha:cabin:media_player.kitchen', uids)
        service.set_room_volume('ha:cabin:media_player.kitchen', 55)
        self.assertEqual(self.ha.calls[-1], ('cabin', 'volume_set', 'media_player.kitchen', {'volume_level': 0.55}))

    def test_grouping_only_when_supported(self):
        snap = self.snap()
        groups = {g['id']: g['members'] for g in snap['groups']}
        self.assertCountEqual(groups['ha:lodge:media_player.kitchen'],
                              ['ha:lodge:media_player.den', 'ha:lodge:media_player.kitchen'])
        self.assertEqual(groups['ha:lodge:media_player.tv'], ['ha:lodge:media_player.tv'])
        self.assertFalse({r['uid']: r for r in snap['rooms']}['ha:lodge:media_player.tv']['can_group'])
        with self.assertRaises(SpeakerError):
            service.set_rooms('ha:lodge:media_player.den',
                              ['ha:lodge:media_player.den', 'ha:lodge:media_player.tv'])
        self.assertEqual(self.ha.calls, [])

    def test_set_rooms_joins_and_unjoins(self):
        result = service.set_rooms('ha:lodge:media_player.kitchen', ['ha:lodge:media_player.kitchen'])
        self.assertEqual(result, 'ha:lodge:media_player.kitchen')
        self.assertEqual(self.ha.calls, [('lodge', 'unjoin', 'media_player.den', None)])

    def test_rooms_on_two_instances_cannot_group(self):
        with self.assertRaises(SpeakerError):
            service.set_rooms('ha:lodge:media_player.kitchen',
                              ['ha:lodge:media_player.kitchen', 'ha:cabin:media_player.kitchen'])
        self.assertEqual(self.ha.calls, [])

    def test_transport_and_tv(self):
        service.transport('ha:lodge:media_player.kitchen', 'pause')
        self.assertEqual(self.ha.calls[-1], ('lodge', 'media_pause', 'media_player.kitchen', None))
        with self.assertRaises(SpeakerError):
            service.transport('ha:lodge:media_player.tv', 'pause')
        with self.assertRaises(SpeakerError):
            service.transport('ha:cabin:media_player.kitchen', 'next')  # no NEXT_TRACK bit

    def test_one_instance_failing_keeps_the_other(self):
        from bdh_sdk.ha import HAError
        self.ha.states['cabin'] = HAError('down')
        uids = {r['uid'] for r in self.snap()['rooms']}
        self.assertIn('ha:lodge:media_player.kitchen', uids)
        self.assertNotIn('ha:cabin:media_player.kitchen', uids)

    @override_settings(BDH_SPEAKERS_SPEAKERS=['ha:lodge:media_player.den', 'DR'])
    def test_picked_rooms_and_guests(self):
        rooms = {r['uid']: r['shown'] for r in self.snap()['rooms']}
        self.assertEqual(rooms, {'ha:lodge:media_player.den': True, 'ha:lodge:media_player.kitchen': False})

    def test_choices_name_the_instance(self):
        choices = dict(service.speaker_choices())
        self.assertEqual(choices['ha:cabin:media_player.kitchen'], 'Cabin Kitchen \u00b7 Home Assistant (Cabin)')
        self.assertEqual(choices['ha:lodge:media_player.den'], 'Den \u00b7 Home Assistant (Lodge)')


@override_settings(BDH_SPEAKERS_BACKENDS=['ha'])
class HAArtTests(HABase):
    JPEG = b'\xff\xd8\xff\xe0' + b'0' * 20

    def test_art_is_proxied_and_cached(self):
        self.ha.static['/api/media_player_proxy/media_player.kitchen?token=abc'] = self.JPEG
        url = reverse('speakers_art')
        for _ in range(2):
            res = self.client.get(url, {'room': 'ha:lodge:media_player.kitchen'})
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res['Content-Type'], 'image/jpeg')
            self.assertEqual(res.content, self.JPEG)
        self.assertEqual(len([c for c in self.ha.calls if c[1] == 'get_static']), 1)

    def test_svg_and_unknown_rooms_are_refused(self):
        self.ha.static['/api/media_player_proxy/media_player.kitchen?token=abc'] = b'<svg onload=alert(1)>'
        url = reverse('speakers_art')
        self.assertEqual(self.client.get(url, {'room': 'ha:lodge:media_player.kitchen'}).status_code, 404)
        self.assertEqual(self.client.get(url, {'room': 'ha:lodge:light.kitchen'}).status_code, 404)
        self.assertEqual(self.client.get(url, {'room': 'ha:lodge:media_player.nope'}).status_code, 404)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_unsafe_paths_are_not_fetched(self):
        for path in ('http://evil/x.jpg', '//evil/x', '/api/../secret', '/a\\b', 'relative.jpg',
                     '/x\nHost: evil', 'http:/x'):
            self.assertFalse(ha_backend._safe_path(path), path)
        self.assertTrue(ha_backend._safe_path('/api/media_player_proxy/media_player.x?token=a:b&cache=1'))
        self.ha.states['lodge'][0]['attributes']['entity_picture'] = '//evil/x.jpg'
        res = self.client.get(reverse('speakers_art'), {'room': 'ha:lodge:media_player.kitchen'})
        self.assertEqual(res.status_code, 404)
        self.assertEqual([c for c in self.ha.calls if c[1] == 'get_static'], [])


class MixedBackendTests(HABase):
    """Sonos and Home Assistant side by side."""

    def setUp(self):
        super().setUp()
        self.zs = household()
        sonos._zones.update({z.uid: z for z in self.zs[:3]})
        sonos._zones_at = float('inf')
        sonos._found.update({z.uid: z for z in self.zs})
        sonos._found_at = float('inf')
        self.ha.states['lodge'].append(player('media_player.dining_room', ' dining room ', 'playing'))
        p = patch('speakers.backends.sonos.night_status', return_value=None)
        p.start()
        self.addCleanup(p.stop)
        p = patch('speakers.backends.sonos._discover_sonos', side_effect=lambda **kw: set(self.zs))
        p.start()
        self.addCleanup(p.stop)

    def test_sonos_rooms_seen_through_ha_are_hidden(self):
        uids = {r['uid'] for r in self.snap()['rooms']}
        self.assertIn('sonos:DR', uids)
        self.assertIn('ha:lodge:media_player.kitchen', uids)
        self.assertNotIn('ha:lodge:media_player.dining_room', uids)
        self.assertNotIn('ha:lodge:media_player.dining_room', dict(service.speaker_choices()))

    def test_mixed_group_rejected(self):
        with self.assertRaises(SpeakerError):
            service.set_rooms('sonos:DR', ['sonos:DR', 'ha:lodge:media_player.kitchen'])
        with self.assertRaises(SpeakerError):
            service.set_rooms('ha:lodge:media_player.kitchen', ['ha:lodge:media_player.kitchen', 'TD'])
        self.assertEqual(self.ha.calls, [])
        self.zs[0].unjoin.assert_not_called()
        res = self.client.post(reverse('speakers_action'),
                               json.dumps({'action': 'set_rooms', 'room': 'sonos:DR',
                                           'rooms': ['sonos:DR', 'ha:lodge:media_player.den']}),
                               content_type='application/json')
        self.assertEqual(res.status_code, 400)

    def test_unprefixed_ids_are_sonos(self):
        self.zs[2].play = MagicMock()
        service.transport('HT', 'play')
        self.zs[2].play.assert_called_once()
        self.assertEqual(self.ha.calls, [])

    @override_settings(BDH_SPEAKERS_SPEAKERS=['HT', 'sonos:TD'])
    def test_legacy_speaker_list_picks_sonos(self):
        sonos._zones_at = 0.0
        self.assertEqual(sorted(z.uid for z in sonos.zones(force=True).values()), ['HT', 'TD'])
        self.assertEqual(ha_backend.picked(), set())

    def test_ha_failure_keeps_sonos(self):
        with patch('speakers.backends.ha.HomeAssistantBackend.snapshot', side_effect=RuntimeError('boom')):
            snap = self.snap()
        self.assertTrue(snap['rooms'])
        self.assertTrue(all(r['backend'] == 'sonos' for r in snap['rooms']))

    def test_room_picker_offers_one_system(self):
        snap = self.snap()
        self.assertEqual({r['backend'] for r in snap['rooms']}, {'sonos', 'ha'})
        for g in snap['groups']:
            self.assertEqual({m.split(':')[0] for m in g['members']}, {g['backend']})


class HubFrameLayoutTests(SimpleTestCase):
    """The app renders inside the hub's iframe, already below the hub navbar, so its
    CSS must not reserve space for that navbar (top:62px / 100vh-62px / phone 118px)
    nor scope rules under the hub's `.navbar`, which doesn't exist in the iframe."""

    def test_no_hub_navbar_offsets(self):
        import pathlib
        import re
        root = pathlib.Path(__file__).resolve().parent.parent
        self.assertTrue((root / "templates").is_dir())
        pattern = re.compile(r"top\s*:\s*(6[0-9]|118)px|100vh\s*-\s*(6[0-9]|118)px|\.navbar(?![-\w])")
        hits = []
        for path in list(root.glob("templates/**/*.html")) + list(root.glob("static/**/*.css")):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    hits.append(f"{path.relative_to(root)}:{n}: {line.strip()}")
        self.assertEqual(hits, [])

import datetime as dt
import json
import os
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from speakers import enforcer, quiet, service
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
        self.is_visible = True
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
    def test_interface_setting(self, discover):
        with patch.dict(os.environ, {'BDH_SPEAKERS_INTERFACE': '10.0.0.5'}):
            sonos._discover_sonos(timeout=1)
        discover.assert_called_once_with(timeout=1, interface_addr='10.0.0.5')

    @override_settings(BDH_SONOS_CONTROL_INTERFACE='10.0.0.9', SONOS_INTERFACE='10.0.0.9')
    @patch('speakers.backends.sonos.soco.discover', return_value=set())
    def test_old_sonos_control_names_are_ignored(self, discover):
        with patch.dict(os.environ, {'BDH_SONOS_CONTROL_INTERFACE': '10.0.0.9', 'SONOS_INTERFACE': '10.0.0.9'}):
            os.environ.pop('BDH_SPEAKERS_INTERFACE', None)
            sonos._discover_sonos()
        discover.assert_called_once_with()

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
            if hasattr(settings, 'BDH_SPEAKERS_PRESETS'):  # the hub's site.toml may set it
                delattr(settings, 'BDH_SPEAKERS_PRESETS')
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
        with patch('speakers.quiet.status', return_value=self.NIGHT):
            sonos.set_room_volume('TD', 80)
            sonos.set_room_volume('DR', 80)
        self.assertEqual(self.zs[1].volume, 25)
        self.assertEqual(self.zs[0].volume, 80)

    def test_night_limit_caps_group_only_when_all_rooms_limited(self):
        with patch('speakers.quiet.status', return_value=self.NIGHT):
            sonos.set_group_volume('HT', 70)
            sonos.set_group_volume('DR', 70)
        self.assertEqual(self.zs[2].group.volume, 25)
        self.assertEqual(self.zs[0].group.volume, 70)

    def test_inactive_night_limit_does_not_cap(self):
        with patch('speakers.quiet.status', return_value=dict(self.NIGHT, active=False)):
            sonos.set_room_volume('TD', 80)
        self.assertEqual(self.zs[1].volume, 80)


QUIET_ENV = {'BDH_SPEAKERS_QUIET_ENABLED': 'true', 'BDH_SPEAKERS_QUIET_START': '22:00',
             'BDH_SPEAKERS_QUIET_END': '07:00', 'BDH_SPEAKERS_QUIET_LIMIT': '25',
             'BDH_SPEAKERS_QUIET_ROOMS': '["Top Deck", "Hot Tub"]'}


def at(h, m=0, day=18):
    return dt.datetime(2026, 11, day, h, m, tzinfo=dt.timezone.utc)


class QuietHoursTests(TestCase):
    def setUp(self):
        quiet.app_state(quiet.APP_ID).delete(quiet.OVERRIDE_KEY)
        quiet.invalidate()
        env = patch.dict(os.environ, QUIET_ENV)
        env.start()
        self.addCleanup(env.stop)

    def test_window_crossing_midnight(self):
        s, e = dt.time(22), dt.time(7)
        self.assertEqual(quiet.window(at(23), s, e), (True, at(7, day=19)))
        self.assertEqual(quiet.window(at(3), s, e), (True, at(7)))
        self.assertEqual(quiet.window(at(7), s, e), (False, None))
        self.assertEqual(quiet.window(at(12), s, e), (False, None))

    def test_same_day_window_and_empty_window(self):
        self.assertEqual(quiet.window(at(13), dt.time(12), dt.time(14)), (True, at(14)))
        self.assertEqual(quiet.window(at(13), dt.time(9), dt.time(9)), (False, None))

    def test_active_inside_window(self):
        q = quiet.compute(at(23))
        self.assertEqual((q['active'], q['limit'], q['rooms']), (True, 25, ['Top Deck', 'Hot Tub']))
        self.assertFalse(quiet.compute(at(12))['active'])

    def test_off_when_disabled_or_no_rooms(self):
        with patch.dict(os.environ, {'BDH_SPEAKERS_QUIET_ENABLED': 'false'}):
            self.assertIsNone(quiet.compute(at(23)))
        with patch.dict(os.environ, {'BDH_SPEAKERS_QUIET_ROOMS': '[]'}):
            self.assertIsNone(quiet.compute(at(23)))

    def test_lift_lasts_until_the_window_ends(self):
        quiet.lift(True, now=at(23))
        self.assertTrue(quiet.compute(at(23, 30))['lifted'])
        self.assertFalse(quiet.compute(at(23, 30))['active'])
        self.assertTrue(quiet.compute(at(6, 59, day=19))['lifted'])
        # The next night the limit is back on its own.
        self.assertTrue(quiet.compute(at(23, day=19))['active'])

    def test_restore_after_lift(self):
        quiet.lift(True, now=at(23))
        quiet.lift(False)
        self.assertTrue(quiet.compute(at(23, 30))['active'])

    def test_lift_outside_window_is_refused(self):
        with self.assertRaises(quiet.QuietError):
            quiet.lift(True, now=at(12))

    def test_cap_for_needs_every_room_limited(self):
        with patch('speakers.quiet.status', return_value=quiet.compute(at(23))):
            self.assertEqual(quiet.cap_for(['Top Deck', 'Hot Tub']), 25)
            self.assertIsNone(quiet.cap_for(['Top Deck', 'Dining Room']))
            self.assertIsNone(quiet.cap_for([]))

    def test_bad_setting_means_no_limit(self):
        with patch('speakers.quiet.compute', side_effect=ValueError('bad')):
            self.assertIsNone(quiet.status(force=True))


class QuietViewTests(TestCase):
    def setUp(self):
        quiet.app_state(quiet.APP_ID).delete(quiet.OVERRIDE_KEY)
        self.addCleanup(quiet.app_state(quiet.APP_ID).delete, quiet.OVERRIDE_KEY)

    def post(self, body):
        return self.client.post(reverse('speakers_action'), json.dumps(body), content_type='application/json')

    @patch('speakers.views.quiet.lift')
    def test_only_admins_can_lift(self, lift):
        self.assertEqual(self.post({'action': 'quiet_override', 'lift': True}).status_code, 403)
        lift.assert_not_called()

    @patch('speakers.views.enforcer.wake')
    @patch('speakers.views.service.snapshot', return_value={'rooms': [], 'groups': [], 'presets': []})
    @patch('speakers.views.quiet.lift')
    @patch('speakers.views.is_admin', return_value=True)
    def test_admin_lifts_and_restores(self, admin, lift, snap, wake):
        res = self.post({'action': 'quiet_override', 'lift': True})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['admin'])
        lift.assert_called_once_with(True)
        wake.assert_not_called()
        self.post({'action': 'quiet_override', 'lift': False})
        lift.assert_called_with(False)
        wake.assert_called_once()

    @patch('speakers.views.quiet.lift', side_effect=quiet.QuietError("Quiet hours aren't on right now"))
    @patch('speakers.views.is_admin', return_value=True)
    def test_lift_outside_window_is_a_400(self, admin, lift):
        res = self.post({'action': 'quiet_override', 'lift': True})
        self.assertEqual(res.status_code, 400)
        self.assertIn("aren't on", res.json()['error'])

    @patch('speakers.views.is_admin', return_value=True)
    def test_lift_must_be_a_bool(self, admin):
        self.assertEqual(self.post({'action': 'quiet_override', 'lift': 'yes'}).status_code, 400)

    @patch('speakers.views.service.snapshot', return_value={'rooms': [], 'groups': [], 'presets': []})
    def test_state_says_not_admin(self, snap):
        self.assertFalse(self.client.get(reverse('speakers_state')).json()['admin'])


class EnforcerTests(TestCase):
    def setUp(self):
        self.zs = household()
        self.addCleanup(enforcer._subs.clear)

    def test_does_not_start_under_tests(self):
        self.assertFalse(enforcer.should_start(['manage.py', 'test']))
        self.assertTrue(enforcer.should_start(['/x/bdh_sdk/runtime/__main__.py']))

    def test_sweep_turns_limited_rooms_down(self):
        self.zs[1].volume = 60
        self.zs[0].volume = 60
        night = {'active': True, 'limit': 25, 'rooms': ['Top Deck']}
        with patch('speakers.quiet.status', return_value=night), \
                patch('speakers.backends.sonos._discover_all', return_value={z.uid: z for z in self.zs}), \
                patch('speakers.enforcer._subscribe') as sub:
            enforcer.sweep()
        self.assertEqual((self.zs[1].volume, self.zs[0].volume), (25, 60))
        sub.assert_called_once_with('Top Deck', self.zs[1])

    def test_event_over_the_limit_turns_down(self):
        night = {'active': True, 'limit': 25, 'rooms': ['Top Deck']}
        with patch('speakers.quiet.status', return_value=night), \
                patch('speakers.backends.sonos._pool') as pool:
            enforcer._on_event('Top Deck', self.zs[1])(MagicMock(variables={'volume': {'Master': '70'}}))
            pool.submit.assert_called_once_with(enforcer._limit_zone, self.zs[1], 25)
            pool.submit.reset_mock()
            enforcer._on_event('Top Deck', self.zs[1])(MagicMock(variables={'volume': {'Master': '20'}}))
            pool.submit.assert_not_called()

    def test_sweep_drops_subscriptions_when_limit_ends(self):
        sub = MagicMock()
        enforcer._subs['Top Deck'] = sub
        with patch('speakers.quiet.status', return_value=None):
            enforcer.sweep()
        sub.unsubscribe.assert_called_once()
        self.assertEqual(enforcer._subs, {})

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


class HomeTheatreTests(TestCase):
    """A soundbar's bonded sub and surrounds are part of its room, never group members."""

    def setUp(self):
        self.zs = household()
        bar = self.zs[3]
        self.sats = [FakeZone('Office', 'SL'), FakeZone('Office', 'SR'), FakeZone('Sub 4', 'SUB')]
        for s in self.sats:
            s.is_visible = False
            s.unjoin = MagicMock(side_effect=Exception('UPnP Error 800'))
            s.join = MagicMock(side_effect=Exception('UPnP Error 800'))
        FakeGroup(bar, [bar, *self.sats])
        HOUSEHOLD.extend(self.sats)
        sonos._zones.clear()
        sonos._zones.update({z.uid: z for z in self.zs})
        sonos._zones_at = float('inf')
        self.addCleanup(reset)

    def test_snapshot_shows_the_soundbar_alone(self):
        snap = sonos.snapshot(force=True)
        groups = {g['id']: g['members'] for g in snap['groups']}
        self.assertEqual(groups['OF'], ['OF'])
        self.assertEqual(sorted(r['uid'] for r in snap['rooms']), ['DR', 'HT', 'OF', 'TD'])

    def test_ungroup_leaves_satellites_alone(self):
        sonos.ungroup('OF')
        for s in self.sats:
            s.unjoin.assert_not_called()

    def test_ungroup_all_skips_home_theatre_and_splits_real_groups(self):
        sonos.ungroup_all()
        for s in self.sats:
            s.unjoin.assert_not_called()
        self.assertEqual(self.zs[1].group.members, [self.zs[1]])

    def test_soundbar_joins_without_handing_off_to_its_sub(self):
        sonos.set_rooms('DR', ['DR', 'TD', 'OF'])
        self.zs[3].avTransport.DelegateGroupCoordinationTo.assert_not_called()
        self.assertIs(self.zs[3].group.coordinator, self.zs[0])

    def test_preset_with_home_theatre(self):
        sonos.set_group(['Office', 'Hot Tub'])
        self.zs[3].avTransport.DelegateGroupCoordinationTo.assert_not_called()
        for s in self.sats:
            s.unjoin.assert_not_called()
        self.assertIs(self.zs[3].group.coordinator, self.zs[2])


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
        p = patch('speakers.quiet.status', return_value=None)
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


# ---------------------------------------------------------------- assistant tools

from bdh_sdk import ALL_MODES  # noqa: E402
from bdh_sdk.tools import ToolError  # noqa: E402

from speakers import assistant, homepage, music  # noqa: E402
from speakers.matching import Ambiguous, NoMatch, Rooms  # noqa: E402


def a_room(uid, label, floor, group=None, volume=30, name=None, **extra):
    return {'uid': f'sonos:{uid}', 'label': label, 'name': name or label, 'floor': floor,
            'group': f'sonos:{group or uid}', 'shown': True, 'volume': volume, 'muted': False,
            'online': True, 'can_group': True, 'backend': 'sonos', **extra}


def a_group(uid, members, state='STOPPED', title='', artist='', volume=30, tv=False):
    return {'id': f'sonos:{uid}', 'members': [f'sonos:{m}' for m in members], 'state': state,
            'title': title, 'artist': artist, 'album': '', 'art': '', 'volume': volume, 'tv': tv,
            'source': {'name': 'Radio'} if title else None, 'can_pause': not tv, 'backend': 'sonos'}


def lodge(**states):
    """Dining Room + Top Deck grouped, the rest alone; ``states`` sets group states."""
    rooms = [a_room('DR', 'Dining Room', 'main'), a_room('TD', 'Top Deck', 'outside_main', group='DR'),
             a_room('LD', 'Lower Deck', 'outside_lower'), a_room('HT', 'Hot Tub', 'outside_lower'),
             a_room('ME', 'Master En-suite', 'upper', name='Master Bedroom En-suite'),
             a_room('BE', 'Basement En-suite', 'lower', name='Basement Master En-suite'),
             a_room('LR', 'Living Room', 'main', volume=80)]
    groups = [a_group('DR', ['DR', 'TD']), a_group('LD', ['LD']), a_group('HT', ['HT']),
              a_group('ME', ['ME']), a_group('BE', ['BE']), a_group('LR', ['LR'], volume=80)]
    for g in groups:
        g.update(states.get(g['id'].split(':')[1], {}))
    return {'rooms': rooms, 'groups': groups, 'presets': [{'label': 'Whole Lodge', 'rooms': '*'}]}


class MatchingTests(SimpleTestCase):
    def setUp(self):
        self.rooms = Rooms([{k: r[k] for k in ('uid', 'label', 'name', 'floor')} for r in lodge()['rooms']],
                           [{'label': 'Whole Lodge', 'rooms': '*'}])

    def uids(self, text):
        return [u.split(':')[1] for u in (r['uid'] for r in self.rooms.resolve(text))]

    def test_room_floor_and_everything(self):
        self.assertEqual(self.uids('the living room speakers'), ['LR'])
        self.assertEqual(self.uids('master bedroom'), ['ME'])
        self.assertEqual(self.uids('basement'), ['BE'])
        self.assertEqual(self.uids('outside'), ['TD', 'LD', 'HT'])
        self.assertEqual(self.uids('dining room and top deck'), ['DR', 'TD'])
        self.assertEqual(len(self.uids('everywhere')), 7)
        self.assertEqual(len(self.uids('whole lodge')), 7)

    def test_plural_means_all_of_them(self):
        self.assertEqual(self.uids('the decks'), ['TD', 'LD'])

    def test_asr_slips(self):
        self.assertEqual(self.uids('hot tube'), ['HT'])
        self.assertEqual(self.uids('dinning room'), ['DR'])
        self.assertEqual(self.uids('master en suite'), ['ME'])

    def test_ambiguous_asks(self):
        with self.assertRaises(Ambiguous) as cm:
            self.rooms.resolve('the deck')
        self.assertIn('Top Deck and Lower Deck', str(cm.exception))
        with self.assertRaises(Ambiguous):
            self.rooms.resolve('master')

    def test_unknown_lists_rooms(self):
        with self.assertRaises(NoMatch) as cm:
            self.rooms.resolve('kitchen')
        self.assertIn("no speaker called 'kitchen'", str(cm.exception))
        self.assertIn('Hot Tub', str(cm.exception))


@patch('speakers.assistant.time.sleep')
@patch.object(service, 'invalidate')
class AssistantTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.delete(homepage.STATE_KEY)
        self.snap = lodge()
        patcher = patch.object(service, 'snapshot', side_effect=lambda force=False: self.snap)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []
        for name in ('transport', 'set_room_volume', 'set_room_mute', 'play_item', 'ungroup'):
            p = patch.object(service, name, side_effect=lambda *a, _n=name: self.calls.append((_n, *a)))
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(service, 'set_rooms', side_effect=self._set_rooms)
        p.start()
        self.addCleanup(p.stop)

    def _set_rooms(self, room, rooms):
        self.calls.append(('set_rooms', room, list(rooms)))
        return room

    def run_tool(self, tool, mode='guest', **args):
        return assistant.run_tool(tool, args, mode=mode)

    def test_tools_declared(self, *_):
        tools = {t.id: t for t in assistant.tools('guest')}
        self.assertEqual(set(tools), {'speaker_status', 'play_music', 'media_control', 'set_volume',
                                      'group_speakers'})
        for t in tools.values():
            self.assertEqual(t.modes, ALL_MODES)
            self.assertFalse(t.confirm)
            self.assertEqual(t.readonly, t.id == 'speaker_status')
        self.assertIn('Hot Tub', tools['play_music'].params['properties']['speakers']['description'])
        self.assertEqual(tools['play_music'].params['required'], ['speakers'])

    def test_status_compact(self, *_):
        self.snap = lodge(DR={'state': 'PLAYING', 'title': 'So What', 'artist': 'Miles Davis',
                              'source': {'name': 'Radio'}})
        text = self.run_tool('speaker_status')
        self.assertTrue(text.startswith('Dining Room + Top Deck: playing So What by Miles Davis (Radio), 30%.'), text)
        self.assertIn('Off: Lower Deck, Hot Tub', text)
        self.assertEqual(self.run_tool('speaker_status', speakers='living room'), 'Living Room: off, 80%.')

    def test_status_nothing_playing(self, *_):
        self.assertTrue(self.run_tool('speaker_status').startswith('Nothing is playing. The speakers are in Dining Room'))

    def test_play_favorite_on_existing_group(self, *_):
        fav = music.Item('Jazz24', 'x-sonosapi-stream:s34682?sid=254', '', 'favorite')
        with patch.object(music, 'favorites', return_value=[fav]), \
                patch.object(music, 'search_station') as search:
            text = self.run_tool('play_music', speakers='dining room and top deck', what='jazz24 radio')
        search.assert_not_called()
        self.assertEqual(self.calls, [('play_item', 'sonos:DR', fav)])
        self.assertEqual(text, 'Playing Jazz24 in the Dining Room and Top Deck.')

    def test_play_searches_tunein_and_groups_rooms(self, *_):
        found = music.station('s249973', 'Smooth Jazz')
        with patch.object(music, 'favorites', return_value=[]), \
                patch.object(music, 'search_station', return_value=found) as search:
            text = self.run_tool('play_music', speakers='hot tub, lower deck', what='some jazz music')
        search.assert_called_once_with('jazz')
        self.assertEqual(self.calls, [('set_rooms', 'sonos:LD', ['sonos:LD', 'sonos:HT']),
                                      ('play_item', 'sonos:LD', found)])
        self.assertEqual(text, 'Playing Smooth Jazz in the Lower Deck and Hot Tub.')

    def test_play_nothing_named_resumes(self, *_):
        self.snap = lodge(HT={'state': 'PAUSED_PLAYBACK', 'title': 'So What'})
        self.assertEqual(self.run_tool('play_music', speakers='hot tub'), 'Resuming So What in the Hot Tub.')
        self.assertEqual(self.calls, [('transport', 'sonos:HT', 'play')])

    @override_settings(BDH_SPEAKERS_DEFAULT_MUSIC='Bluegrass')
    def test_play_some_music_uses_default(self, *_):
        with patch.object(music, 'find', return_value=music.station('s1', 'Bluegrass Radio')) as find:
            self.run_tool('play_music', speakers='hot tub', what='some music')
        find.assert_called_once_with('Bluegrass')

    def test_play_unknown_music(self, *_):
        with patch.object(music, 'favorites', return_value=[]), \
                patch.object(music, 'search_station', return_value=None):
            with self.assertRaises(ToolError):
                self.run_tool('play_music', speakers='hot tub', what='zzz')
        self.assertEqual(self.calls, [])

    def test_play_turns_a_loud_room_down_for_guests(self, *_):
        with patch.object(music, 'find', return_value=music.station('s1', 'Jazz')):
            text = self.run_tool('play_music', speakers='living room', what='jazz')
            self.calls.clear()
            self.run_tool('play_music', mode='owner', speakers='living room', what='jazz')
        self.assertIn('turned the Living Room down to 60%', text)
        self.assertNotIn('set_room_volume', [c[0] for c in self.calls])

    def test_ambiguous_room_asks(self, *_):
        with self.assertRaises(ToolError) as cm:
            self.run_tool('play_music', speakers='the deck', what='jazz')
        self.assertIn('Top Deck and Lower Deck', str(cm.exception))

    def test_volume_capped_for_guests(self, *_):
        text = self.run_tool('set_volume', speakers='hot tub', level=90)
        self.assertEqual(self.calls, [('set_room_volume', 'sonos:HT', 60)])
        self.assertEqual(text, 'The Hot Tub is at 60%. 60% is as loud as I can set it.')
        self.calls.clear()
        self.run_tool('set_volume', mode='owner', speakers='hot tub', level=90)
        self.assertEqual(self.calls, [('set_room_volume', 'sonos:HT', 90)])

    def test_volume_up_never_lowers_a_loud_room(self, *_):
        self.assertIn('as loud as I can', self.run_tool('set_volume', speakers='living room', change='up'))
        self.assertEqual(self.calls, [])
        self.run_tool('set_volume', speakers='living room', change='down', by=5)
        self.assertEqual(self.calls, [('set_room_volume', 'sonos:LR', 75)])

    @override_settings(BDH_SPEAKERS_ASSISTANT_MAX_VOLUME=40)
    def test_volume_cap_setting(self, *_):
        self.run_tool('set_volume', speakers='hot tub', level=50)
        self.assertEqual(self.calls, [('set_room_volume', 'sonos:HT', 40)])

    def test_volume_follows_the_room_playing(self, *_):
        self.snap = lodge(DR={'state': 'PLAYING', 'title': 'x'})
        self.assertEqual(self.run_tool('set_volume', change='up'), 'The Dining Room and Top Deck are at 40%.')
        self.assertEqual(self.calls, [('set_room_volume', 'sonos:DR', 40), ('set_room_volume', 'sonos:TD', 40)])

    def test_volume_quiet_hours_hold(self, *_):
        night = {'active': True, 'limit': 25, 'end': '07:00', 'rooms': ['Hot Tub']}
        with patch('speakers.quiet.status', return_value=night), \
                patch('speakers.quiet.cap_for', side_effect=lambda names: 25 if 'Hot Tub' in names else None):
            text = self.run_tool('set_volume', speakers='hot tub', level=50)
        self.assertEqual(self.calls, [('set_room_volume', 'sonos:HT', 25)])
        self.assertIn('Quiet hours hold the Hot Tub to 25% until 07:00', text)

    def test_pause_everything_then_resume(self, *_):
        self.snap = lodge(DR={'state': 'PLAYING', 'title': 'x'}, HT={'state': 'PLAYING', 'title': 'y'},
                          LR={'state': 'PLAYING', 'title': 'TV audio', 'tv': True})
        text = self.run_tool('media_control', action='stop')
        self.assertEqual(self.calls, [('transport', 'sonos:DR', 'pause'), ('transport', 'sonos:HT', 'pause')])
        self.assertEqual(text, 'Stopped the music in the Dining Room, Top Deck and Hot Tub.')
        self.calls.clear()
        self.snap = lodge(DR={'state': 'PAUSED_PLAYBACK', 'title': 'x'}, HT={'state': 'PAUSED_PLAYBACK', 'title': 'y'},
                          BE={'state': 'PAUSED_PLAYBACK', 'title': 'old'})
        self.run_tool('media_control', action='resume')
        self.assertEqual(sorted(self.calls), [('transport', 'sonos:DR', 'play'), ('transport', 'sonos:HT', 'play')])

    def test_skip_needs_one_room_playing(self, *_):
        self.snap = lodge(DR={'state': 'PLAYING', 'title': 'x'}, HT={'state': 'PLAYING', 'title': 'y'})
        with self.assertRaises(ToolError) as cm:
            self.run_tool('media_control', action='next')
        self.assertIn('Which one?', str(cm.exception))
        self.assertEqual(self.run_tool('media_control', action='next', speakers='hot tub'),
                         'Skipped to the next track in the Hot Tub.')
        self.assertEqual(self.calls, [('transport', 'sonos:HT', 'next')])
        with self.assertRaises(ToolError) as cm:
            self.run_tool('media_control', action='next', speakers='lower deck')
        self.assertEqual(str(cm.exception), 'The Lower Deck is not playing anything.')

    def test_pause_tv_reports_why(self, *_):
        self.snap = lodge(LR={'state': 'PLAYING', 'tv': True})
        service.transport.side_effect = SpeakerError('This speaker is playing the TV')
        with self.assertRaises(ToolError) as cm:
            self.run_tool('media_control', action='pause', speakers='living room')
        self.assertIn('playing the TV', str(cm.exception))

    def test_group_join_and_separate(self, *_):
        self.snap = lodge(HT={'state': 'PLAYING', 'title': 'So What'})
        text = self.run_tool('group_speakers', action='join', speakers='lower deck with the hot tub')
        self.assertEqual(self.calls, [('set_rooms', 'sonos:HT', ['sonos:HT', 'sonos:LD'])])
        self.assertEqual(text, 'The Hot Tub and Lower Deck now play together, playing So What.')
        self.calls.clear()
        self.snap = lodge()
        self.assertEqual(self.run_tool('group_speakers', action='separate', speakers='top deck'),
                         'The Top Deck plays on its own now.')
        self.assertEqual(self.calls, [('set_rooms', 'sonos:DR', ['sonos:DR'])])

    def test_speakers_down(self, *_):
        service.snapshot.side_effect = RuntimeError('boom')
        with self.assertRaises(ToolError) as cm:
            self.run_tool('speaker_status')
        self.assertEqual(str(cm.exception), assistant.DOWN)


class MusicTests(SimpleTestCase):
    def test_station_uri_and_meta(self):
        item = music.station('s249973', 'Smooth & Jazz')
        self.assertEqual(item.uri, 'x-sonosapi-stream:s249973?sid=254&flags=8224&sn=0')
        self.assertIn('id="F00092020s249973"', item.meta)
        self.assertIn('Smooth &amp; Jazz', item.meta)

    def test_search_picks_a_station(self):
        results = [MagicMock(item_id='0fffffffp123', title='A show'),
                   MagicMock(item_id='0fffffffs249973', title='Smooth Jazz')]
        service_ = MagicMock()
        service_.search.return_value = results
        with patch.object(music, '_tunein', return_value=service_):
            self.assertEqual(music.search_station('jazz').uri.split('?')[0], 'x-sonosapi-stream:s249973')

    def test_query_drops_vague_words(self):
        self.assertEqual(music.query('some jazz music please'), 'jazz')
        self.assertEqual(music.query('some music'), '')


class SonosPlayItemTests(TestCase):
    def setUp(self):
        self.zs = household()
        sonos._zones.update({z.uid: z for z in self.zs})
        sonos._zones_at = float('inf')

    def tearDown(self):
        reset()

    def test_plays_on_the_coordinator(self):
        self.zs[0].play_uri = MagicMock()
        sonos.play_item('TD', music.station('s1', 'Jazz'))
        self.zs[0].play_uri.assert_called_once()
        self.assertEqual(self.zs[0].play_uri.call_args[0][0], 'x-sonosapi-stream:s1?sid=254&flags=8224&sn=0')

    def test_leaves_a_soundbar_on_the_tv(self):
        tv = self.zs[3]
        tv.transport = 'PLAYING'
        tv.play_uri = MagicMock()
        tv.get_current_media_info = lambda: {'uri': 'x-sonos-htastream:RINCON_1:spdif'}
        with self.assertRaises(SpeakerError):
            sonos.play_item('OF', music.station('s1', 'Jazz'))
        tv.play_uri.assert_not_called()

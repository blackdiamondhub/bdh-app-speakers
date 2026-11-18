from bdh_sdk import PluginSpec, Setting, Tile
from bdh_sdk.plugins import BOOL, INT, LIFECYCLE_ALWAYS, MULTI, NETWORK_LAN, STR, TIME

spec = PluginSpec(
    id="speakers",
    app="speakers",
    urls="speakers.urls",
    status="speakers.homepage.status",
    run_action="speakers.homepage.run_action",
    tiles=(
        Tile(
            title="Speakers",
            url_name="speakers",
            icon="speakers/hub/icon.svg",
            section="lodge",
            subtitle="Group rooms, volume and play/pause",
            order=20,
        ),
    ),
    settings=(
        Setting("backends", "Speaker systems", type=MULTI, default=["sonos", "ha"],
                choices=(("sonos", "Sonos"), ("ha", "Home Assistant")),
                help="Sonos is found on the LAN; Home Assistant lists the media players of "
                     "every Home Assistant the hub knows. A Home Assistant player with the "
                     "same name as a Sonos room is left out."),
        Setting("speakers", "Speakers to show", type=MULTI, default=None,
                choices_from="speakers.service.speaker_choices",
                help="Rooms this hub lists and controls. New speakers stay hidden until ticked. "
                     "Until this is saved, the hub shows the Sonos Amps and every Home "
                     "Assistant media player."),
        Setting("pause_grace", "Keep paused music on the homepage", type=INT, default=5,
                minimum=0, maximum=60, unit="min",
                help="How long a paused group stays on the Speakers tile so it can be resumed."),
        Setting("models", "Sonos: models to show", type=STR, default="Amp",
                help="Sonos only. Comma-separated model names shown until Speakers to show "
                     "is saved; * shows every Sonos."),
        Setting("interface", "Sonos: network interface", type=STR, default="",
                help="Sonos only. IP of the hub's interface on the Sonos network, when "
                     "discovery picks the wrong one."),
        Setting("quiet_enabled", "Quiet hours", type=BOOL, default=False,
                help="Limit the volume of the rooms below between the two times, e.g. "
                     "outdoor speakers at night. Applies to changes from the Sonos app "
                     "too. An admin can lift it for the rest of the night from the "
                     "Speakers page."),
        Setting("quiet_start", "Quiet hours: start", type=TIME, default="22:00"),
        Setting("quiet_end", "Quiet hours: end", type=TIME, default="07:00",
                help="May be after midnight."),
        Setting("quiet_limit", "Quiet hours: volume limit", type=INT, default=25,
                minimum=0, maximum=100, unit="%"),
        Setting("quiet_rooms", "Quiet hours: rooms", type=MULTI, default=None,
                choices_from="speakers.quiet.room_choices",
                help="Sonos rooms held to the limit."),
    ),
    # Home Assistant media players: read their state, call media_player services,
    # and fetch album art through HA's media_player proxy.
    permissions=("ha.read:media_player.*", "ha.call:media_player.*", "media"),
    api_version=2,
    # soco finds speakers by SSDP multicast and talks to each one over HTTP on the LAN.
    network=NETWORK_LAN,
    # The Speakers tile shows what's playing right now, so the app has to be running.
    lifecycle=LIFECYCLE_ALWAYS,
    memory_mb=128,
)
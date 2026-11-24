# bdh-app-speakers

Black Diamond Hub app for a site's speakers: Sonos (through soco, on the LAN)
and Home Assistant `media_player` entities (on every Home Assistant the hub
knows about). It was `bdh-app-sonos-control` (`sonos_control`) until 3.0.0.

Every room or group is a player card (playing ones first, idle ones as compact
rows) with play/pause/skip and volume; group cards expand to per-room volume
and mute. Each card's Rooms button opens a picture of the house (or a plain
grid of rooms when no floors are configured) where you tick the rooms that
play together (a room already in another group can be moved after a
confirmation). Only rooms of the same system can be grouped: the picker for a
Sonos group lists Sonos rooms, the picker for a Home Assistant group lists
players of that Home Assistant that support grouping, and the server refuses
mixed groups. Players that can't be grouped have no Rooms button. Quick
groups (configured per site), Ungroup all and Pause all sit on top, and every
group card has an Ungroup button. A soundbar playing its TV (or a line-in)
gets volume only, and Pause all and the homepage tile leave it alone. Guests
play music from their own phones (Spotify Connect, AirPlay and so on).
Installed into the hub through the `blackdiamondhub.plugins` entry point
(see [bdh-sdk](https://github.com/blackdiamondhub/bdh-sdk)).

Page: `/speakers/`. The page polls `state/` every 2 seconds and sends changes
to `action/`. Every screen shares one cached snapshot, so the speakers see the
same traffic however many screens are open. Home Assistant album art is
fetched by the app (`art/?room=…`) so the browser never talks to Home
Assistant.

On the hub's homepage the Speakers tile lists the groups that are playing,
each with play/pause and skip, and a pause-everything button (which becomes
Resume). Paused groups stay on the tile, dimmed, for `pause_grace` minutes so
a quick pause can be undone there; groups that were already paused are never
listed, since idle Sonos speakers sit paused for months.

## Backends

`speakers/backends/` holds one class per system, all implementing
`backends.base.Backend`: `snapshot()` (rooms and groups), `choices()` (for the
Settings page), `transport`, `set_group_volume`, `set_room_volume`,
`set_room_mute`, `set_members` / `set_group` / `ungroup` / `ungroup_all`
(grouping), `pause_all`, `art` and a `supports` set (`grouping`, `night`).
`speakers/service.py` merges the enabled backends: ids the browser sees are
`<backend>:<local id>`, i.e. `sonos:<RINCON uid>` and
`ha:<instance id>:<entity_id>`. Ids without a prefix (saved before 1.0) are
Sonos. A backend that fails is logged and left out; the others keep working.

- **Sonos**: discovery by SSDP, `models` filter, `interface`, source
  detection, TV/line-in can't pause, night limit, coordinator hand-off when
  the coordinator leaves a group. Unchanged from `sonos_control` 2.7.
- **Home Assistant**: every `media_player` on every instance from
  `bdh_sdk.ha.instances()`. `supported_features` decides what a player can do
  (pause, volume, mute, next/previous, grouping); grouping uses
  `group_members` and the `join` / `unjoin` services. `unavailable` players are
  offline, anything not playing/paused is stopped. When the Sonos backend is
  on, Home Assistant players named like a Sonos room (case-insensitive) are
  hidden, since Home Assistant's Sonos integration would list every Sonos
  room twice.

## Settings

Each reads the Settings page, then the environment variable, then the Django
setting of the same name; the hub fills the Django settings from
`[apps.speakers]` in its `sites/<site>/site.toml`. The old `sonos_control`
names (`BDH_SONOS_CONTROL_*`, `SONOS_INTERFACE`, `[apps.sonos_control]`) are
no longer read (since 3.1).

- `BDH_SPEAKERS_BACKENDS`: Speaker systems, `sonos` and/or `ha` (default
  both).
- `BDH_SPEAKERS_SPEAKERS`: Speakers to show. On the Settings page it is one
  checklist of every room of every system, labelled `Name · Sonos` or
  `Name · Home Assistant (<instance>)` (ticked rooms that are offline stay
  listed). Sonos values are bare room uids (as before 1.0), Home Assistant
  values are `ha:<instance>:<entity_id>`. New speakers stay hidden until
  ticked. A group that mixes ticked and unticked rooms shows the unticked
  ones greyed out, without controls; ungrouping such a group pauses an
  unticked Sonos room left playing. Until this is saved, Sonos uses `models`
  and every Home Assistant player is shown.
- `BDH_SPEAKERS_ROOMS`: `{room name: {label, floor, icon}}` (JSON in the
  environment), for rooms of any system. `floor` is `upper` / `main` /
  `lower` (inside the house picture) or `outside_main` / `outside_lower`. If
  no room has a floor, the room picker is a plain grid. Unlisted rooms use
  their own name.
- `BDH_SPEAKERS_PRESETS`: quick groups,
  `[{id, label, icon, rooms: [room names] or "*"}]` (JSON in the
  environment); each system groups its own matching rooms. Default: one
  "Everywhere" button.
- `BDH_SPEAKERS_PAUSE_GRACE`: minutes a paused group stays on the homepage
  Speakers tile (default 5, also on the Settings page).
- Sonos only:
  - `BDH_SPEAKERS_MODELS`: comma-separated model names to control (default
    `Amp`); `*` controls every Sonos. Ignored once Speakers to show has been
    saved.
  - `BDH_SPEAKERS_INTERFACE`: IP of the interface
    on the Sonos network, for Docker with host networking.
  - Quiet hours (Settings page): `quiet_enabled`, `quiet_start` /
    `quiet_end` (`HH:MM`, the end may be after midnight), `quiet_limit`
    (volume %) and `quiet_rooms` (Sonos rooms). During quiet hours the page
    shows a banner and the sliders of those rooms stop at the limit. The app
    also subscribes to each quiet room's volume events (plus a 30 s sweep),
    so a change from the Sonos app or a phone is turned back down. An admin
    can lift the limit until the end of tonight's window from the banner.

## Container (hub API 2)

The app can run in its own container: `network: lan` (soco finds speakers by
SSDP multicast and talks to each one over HTTP, so the supervisor runs it on
the host network and the admin must approve that), `lifecycle: always` (the
Speakers tile shows what is playing now), `memory_mb: 128`. Broker
permissions: `ha.read:media_player.*` and `ha.call:media_player.*` (Home
Assistant players) and `media` (album art through `ha.get_static`). Settings
come from the hub through the broker. The app has no database tables, so
moving between in-process and container needs no migration.

## Development

CI (`.github/workflows/ci.yml`) uses the reusable `app-ci.yml` from
bdh-sdk. It builds the wheel, installs it next to the hub, then runs
`hub_smoke`, the app tests and the selenium tests.

To release, bump `version` in `pyproject.toml`. When that lands on
`main`, `release.yml` tags it and publishes the wheel, and the release
notes include the hashed line for the hub's `sites/<site>/apps.txt`.

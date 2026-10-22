# Thermostat Schedule

> [!WARNING]
> **Work in progress – not production ready yet.** This integration is under active development.
> Expect bugs and breaking changes (including to the stored data and configuration) between
> versions, and don't rely on it for heating you can't afford to lose.

A Home Assistant integration for HomeMatic-IP-style weekly heating schedules on top of your
existing `climate` entities. Built for Sonoff TRVZB (Zigbee2MQTT), but works with any thermostat
that supports `climate.set_temperature`.

- **Named profiles** per room (e.g. `home`, `away`, `vacation`), each a full weekly schedule with
  **weekday-specific** switch points.
- **Trigger rules** switch profiles automatically (person away, vacation boolean, …). When a rule
  ends, the room returns to whatever base profile you had selected.
- **Hub → Room → Thermostat(s)** layout. Create several hubs (e.g. one per floor).
- **Multiple TRVs per room** share one schedule and stay in sync, including manual changes.
- **Master switch** per hub and **Automatic mode** switch per room (guest in the room? → manual).
- Optional **external temperature sensor** per room, pushed to Sonoff TRVZBs.
- Schedules are editable at any time in the integration's options.

## Installation (HACS)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/chindocaine/ha-thermostat-schedule`
   with category **Integration**.
2. Install **Thermostat Schedule** and restart Home Assistant.
3. **Settings → Devices & services → Add integration → Thermostat Schedule**.

Requires a recent Home Assistant (config subentries; developed against 2026.10).

> **Dashboard card:** a Lovelace card for editing schedules visually is planned. It will live in this
> repository under `custom_components/thermostat_schedule/frontend/` and be registered by the
> integration itself, so a single HACS installation provides both.

## Concepts

### Hub
A hub holds the **profile names**, the **default schedules**, the **global trigger rules**, the
**base profile** and a **master switch**. Add the integration again for another hub. Every hub is
fully independent; a thermostat can belong to only one room across all hubs.

Hub settings (**Configure** on the hub):

- **Default schedules** – one weekly schedule per profile, used by every room (using the room's
  eco/comfort presets).
- **Profiles** – add, rename, delete.
- **Global trigger rules** – apply to all rooms of the hub.

### Room
Added with the **Add room** button on the hub. Room settings (**⋮ → Reconfigure** on the room):

- **Thermostats & settings** – thermostats, eco/comfort temperatures, sync, manual-change behavior,
  external sensor.
- **Schedule overrides** – override single days of a profile for this room; everything else keeps
  following the hub default (changes to the default still reach the room). Overridden days are
  marked with ✎; check *Use hub default* to drop an override, or reset a whole profile.
- **Room-specific rules** – rules that only apply to this room.

All thermostats of a room follow the same schedule.

### Profiles and the effective profile
For each room the profile is resolved in this order:

1. The room's **Profile** select, if pinned (anything other than *Follow hub*).
2. The highest-priority **active rule** – global rules and this room's own rules compete by priority.
3. The hub's **Base profile** select.

Because rules are overlays on the base profile, nothing has to be remembered: when the person
returns home the rule becomes inactive and the room falls back to your selected base profile.

### Rules
A rule says: *while these entities are / are not in these states, use that profile*. Select any
existing entity (person, `input_boolean`, binary sensor, …) and set:

- **Match** – any or all of the entities must satisfy the condition,
- **Entity state** – **is** (one of the listed states) or **is not** (anything except the listed
  states), plus the **state(s)**,
- an optional delay ("only after N minutes"), the target **profile** and a **priority**.

Examples:

| Rule | Entities | Match | State | Profile | Priority |
|---|---|---|---|---|---|
| Nobody home | `person.a`, `person.b` | all | **is not** `home` | `away` | 10 |
| Vacation | `input_boolean.vacation` | any | **is** `on` | `vacation` | 20 |

"**is not** `home`" counts every other zone (work, school, …) as away, not just `not_home`.
Unavailable/unknown entities never match, not even for "is not", so a flaky tracker can't switch a
room to "away".

Global rules live in the hub's settings, room-specific rules in the room's settings.

### Editing schedules
Hub: **Configure → Default schedules**. Room: **Reconfigure → Schedule overrides**. Choose a profile,
then a day. Each day is a list of switch points, one per line as `HH:MM temperature`. The temperature
is either a **preset name** or a **number**:

```
00:00 eco
08:00 comfort
19:00 23
22:30 eco
```

Each temperature applies until the next line; use a dot for half degrees (`21.5`). If there is no
`00:00` line, the last temperature carries over midnight. The inline form
`00:00 eco, 08:00 comfort, 19:00 23, 22:30 eco` is accepted as well. Use **Also apply to** to copy a
day to other days (e.g. Monday → Tuesday–Friday). Changes apply immediately. New profiles start with
`eco` / `comfort` / `eco` (or a copy of an existing profile).

### Temperature presets
Every room has two presets, set in the room's settings: **eco** (default 19 °C) and **comfort**
(default 21.5 °C). Schedules refer to them by name, so one default schedule can serve rooms with
different temperatures (a bathroom with comfort 23 °C, a bedroom with eco 17 °C). Changing a preset
takes effect everywhere it is used. Use a literal number anywhere you need a one-off temperature.

### Manual changes
When someone changes the target on a thermostat, per room you choose:

- **Hold until the next switch point** (default), **hold for a duration**, **hold until resumed**
  (use the *Resume schedule* button), or **ignore** (schedule is restored immediately).

With *Keep thermostats in sync* on, the change is copied to all other thermostats in the room.
A profile change (e.g. a rule triggering) cancels a temporary hold.
Holds are kept in memory: they end when the integration reloads or Home Assistant restarts.

### External temperature sensor (e.g. Sonoff TRVZB / Zigbee2MQTT)
Pick a temperature sensor for the room, then assign, per thermostat, the entities to use. Nothing is
hard-coded to one model:

- **External temperature input** (`number` entity) – receives the sensor value. Required.
- **Temperature sensor mode** (`select` entity) – optional. Leave it empty for thermostats that have
  no such select; then only the value is sent.
- **Option for external / internal sensor** – the values of that select that mean "use the external
  sensor" and "use the internal sensor". Defaults are the TRVZB values `remote_temperature` and
  `local_temperature`; change them for other models.

The entities are pre-selected when they can be detected on the thermostat's device (for the TRVZB:
`number.<trv>_external_temperature_input` and `select.<trv>_temperature_sensor_select`), but you can
pick any entity. The integration then:

- sends sensor updates to the thermostat (ignoring changes below 0.1 K, at most every 30 s),
- sets the mode select to the external option (and repairs it if the thermostat drops back, e.g. the
  TRVZB reporting `remote_source_offline`),
- re-sends the value regularly (heartbeat, default 15 min),
- if the sensor is unavailable for longer than the grace period (default 60 min), switches the
  thermostat back to the internal option so it never regulates on stale data — or keeps sending the
  last value, as configured. When the sensor returns, external mode is restored.

This works independently of the schedule switches.

## Entities

Per hub: `switch` *Heating schedules* (master), `select` *Base profile*, `sensor` *Effective mode*.

Per room: `switch` *Automatic mode*, `select` *Profile* (Follow hub / pinned), `button`
*Resume schedule*, `sensor` *Target temperature* (attributes: scheduled temperature, profile,
override, next change, …), `sensor` *Active profile*, and (diagnostic) *External temperature status*.

### Service `thermostat_schedule.set_override`
Sets a manual room temperature (with optional duration) for a room — target the room's
*Target temperature* sensor.

## Notes & limitations

- The schedule only sets **temperatures**; heat/off modes stay manual. Turn the master switch off
  in summer.
- If Home Assistant is down, TRVs simply keep their last setpoint.
- Schedules are stored in `.storage/thermostat_schedule.<entry_id>`.

## Development

```bash
python3.14 -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
pytest
ruff check . && ruff format --check .
```

See [PLAN.md](PLAN.md) for the design rationale and roadmap.

## License

See [LICENSE](LICENSE).

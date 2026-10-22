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
A hub holds the **profile names**, **trigger rules**, the **base profile** and a **master switch**.
Add the integration again for another hub. Every hub is fully independent; a thermostat can belong to
only one room across all hubs.

### Room
Added with the **Add room** button on the hub. A room has one or more thermostats, a manual-change
behavior, and optionally an external temperature sensor. All thermostats of a room follow the same
schedule.

### Profiles and the effective profile
For each room the profile is resolved in this order:

1. The room's **Profile** select, if pinned (anything other than *Follow hub*).
2. The highest-priority **active rule** (global rules or rules scoped to that room).
3. The hub's **Base profile** select.

If a profile is missing for a room, the first profile of the hub is used.

Because rules are overlays on the base profile, nothing has to be remembered: when the person
returns home the rule becomes inactive and the room falls back to your selected base profile.

### Rules
Options → **Trigger rules**. A rule says: *while these entities have this state, use that profile*.
Select any existing entity (person, `input_boolean`, binary sensor, …); for example:

| Rule | Entities | Match | State | Profile | Priority |
|---|---|---|---|---|---|
| Nobody home | `person.a`, `person.b` | all | `not_home` | `away` | 10 |
| Vacation | `input_boolean.vacation` | any | `on` | `vacation` | 20 |

Unavailable/unknown entities never satisfy a rule. An optional delay ("only after N minutes")
avoids reacting to short absences.

### Editing schedules
Options → **Edit schedules** → room → profile → day. Each day is a list of switch points:

```
06:00 21
22:00 17
```

Each temperature applies until the next line. If there is no `00:00` line, the last temperature
carries over midnight (so the example above has 17 °C until 06:00). Use **Also apply to** to copy a
day to other days (e.g. Monday → Tuesday–Friday). Changes apply immediately.

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

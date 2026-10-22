# Thermostat Schedule – Implementation Plan

Custom Home Assistant integration (domain `thermostat_schedule`) for HomeMatic-IP-style weekly heating
profiles on top of existing `climate` entities (first target: Sonoff TRVZB via Zigbee2MQTT/ZHA).

Status: **MVP implemented (roadmap phases 0–3b).** The card (phase 4) and extra triggers (phase 5) are still open.
See [Implementation status](#9-implementation-status) at the end for how the code deviates from this plan.

---

## 1. Goals / non-goals

**Goals**
- Several named profiles per room ("home", "away", "vacation", …), each a full **weekday-specific** weekly schedule.
- Switch profiles via triggers (person away, calendar, boolean, …); when the trigger ends, return to the previously selected profile *automatically*.
- Works with any existing `climate` entity, not just TRVZB.
- Schedules are easy to edit later (no YAML-only, no re-creating the entry).
- Extensible to room occupancy and other triggers.
- **Multiple hubs** (e.g. one per floor), each with its own profiles, rules, base profile and master switch.
- **Multiple TRVs per room**, kept in sync (schedule *and* manual changes propagate to all TRVs of the room).
- Per-room **auto/manual** switch and a **master on/off** switch per hub (summer mode).
- Optional **external temperature sensor** per room, pushed to the TRVs (TRVZB external sensor mode).
- Latest HA only (no backward-compatibility burden; uses config subentries).

**Non-goals (v1)**: window-open logic, learning/optimization, cooling/HVAC-mode scheduling, per-profile hvac_mode.
The schedule only sets **temperatures**; HVAC modes (heat/off) stay under manual control.

---

## 2. Decisions & recommendations

### 2.1 Wrapper climate entity vs. controller-only

| Option | Pros | Cons |
|---|---|---|
| **A. Controller only (recommended)** – integration reads/writes the existing climate entity via `climate.set_temperature`; exposes its own `select`/`sensor`/`switch` entities | Original entity stays untouched (Z2M/ZHA features, HomeKit, dashboards keep working); no duplicated state; small surface; works with any climate | Two places show a setpoint (device + schedule sensor); need override detection logic |
| B. Wrapper climate entity | One entity per room, can show "scheduled vs. manual" as presets | Duplicates the device; must proxy all features/modes; breaks integrations that reference the original; much more code to maintain |
| C. Built-in `schedule` helpers + user automations | Zero custom code, native UI | One helper per profile per room, no named-profile switching, calendar-style UI not temperature-oriented, lots of manual automation glue |

**Recommendation: A.** A room can target one *or several* climate entities (rooms with 2 TRVs). The setpoint is clamped to the
entity's `min_temp`/`max_temp` and only written when it differs from the current target (avoid Zigbee chatter on battery devices).

I would **not** rely on the TRVZB's on-device schedule: it is firmware/Z2M-version dependent and not generic. HA-side scheduling is
portable (side effect: if HA is down, the TRV just keeps its last setpoint – document this).

### 2.2 Profile switching: config-based rules vs. external automations only

| Option | Pros | Cons |
|---|---|---|
| **Declarative rules in the integration (recommended)** | "Return to previous profile" falls out for free (see 2.3); state-based so restart-safe; no automation boilerplate; visible in UI ("why is it in away mode?") | Must build a rule editor; limited to supported condition types |
| Services only (`set_profile`) + user automations | Maximally flexible, trivial to build | User must implement "remember previous profile" themselves (helpers + automations); fragile across restarts; poor discoverability |
| Hybrid | Rules for 90% case, services for escape hatch | Slightly more surface |

**Recommendation: hybrid** – rules in the config UI **plus** services (`set_profile`, `clear_override`, `set_mode`) so any
automation can drive it too (e.g. for something exotic, or a "party" button).

### 2.3 Model: base mode + prioritized overlays (this is the key idea)

Instead of imperative "switch to X, remember Y", resolve the effective profile *declaratively*:

```
effective_profile = highest-priority ACTIVE rule's profile
                    else  base profile (user-selected: home / vacation / …)
```

- The **base profile** is a normal `select` entity the user (or an automation) sets: `home` ↔ `vacation`.
- **Rules** are overlays with a condition + priority: "person.michael not_home → away (priority 10)".
- Person leaves → rule active → `away`. Person returns → rule inactive → falls back to base (`home` *or* `vacation`, whichever was selected). No memory needed.
- Evaluated from current states on every relevant state change **and at startup** → robust against restarts and missed events.
- Future occupancy = just another rule type with a `for:` delay.

Supported condition types (v1 → later):
1. Entity state (person/device_tracker/binary_sensor/input_boolean/…) equals / not-equals value — **v1**
2. Multiple entities with `any`/`all` (e.g. "all persons away") — **v1**
3. Calendar entity has active event (vacation!) — v1.1
4. Template — v1.1
5. Room occupancy/presence with `for` delay, scoped to one room — v2

### 2.4 Global modes vs. per-room-only

"home / away / vacation" are house-wide concepts, but each room defines its *own* temperatures per mode.

| Option | Pros | Cons |
|---|---|---|
| A. Fully per-room (own profiles, own base select, own rules) | Simplest model; closest to HmIP per-device | "Everyone left" must be configured in every room; N base selects |
| **B. Hub with global modes + rules; rooms hold schedule per profile name (recommended)** | Set up "away" once; one base select for the house; room can still add *room-scoped* rules | Slightly more concept (hub + rooms) |
| C. B plus per-room "follow hub / pin to profile" to ignore the global mode | Covers the guest room that always stays cold | One extra select entity per room |

**Recommendation: B + C** (C is cheap: one `select` per room with options `Follow hub` + all profile names; resolver
simply skips base/rules when a profile is pinned). Structured as one config entry ("Thermostat Schedule" hub) with a
**config subentry per room** (subentries: native "Add room" button, per-room reconfigure, no entry reload for sibling rooms).

**Multiple hubs:** the integration allows any number of hub entries (e.g. "Ground floor", "Upper floor"). Each hub is fully
independent: own profile names, rules, base-profile select, master switch and storage (`.storage/thermostat_schedule.<entry_id>`).
Guard rails: a climate entity (and external sensor target) can belong to only one room across *all* hubs (validated in the
room flow); entity IDs and services are hub-scoped (services take a hub/room entity as target). There is deliberately no
cross-hub "super hub" – if you ever want one switch for everything, use a group/automation on the hub master switches.
Matching is by profile **name**; if a room has no schedule for the effective profile it falls back to the room's
`fallback_profile` (default: first profile) rather than leaving the TRV unmanaged. The UI warns about such gaps.

### 2.5 Where to edit schedules (UI)

| Option | Pros | Cons |
|---|---|---|
| Options/config flow only | No frontend code, ships fast, works on mobile app | Poor for time grids: many steps, no overview; fine for ≤ 6 switch points/day |
| **Custom Lovelace card / panel + websocket API** | Real weekly grid, drag segments, copy day, overview of all rooms | TypeScript/Lit frontend; extra maintenance; must be served by the integration (or HACS plugin) |
| YAML | Easy diffs/versioning | Hostile for daily use; reload needed |
| Import/export JSON via services | Backup, bulk edits, sharing | Not a primary UI |

**Recommendation: two phases.**
1. **Phase 1:** backend + a *good enough* flow-based editor (day-by-day, "copy to other days"). Fully usable, no frontend.
2. **Phase 2:** card + websocket API (`thermostat_schedule/rooms`, `…/get_schedule`, `…/save_schedule`) with grid editor, profile tabs.
   Flow editor stays as fallback. Because the card uses the same storage and validation layer, nothing is thrown away.

Structure (rooms, climate entities, profile names, rules) → config/subentry flows (they're rarely edited).
Schedule data → **own `Store` (`.storage/thermostat_schedule`)**, *not* config entry options: it's large, edited often,
and saving options reloads the entry.

### 2.6 Manual changes on the TRV (override policy)

Detect setpoint changes not caused by us (compare to last written value + context id). Per-room option:

| Policy | Behavior |
|---|---|
| **Until next switch point (default, HmIP "auto with temporary manual")** | Manual value held until the next block starts, then schedule resumes |
| For duration | Held N minutes (`override_minutes`) |
| Until resumed | Schedule paused until `clear_override` / switch turned on |
| Ignore | Schedule re-applies on next reconcile |

**Multi-TRV sync.** All TRVs in a room share one *room setpoint*. If one TRV's target changes and the change was not written by
us (no matching context / expected-value entry), the controller treats it as a manual override of the **room**: it records
the override (per the policy above) and writes the same value to the other TRVs. Details:
- *Echo suppression:* every write we make is registered as "expected value" per TRV (with a short TTL); matching state
  changes are ignored, so propagated writes never trigger further propagation (no ping-pong).
- *Debounce (~2 s):* if several TRVs report changes at once (e.g. a dial turned while another syncs), the first
  external change wins, later ones inside the window are ignored.
- *Rounding:* compare with 0.5 K tolerance to avoid loops on devices that round differently.
- Sync stays active in **manual** room mode too (a manual change on one TRV still propagates); it can be turned off per room
  (`sync_thermostats`, default on).

### 2.7 Switches: master (per hub) and auto/manual (per room)

| Switch | Off means |
|---|---|
| `switch.<hub>_enabled` (master, per hub) | Controllers stop writing setpoints; rules are not evaluated (effective mode shows `disabled`); base profile is remembered. Turning on reconciles immediately. The external-temperature feature (2.8) is **unaffected** (it is not a schedule function). |
| `switch.<room>_auto` (per room) | Room is purely manual: no schedule writes, overrides irrelevant. Multi-TRV sync and external sensor keep working. Turning on resumes the schedule immediately. |

State of both switches is restored across restarts (`RestoreEntity`). A room is "actively scheduled" only if master **and** room auto are on.

### 2.8 External temperature sensor (TRVZB via Zigbee2MQTT) – optional per room

Each room may reference **one** temperature `sensor` entity. For every TRV in the room the controller:
1. On each sensor update (throttled: ignore changes < 0.1 K, minimum interval ~30 s to spare TRV batteries) writes the value to the
   TRV's external temperature input.
2. Ensures the TRV's temperature source is set to *external* (checked on setup, on TRV `unavailable → available`, and
   periodically; only written when it differs).
3. Sends a **heartbeat** re-write of the last value (e.g. every 15 min) even if unchanged, because TRVZB firmware falls back
   to its internal sensor when it hasn't received an external value for a while (**to verify on the real device; the timeout
   value is not documented reliably**).
4. If the sensor goes `unavailable`/`unknown` for longer than a configurable grace period (default 60 min): stop sending
   and switch the TRV back to *internal* so it never regulates on stale data; switch to external again once the sensor recovers.

Z2M exposes the relevant controls as MQTT-discovered HA entities (expected: a `number` for the external temperature input
and a `select` for the temperature sensor type – **exact entity names/options must be verified against your TRVZB + Z2M version**).
Design: an abstraction `ExternalSensorDriver` with a first implementation `Z2MSonoffTrvzbDriver`; the room flow lets you pick the
`number` and `select` entities per TRV (auto-suggested from the climate entity's device via the entity registry – siblings on the
same device). Alternative drivers (ZHA, direct MQTT publish to `zigbee2mqtt/<name>/set`) can be added later without touching the
controller. Direct-MQTT is more robust against entity renames but couples to the MQTT integration; start with entity-based.

---

## 3. Data model

```jsonc
// .storage/thermostat_schedule  (versioned for migrations)
{
  "version": 1,
  "rooms": {
    "<subentry_id>": {
      "fallback_profile": "home",
      "profiles": {
        "home": {
          "mon": [ {"start": "00:00", "temp": 17.0}, {"start": "06:00", "temp": 21.0}, {"start": "22:00", "temp": 17.0} ],
          "tue": [ ... ], "wed": [ ... ], "thu": [ ... ], "fri": [ ... ],
          "sat": [ {"start": "00:00", "temp": 17.0}, {"start": "08:00", "temp": 21.0}, {"start": "23:00", "temp": 17.0} ],
          "sun": [ ... ]
        },
        "away": { ... },
        "vacation": { ... }
      }
    }
  }
}
```

Rules (hub config entry options):

```jsonc
{ "id": "r1", "name": "Nobody home", "profile": "away", "priority": 10, "scope": "global",   // or {"room": "<id>"}
  "condition": { "type": "state", "mode": "all", "entities": ["person.michael"], "state": "not_home", "for": null } }
```

Validation: first point of each day is `00:00` (UI auto-inserts); strictly increasing times; ≤ 12 points/day (soft limit, HmIP-like);
temps 4–35 °C, step 0.5 (clamped to entity limits at apply time); every profile defines all 7 days (new profile = copy of an existing one).
The UI offers "Mon–Fri / Sat–Sun" shortcuts, but days are **stored individually**.

---

## 4. Runtime architecture

```
custom_components/thermostat_schedule/
  manifest.json  hacs.json  const.py
  __init__.py            # setup hub, load store, create controllers, register services/websocket/frontend
  config_flow.py         # hub flow + room subentry flow + options (profiles, rules, schedule editor v1)
  models.py              # dataclasses + validation (pure python)
  schedule.py            # pure: effective_setpoint(profile, dt), next_change(profile, dt) – DST-safe, HA timezone
  resolver.py            # pure: base profile + rules + states -> effective profile (+ reason)
  storage.py             # Store wrapper, migrations
  controller.py          # per-room: listens, reconciles, applies, override detection, multi-TRV sync
  external_sensor.py     # sensor -> TRV external temperature (driver abstraction, throttle, heartbeat, fallback)
  conditions.py          # condition types, entity subscriptions (async_track_state_change_event), `for` timers
  select.py sensor.py switch.py   # entities (below)
  websocket_api.py       # phase 2
  services.yaml  strings.json  translations/{en,de}.json  diagnostics.py
  frontend/              # phase 2 card
tests/                   # pytest-homeassistant-custom-component
```

**Reconcile loop (no polling):** the controller computes `desired = f(effective_profile, now)` and schedules a single
`async_track_point_in_time` for `next_change`. It reconciles on: block boundary, base profile change, rule state change,
schedule edited, switch toggled, HA start, climate entity going `unavailable → available` (TRVs drop off the mesh often),
and override expiry. Pure functions (`schedule.py`, `resolver.py`) hold the logic → easy exhaustive unit tests (DST, week wrap, midnight).

**Entities**
- Per hub: `switch.<hub>_enabled` (master), `select.<hub>_base_profile` (home/vacation/…), `sensor.<hub>_effective_mode` (+ attribute: winning rule).
- Per room: `switch.<room>_auto`, `select.<room>_profile` (`Follow hub` / pinned profile), `button.<room>_resume_schedule`,
  `sensor.<room>_scheduled_temperature` (attrs: profile, reason, next_change, next_temperature, override_until),
  diagnostic `sensor.<room>_external_temperature_status` (only if an external sensor is configured).
- All hub/room entities are attached to a HA *device* per hub/room (so they group nicely in the UI).

**Services:** `set_profile` (base), `set_override` (temp + duration), `clear_override`, `export_schedule`, `import_schedule`.

**Edge cases to cover:** DST transitions; HA offline during a switch point (reconcile on start); climate `hvac_mode: off`
(default: don't turn it on, only manage setpoint; option "manage on/off" later); unavailable entity (retry on return);
setpoint rounding (TRVZB 0.5 steps); TRVs in one room reporting different setpoints at startup (schedule wins if auto,
otherwise the first available TRV's value is adopted as the room value); external-sensor fallback (see 2.8); renamed profile referenced by rules (rename propagates; delete blocked while referenced).

---

## 5. Config dialog mockups

### 5.1 Add integration / hub

```
┌─ Thermostat Schedule ────────────────────────────┐
│  Set up heating schedules                        │
│  Name                [ Upper floor           ]   │
│  (add the integration again for another hub)     │
│                                                  │
│  Initial profiles (comma separated or tick):     │
│   [x] home      [x] away      [x] vacation       │
│   [ ] custom…                                    │
│                                                  │
│  Default (base) profile   [ home            ▾ ]  │
│                                                  │
│                                  [ Submit ]      │
└──────────────────────────────────────────────────┘
```

### 5.2 Hub page (after setup)

```
┌─ Thermostat Schedule ────────────────────────────────────┐
│  Upper floor                                              │
│  Heating schedules   [ ON ]   (master switch)             │
│  Rooms                                      [ + Add room ]│
│   ▸ Home office   2 TRVs · ext. sensor  [AUTO]  ⚙         │
│   ▸ Bedroom       1 TRV                 [MANUAL] ⚙        │
│                                                           │
│  Entities: switch.enabled, select.base_profile,           │
│            sensor.effective_mode                          │
│  [ Configure ]  → Profiles & rules                        │
└──────────────────────────────────────────────────────────┘
```

### 5.3 Add room (subentry)

```
┌─ Add room ───────────────────────────────────────┐
│  Name                [ Home office            ]  │
│  Thermostat(s)       [ climate.office_trv_1 ✕ ]  │
│                      [ climate.office_trv_2 ✕ ]  │
│                      [ + add another climate ]   │
│  [x] Keep thermostats in sync (propagate manual  │
│      changes to all TRVs in this room)           │
│  Manual change behavior                          │
│                      ( ) Ignore                  │
│                      (•) Hold until next switch  │
│                      ( ) Hold for [ 120 ] min    │
│                      ( ) Hold until resumed      │
│  Follow hub profile  [ Follow hub ▾ ]            │
│  Start schedule from [ Default template ▾ ]      │
│                      (Default / Copy room… / Empty)
│                                                  │
│                      [ Submit ]                  │
└──────────────────────────────────────────────────┘
```

### 5.3b Room: external temperature sensor (optional step)

```
┌─ External temperature · Home office ─────────────┐
│  [x] Use an external temperature sensor          │
│  Sensor              [ sensor.office_temp   ▾ ]  │
│                                                  │
│  Per thermostat (auto-detected from device):     │
│   climate.office_trv_1                           │
│     External temp input [ number.office_trv_1_external_temperature_input ▾ ]
│     Sensor type select  [ select.office_trv_1_temperature_sensor_type    ▾ ]
│   climate.office_trv_2   (same)                  │
│                                                  │
│  Advanced                                        │
│   Min. change  [ 0.1 ] K   Min. interval [ 30 ] s│
│   Heartbeat    [ 15 ] min                        │
│   If sensor unavailable for [ 60 ] min →         │
│        ( ) keep last value  (•) fall back to internal sensor
│                          [ Submit ]              │
└──────────────────────────────────────────────────┘
```

### 5.4 Room → menu

```
┌─ Home office ────────────────────────────────────┐
│  What do you want to do?                         │
│   ▸ Edit schedule                                │
│   ▸ Rename / change thermostats                  │
│   ▸ External temperature sensor                  │
│   ▸ Copy schedule from another room              │
│   ▸ Room-specific rules   (0)                    │
│   ▸ Delete room                                  │
└──────────────────────────────────────────────────┘
```

### 5.5 Edit schedule – pick profile & day (flow editor, phase 1)

```
┌─ Edit schedule · Home office ────────────────────┐
│  Profile  [ home ▾ ]   (home ✓ · away ✓ · vacation ⚠ incomplete)
│  Day      [ Monday ▾ ]                           │
│                                                  │
│   00:00 ─ 06:00   17.0 °C                        │
│   06:00 ─ 08:30   21.0 °C                        │
│   08:30 ─ 17:00   21.0 °C                        │
│   17:00 ─ 22:00   21.0 °C                        │
│   22:00 ─ 24:00   17.0 °C                        │
│                                                  │
│  ▸ Edit a block   ▸ Add switch point             │
│  ▸ Copy this day to…   ▸ Done                    │
└──────────────────────────────────────────────────┘
```

### 5.6 Edit switch point / copy day

```
┌─ Edit block ─────────────────┐   ┌─ Copy Monday to… ───────────┐
│  Starts at  [ 06:00 ]        │   │  [x] Tuesday  [x] Wednesday │
│  Temperature [ 21.0 ] °C     │   │  [x] Thursday [x] Friday    │
│  [ Delete block ]            │   │  [ ] Saturday [ ] Sunday    │
│  [ Submit ]                  │   │  [ Submit ]                 │
└──────────────────────────────┘   └─────────────────────────────┘
```

### 5.7 Profiles & rules (hub → Configure)

```
┌─ Profiles & rules ───────────────────────────────┐
│  Profiles   home · away · vacation   [ add / rename / delete ]
│                                                  │
│  Rules (highest priority wins, else base profile)│
│   1. Vacation calendar  → vacation   prio 20  ● inactive
│   2. Nobody home        → away       prio 10  ● ACTIVE
│  [ + Add rule ]                                  │
│                                                  │
│  Currently: base = home → effective = away       │
└──────────────────────────────────────────────────┘
```

### 5.8 Add rule

```
┌─ Add rule ───────────────────────────────────────┐
│  Name         [ Nobody home                    ] │
│  Applies to   (•) All rooms  ( ) Only: [ room ▾ ]│
│  When         [ State of entities ▾ ]            │
│    Entities   [ person.michael ✕ ] [ + ]         │
│    Match      ( ) any  (•) all                   │
│    State      [ not_home ▾ ]                     │
│    For at least [ 00:10:00 ]  (optional)         │
│  Switch to    [ away ▾ ]                         │
│  Priority     [ 10 ]                             │
│                          [ Submit ]              │
└──────────────────────────────────────────────────┘
```

### 5.9 Phase-2 card (concept)

```
 Home office     [ home ] [ away ] [ vacation ]        ● active: away (Nobody home)
 ─────────────────────────────────────────────────────────────────────────────
        00   03   06   09   12   15   18   21   24
 Mon    ░░░░░░▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░      drag edges to move switch points
 Tue    ░░░░░░▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░      click a block → temperature popover
 …                                                [Copy day ▾]  [Mon–Fri ▸ apply]
 Sat    ░░░░░░░░▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░
 Sun    ░░░░░░░░▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░      ░ 17° (eco)   ▓ 21° (comfort)
                                                  [ Save ]  [ Discard ]
```

---

## 6. Roadmap

| Done | Phase | Scope |
|:---:|---|---|
| ✅ | **0 – Scaffold** | manifest, hacs.json, CI (ruff, pytest-homeassistant-custom-component, hassfest, HACS validate); devcontainer not added |
| ✅ | **1 – Core engine** | `models`, `schedule`, `resolver` with full unit tests; storage (migrations: version field only so far) |
| ✅ | **2 – Controller** | per-room controller, master/auto switches, profile pin select, override policies, multi-TRV sync with echo suppression, reconcile triggers; tested against fake climates |
| ✅ | **3 – Flows** | multi-hub setup, room subentry flows, profiles, rules (state conditions), flow-based schedule editor, services, translations (en/de) — **MVP / usable** |
| ✅ | **3b – External sensor** | `external_sensor.py`, configurable entities and mode option values (TRVZB defaults), throttle/heartbeat/fallback, room flow steps |
| ⬜ | **4 – Card** | websocket API, Lit card (hub/room selector, grid editor, profile tabs, copy day), served by the integration |
| ⬜ | **5 – More triggers** | calendar, template, room occupancy w/ `for`, window-open pause, import/export, diagnostics |

---

## 7. Decided

- Hub → Room → Climate layout; multiple hubs allowed; several TRVs per room with sync incl. manual overrides.
- Rooms can ignore hub modes (follow hub / pin profile).
- Manual override default: hold until next switch point.
- Temperatures only; per-hub master switch and per-room auto/manual switch.
- Zigbee2MQTT + TRVZB; optional external temperature sensor per room.
- Latest HA only.

## 8. Open points (verify before/during implementation)

1. **TRVZB external sensor details (spike first):** exact Z2M entity names, option values for the sensor-type select, accepted
   value range/resolution, and how long the TRV trusts a stale external value. This determines heartbeat/fallback defaults.
2. **Master switch scope:** one per hub (as planned). Is a single global switch across all hubs needed, or is a group/automation fine?
3. **Vacation-style rules:** is a calendar entity the trigger you want for "vacation", or a manual select/`input_boolean`? (Both are
   supported; calendar is v1.1.)

---

## 9. Implementation status

Done (57 tests, `pytest` + `ruff` green against HA 2026.10): scaffold/HACS layout, pure engine
(`models`, `schedule`, `resolver`), rule engine, store, room controller (auto/manual, master switch,
pin, overrides, multi-TRV sync with echo suppression), external sensor sync (TRVZB/Z2M), multi-hub
config flow, room subentry flow, options flow (schedules, profiles, rules), services, en/de translations, CI.

Deviations from the plan above:

- **Schedule editor format:** one text field per day (`HH:MM temperature`, one line per switch point) plus an
  "Also apply to" multi-select, instead of per-block add/edit steps. Fewer steps, easy copy/paste.
  A missing `00:00` line is filled with the last temperature (night value wraps over midnight).
- **Base profile:** the first profile in the list when creating the hub (no separate field).
- **Vacation trigger:** a normal rule on any existing entity (e.g. your own `input_boolean`); nothing is created.
- **Hold policy "ignore":** the schedule value is restored immediately.
- **Overrides are in memory** (lost on reload/restart); base profile, switches, pins and schedules persist.
- **Not yet built:** `export_schedule`/`import_schedule` services, calendar/template conditions, occupancy triggers.
- **Z2M details confirmed** from the device page: `number.*_external_temperature_input` (0–99.9 °C),
  `select.*_temperature_sensor_select` with `local_temperature` / `remote_temperature` /
  `remote_source_offline`. The firmware's stale-value timeout is undocumented, hence the heartbeat.
- **Card + HACS:** HACS installs one category per repository, so the card will be shipped inside the integration
  (`custom_components/thermostat_schedule/frontend/`) and registered by it; one HACS install provides both.

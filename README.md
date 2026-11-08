# z2m-to-zha

Move a **Zigbee2MQTT** network to **ZHA** (Home Assistant's built-in Zigbee integration) on the
**same coordinator**, without re-pairing or re-interviewing any device, and keep every
`entity_id`. Recorder history, long-term statistics, the Energy dashboard, automations, scripts,
helpers and dashboards carry on unchanged.

This came out of a real migration of a production house: about 40 devices (mostly Tuya), a Z-Stack coordinator (CC2652P), and a **64 second** Zigbee outage. The scripts
are written so they do not depend on that house. Everything (add-on, data dir, serial port,
adapter, radio type, zigpy schema version) is auto-detected and can be overridden in a config
file.

> **Read the whole guide before running anything.** This changes a live system. Every writing
> step needs `--yes`, everything else is read-only, and a tested rollback exists, but you are
> responsible for your own backups.

## What already exists, and what this adds

| | Direction | Re-pair | Keeps entity ids | Keeps history |
|---|---|---|---|---|
| [Nabu Casa guide](https://support.nabucasa.com/hc/en-us/articles/26123745634845) "Migrate a Zigbee2MQTT network using ZHA" | Z2M to ZHA | no (network reused) | no, new entities | no |
| [zigporter](https://github.com/nordstad/zigporter) | both | yes, device by device | yes | not stated |
| [zha2z2m](https://github.com/19DMO89/zha2z2m) | ZHA to Z2M | yes (network reuse optional) | yes | yes |
| [zha_z2m_migrator](https://github.com/werner-j/zha_z2m_migrator), [zha-to-z2m](https://github.com/mietzen/zha-to-z2m), [z2m discussion 24478](https://github.com/Koenkk/zigbee2mqtt/discussions/24478) | ZHA to Z2M | no (database conversion) | n/a | n/a |
| **z2m-to-zha** | **Z2M to ZHA** | **no, and no re-interview** | **yes** | **yes** |

## Supported setups

* **Home Assistant**: any install type. On HA OS or Supervised, run it from an SSH add-on
  (Advanced SSH & Web Terminal with protection mode off, so `$SUPERVISOR_TOKEN` and Docker are
  available). Elsewhere, point it at HA with a URL and a long-lived access token and tell it how
  to run HA's Python and how to stop/start Zigbee2MQTT (see Configuration).
* **Zigbee2MQTT**: the add-on (auto-detected) or any other install (stop/start commands in the
  config). Its `database.db` must be readable.
* **Radios**: anything zigpy supports with the same firmware family on both sides.
  * **Z-Stack (TI CC2652/CC1352 etc.)**: tested end to end.
  * **EmberZNet (Silicon Labs)**, **deCONZ**, **ZiGate**, **XBee**: the code paths are generic
    zigpy calls, but they are untested. The listen test needs `--allow-write` there, because only
    zigpy-znp has a read-only network start.
  * Moving between radio *families* (for example Ember to Z-Stack) is a different problem and is
    not what this does.
* Tested with HA Core 2026.11, zha 2.4.2, zigpy 2.4.1 (DB schema v16), zha-quirks 2.4.1,
  zigpy-znp 1.1.1, Zigbee2MQTT 2.14. The DB schema is created by whatever zigpy your HA has.

## How it works

1. **The network lives in the radio.** The coordinator keeps PAN id, extended PAN id, channel,
   network key and frame counter in its own memory. ZHA's config flow can *reuse* those settings
   without writing anything, so the devices never notice that the software on top changed.
2. **ZHA must neither interview 40 devices nor form a network.** Without its own database ZHA
   interviews every device it hears, which fails a lot for sleepy battery devices. And if zigpy
   ever reads the radio as "not formed" while its database has no network backup, it forms a
   *new random network* (`auto_form`). So before the ZHA entry is created, `zigbee.db` is
   preseeded:
   * devices, endpoints, clusters, node descriptors and the Basic cluster attributes
     (manufacturer and model, which quirks match on) from Z2M's `database.db`, which holds the
     full interview of every device,
   * metering and electrical scaling attributes (multiplier, divisor, unit) that Z2M has cached,
   * a network backup seed made from Z2M's `coordinator_backup.json` (both use the Open
     Coordinator Backup format). It tells zigpy this radio is already formed.

   The schema is created by the zigpy inside your HA, so it never needs a migration.
3. **History is keyed by `entity_id`.** The recorder and long-term statistics follow the
   entity_id string, not the integration. The old MQTT entities are removed (which frees the ids)
   and each ZHA entity is renamed *onto* the old id. History continues. HA logs "Cannot migrate
   history ... already in use" during this. That warning is expected and harmless.
4. **Z2M stays installed and stopped.** HA keeps registry tombstones for the removed MQTT
   entities and devices as long as the MQTT config entry exists. If Z2M is started again, its
   entities come back with the **same** entity ids and device ids. That makes the rollback cheap.

## Quick start

```bash
# on an HA OS box, inside the SSH add-on (login shell, so $SUPERVISOR_TOKEN is set)
git clone <this repo> /tmp/z2m-to-zha && cd /tmp/z2m-to-zha
bash -lc "python3 -m z2m2zha detect"                 # what was found (read-only)
```

### Use it as an agent skill (Claude Code, Codex)

The repository root is also an [Agent Skill](https://agentskills.io): `SKILL.md` tells a coding
agent how to run the procedure, what is read-only, and which steps it must ask you about
first. Install it by cloning (or symlinking) the repo as a skill folder:

```bash
# Claude Code, personal skill (or .claude/skills/z2m-to-zha inside a project)
git clone <this repo> ~/.claude/skills/z2m-to-zha
# Codex, personal skill (or .agents/skills/z2m-to-zha inside a repository)
git clone <this repo> ~/.agents/skills/z2m-to-zha
```

Then ask the agent something like "migrate my Zigbee2MQTT network to ZHA" or "check if my Z2M
network can move to ZHA". The skill copies the tool to your HA box over SSH when the agent runs
elsewhere.

### Run it yourself

All commands run from the repository root as `python3 -m z2m2zha <command>`. On HA OS wrap them
in `bash -lc "..."` so the Supervisor token is loaded.

| Command | Writes? | Zigbee outage | What it does |
|---|---|---|---|
| `detect` | no | no | Shows HA, Z2M, radio, HA's Python and package versions |
| `scan-refs` | no | no | Finds references to Z2M entities and devices in your HA config |
| `suggest-device-config` | no | no | Prints the `zha: device_config` that keeps Z2M switches as switches |
| `build-test` | no (temp files) | no | Builds and validates a throwaway preseeded DB, shows the quirk per device |
| `preflight` | no | about 1 min | Reads the radio with zigpy and compares it with Z2M's backup |
| `listen-test` | no (Z-Stack) | 2 to 4 min | Runs the network on zigpy and lists which devices talk |
| `capture` | no | no | Snapshot of all Z2M entities, devices, helpers, Battery Notes |
| `cutover` | **yes** | about 1 min | The migration (stop Z2M, preseed, ZHA flow, plan, apply, verify) |
| `plan` | no | no | Maps every Z2M entity to its ZHA successor, shows blocking rows |
| `apply --yes` | **yes** | no | Renames, moves helpers and Battery Notes, then runs `polish` |
| `polish` | **yes** | no | Makes friendly names, icons and device classes match the capture |
| `nudge --yes` | **yes** | no | Writes captured values back to config entities that stay unknown |
| `verify` | no | no | Checks important entities, estimates the energy gap |
| `rollback --yes` | **yes** | about 1 min | Back to Zigbee2MQTT with the same ids |

`preflight`, `listen-test`, `cutover` and `rollback` **detach themselves** (own session, output
in `state/<name>.log`). A dropped SSH session can therefore never leave Zigbee2MQTT stopped
half-way. Follow them with `tail -f state/<name>.log` until the `RESULT` line. Add
`--foreground` to stay attached.

## Step by step

### 1. Prepare (Z2M keeps running, no outage)

1. **Back up.** Take a full HA backup and copy the whole Z2M data dir (`database.db`,
   `coordinator_backup.json`, `configuration.yaml`, `devices.yaml`, `state.json`). The state dir
   of this tool also ends up holding network backups (mode 0600, they contain the network key).
2. **Clean up Z2M.** Every device should have a complete interview. `build-test` lists devices it
   had to skip. A device that never finished its interview in Z2M will not work in ZHA either.
3. **Check references:** `scan-refs`. Entity id references survive. Anything listed under
   `device_ids_BREAK` (device triggers, conditions and actions), `entity_uuids_BREAK`,
   `ieee_BREAK` or raw `zigbee2mqtt/` MQTT topics must be rewritten, ideally into entity based
   triggers and actions. `plan` and `verify` use this report too.
4. **Light vs switch:** `suggest-device-config`. ZHA decides light or switch from the endpoint's
   device type, and many relays announce "on/off light". Without the override, `switch.x` would
   become `light.x`, its history could not continue and any `switch_as_x` helper built on it
   would break. Merge the printed YAML into `configuration.yaml` and **restart HA once, before
   the cutover**:
   ```yaml
   zha:
     device_config:
       "aa:bb:cc:dd:ee:ff:00:11-1":   # <ieee>-<endpoint>
         type: switch
   ```
5. **Quirks:** `build-test`. It shows which quirk ZHA would apply to each device. Tuya `_TZE...`
   devices without a quirk usually show few or no entities in ZHA. Add custom quirks
   (`zha: custom_quirks_path: /config/custom_zha_quirks/`, see `examples/quirks/`) and run
   `build-test` again until every device you care about has one.
6. **Preflight** (about 1 minute of outage): `preflight`. PASS means zigpy reads this radio as
   the same network Z2M has (PAN, extended PAN, channel, network key, coordinator). It also
   saves the radio's own backup in the staging folder as a seed fallback for when Z2M has no
   `coordinator_backup.json`.
7. **Optional listen test** (a few minutes of outage): `listen-test`. It runs the real network on
   zigpy with a throwaway preseed. It reads the manufacturer name from every router (proof of
   two-way traffic) and counts frames from sleepy devices.
8. **Configuration:** set `energy.power` (and `energy.utility_meter`) if a Z2M power sensor feeds
   a Riemann integration in your Energy dashboard, and list in `must_exist` the entities that
   matter most to you.

### 2. Cut over (about 1 minute of outage)

```bash
bash -lc "python3 -m z2m2zha cutover"
tail -f state/cutover.log
```

1. **capture**: entity registry entries, their options, the current state and attributes
   (friendly name, icon, device class), devices, areas, labels, `switch_as_x` helpers and Battery
   Notes.
2. **Stop Z2M** (add-on boot set to manual), then wait until no process holds the serial port.
   This checks `/proc/*/fd` in every container rather than sleeping a fixed time.
3. **Build and validate** `zigbee.db` from the files Z2M just saved, then put it in place. Any
   existing `zigbee.db` is kept as `zigbee.db.pre-z2m2zha-<time>`.
4. **ZHA config flow over REST**, using only safe choices:
   * `choose_serial_port`: the configured port, or the manual option, then
     `manual_pick_radio_type` and `manual_port_config` when HA's probe fails
   * `choose_setup_strategy`: **`setup_strategy_advanced`**. "Recommended" can form a new
     network.
   * `choose_formation_strategy`: **`reuse_settings`**
   * **STOP signal**: if the menu offers `choose_automatic_backup`, ZHA thinks the radio
     differs from the seeded backup. The tool aborts and starts Z2M again.
   * The client timeout is 300 seconds, because the radio probe can take more than 60.
5. Wait until ZHA is loaded with every device. **The outage ends here.**
6. **plan** in a loop. Energy counters without a value are read actively. Then **apply**, which
   includes **polish**, then **verify**.

Before the ZHA entry exists, any failure aborts the flow, waits for the serial port and starts
Zigbee2MQTT again, so you are back where you started. After that point, use `rollback`.

If `plan` has blocking rows, the cutover stops with ZHA running and the registry untouched. Fix
the rows with `state/overrides.json` (`{"switch.old_id": "switch.zha_id"}`, or `null` to drop one
on purpose), wait for the device to report, or add a `device_config` override. Then run `plan`
and `apply --yes` yourself.

### 3. After the cutover

* `verify` lists every important entity with its platform and state. Battery devices stay
  `unknown` until they next report.
* `nudge --yes`: some Tuya devices only report on change and ignore the data query. Their
  numbers, selects and thermostat state stay unknown in ZHA until something changes. Writing the
  captured value back makes them report. Only entities that are unknown right now are touched.
  Relays are never toggled.
* **Energy gap**: during the outage the power sensor was unavailable, so a Riemann integration
  did not integrate. `verify` estimates the missing kWh (trapezoid, like the Riemann helper) and
  prints a one-time `utility_meter.calibrate` action for you to review and run.
* **Metering scale**: a plug whose quirk does not cover its manufacturer may show energy 100x
  too high or without a unit. `tools/fix_attribute_cache.py` writes the multiplier and divisor
  into the zigpy cache (with HA stopped).
* Keep Z2M installed, stopped and on manual boot for a couple of weeks. Uninstalling it is fine
  later, but **deleting the MQTT integration deletes the tombstones** the rollback relies on.

## Mapping rules

* The join is on the device IEEE first: Z2M unique id `0x<ieee>_<property>_zigbee2mqtt`, ZHA
  device identifier `(zha, <ieee with colons>)`. Then entities are matched within the device by
  domain and a rule per Z2M property. ZHA unique ids look like `<ieee>-<endpoint>-<cluster>`.
  Examples: on/off `-1` or `-1-6`, occupancy `-1-1030`, energy cluster `1794` or translation key
  `summation_delivered`, multi-gang `switch_l2` to endpoint 2.
* **Same domain only.** Z2M often exposes a value twice (for example a sensor and a select).
  The duplicate is dropped.
* Entities without a ZHA counterpart (link quality is optional, plus firmware `update.*`, some
  diagnostics, Z2M's own bridge entities) are dropped. Their old history stays readable.
* A row **blocks** when a counter would jump, unit or state class would change (that would
  corrupt statistics), the device is missing in ZHA, or an entity that your config references
  would be dropped.

## Traps this handles (learned the hard way)

* **switch_as_x deletes itself.** When the entity it wraps disappears from the registry, the
  helper removes its own config entry and its light. The helpers are disabled while the old
  entities go away and are enabled again afterwards.
* **"Connected via Zigbee2MQTT Bridge"** and a coordinator named "Zigbee2MQTT Bridge": the Z2M
  bridge device has the coordinator's IEEE. Its name is never copied to the ZHA coordinator.
* **Friendly names.** HA shows "<device name> <entity name>", and ZHA names entities differently
  from Z2M. `polish` splits the captured friendly name and sets the entity name, icon and device
  class so everything looks as before.
* **Battery Notes** keys on `device_id`. Each note is recreated on the new device through its
  subentry flow (None values stripped, or HA returns HTTP 400), its entities get the old ids back
  and the replaced date is restored.
* **Select options** are capitalised in ZHA ("Off", "External") where Z2M used lowercase. Check
  automations that compare option strings. `nudge` matches them case-insensitively.
* **The probe holds the port.** If a client gives up during the config flow's radio probe, HA
  keeps probing and holds the serial port, and Z2M then fails with "Cannot lock port". The tool
  deletes the flow and waits for the port before starting Z2M again.
* **Device-based automations** (`device_id` triggers, conditions and actions, `zha_event` or MQTT
  device triggers) do not follow a rename. `scan-refs` finds them. Rewrite them.

## Rollback (tested path)

```bash
bash -lc "python3 -m z2m2zha rollback --yes"
```

1. Saves a fresh ZHA network backup (state dir, 0600).
2. Disables the `switch_as_x` helpers and removes the Battery Notes on ZHA devices.
3. **Deletes the ZHA config entry.** That only unloads ZHA: no leave is sent and the radio is not
   touched. Never use ZHA's "Remove device" for this, because that sends a real leave.
4. Starts Zigbee2MQTT. zigbee-herdsman sees the same adapter, PAN and keys and resumes the
   network as on any restart.
5. Waits for MQTT discovery. The tombstoned entries come back with their original ids, history
   continues. Ids and customisations are re-asserted where needed.
6. Restores Battery Notes and `switch_as_x`, reloads helpers. Then run `verify`.

## Configuration

Copy `config.example.json` to `config.json` (or pass `--config`). Every key is optional.

| Key | Default | Meaning |
|---|---|---|
| `ha.url`, `ha.token_env` | Supervisor proxy | For HA without Supervisor: base URL, and the env var with a long-lived token |
| `ha_config_dir` | `/homeassistant` or `/config` | HA config dir as this tool sees it |
| `ha_config_dir_in_ha` | `/config` in a container | The same dir as HA's Python sees it |
| `ha_python` | auto | Command that runs HA's Python, e.g. `docker exec -i homeassistant python3` |
| `zha_database` | `<config>/zigbee.db` | Only if you set a custom `zha: database_path` |
| `custom_quirks_dir_in_ha` | `/config/custom_zha_quirks` if present | Quirks for `build-test` |
| `z2m.addon_slug` | auto | Z2M add-on slug |
| `z2m.data_dir` | add-on `data_path` | Z2M data dir as this tool sees it |
| `z2m.stop_cmd`, `z2m.start_cmd` | none | For non add-on installs, e.g. `docker stop zigbee2mqtt` |
| `radio.port`, `radio.type`, `radio.baudrate`, `radio.flow_control` | from Z2M's `serial` | Type is a zha radio name: `znp`, `ezsp`, `deconz`, `zigate`, `xbee` |
| `port_check` | `docker` if available, else `proc` | How to find who holds the serial port (`none` to skip) |
| `energy.power`, `energy.utility_meter` | none | For the energy gap estimate |
| `soft_counters` | `[]` | `total_increasing` sensors that are known noise (state class change accepted) |
| `must_exist` | `[]` | Extra entities `verify` must find with a state |

The serial port path must be the same for this tool and for HA (true on HA OS). A network
coordinator is written `tcp://host:port` in Z2M and is translated to zigpy's `socket://`.

## Layout

```
SKILL.md        agent skill instructions (Claude Code, Codex)
z2m2zha/        the tool (stdlib only, runs where it can reach HA and the Z2M files)
zigpy_side/     small scripts run with HA's own Python (zigpy, zha, quirks), passed with -c
tools/          fix_attribute_cache.py (stdlib sqlite)
examples/quirks/  two zha-quirks v2 quirks written for this migration (Tuya X5H thermostat,
                  Tuya PJ-1203 clamp meter)
```

Nothing is copied into the HA container. Files HA's Python must read are staged in
`<HA config>/.z2m2zha/` (mode 0700) and removed afterwards. Output from HA's Python is masked so
nothing that looks like a key is ever printed.

## License

MIT

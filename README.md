# z2m-to-zha

Move a **Zigbee2MQTT** network to **ZHA** (Home Assistant's built-in Zigbee integration) on the
**same coordinator**, without re-pairing or re-interviewing any device, and keep every
`entity_id`. Recorder history, long-term statistics, the Energy dashboard, automations, scripts,
helpers and dashboards carry on unchanged.

This came out of a real migration of a production house: about 40 devices (mostly Tuya), a Z-Stack coordinator (CC2652P), and a **64 second** Zigbee outage. The scripts
are written so they do not depend on that house. Everything (add-on, data dir, serial port,
adapter, radio type, zigpy schema version) is auto-detected and can be overridden in a config
file.

> **Maturity.** The method (preseeded `zigbee.db`, reused network, ids renamed onto the old ones,
> tombstone rollback) ran end to end on that house with a one-off version of these scripts. This
> generic tool reuses that logic and was verified read-only on the same house (`detect`,
> `scan-refs`, `build-test`, `capture`, `plan`, `verify`), but it has not done a full cutover
> itself yet. Read each step's output instead of assuming success, and report what you find.

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
git clone https://github.com/vladyspavlov/z2m-to-zha.git /tmp/z2m-to-zha && cd /tmp/z2m-to-zha
bash -lc "python3 -m z2m2zha detect"                 # what was found (read-only)
```

### Use it as an agent skill (Claude Code, Codex)

The repository root is also an [Agent Skill](https://agentskills.io): `SKILL.md` tells a coding
agent how to run the procedure, what is read-only, and which steps it must ask you about
first. Install it by cloning (or symlinking) the repo as a skill folder:

```bash
# Claude Code, personal skill (or .claude/skills/z2m-to-zha inside a project)
git clone https://github.com/vladyspavlov/z2m-to-zha.git ~/.claude/skills/z2m-to-zha
# Codex, personal skill (or .agents/skills/z2m-to-zha inside a repository)
git clone https://github.com/vladyspavlov/z2m-to-zha.git ~/.agents/skills/z2m-to-zha
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
| `suggest-zha-config` | no | no | Prints the `zha:` block: switch pins, TX power from Z2M, custom quirks path |
| `build-test` | no (temp files) | no | Builds and validates a throwaway preseeded DB, shows the quirk per device |
| `preflight` | no | about 1 min | Reads the radio with zigpy and compares it with Z2M's backup |
| `listen-test` | no (Z-Stack) | 2 to 4 min | Runs the network on zigpy and lists which devices talk |
| `backup --yes` | yes (backups only) | no | Copies the Z2M data dir and creates a full HA backup with your automatic backup settings |
| `capture` | no | no | Snapshot of all Z2M entities, devices, helpers, Battery Notes (plus a durable copy) |
| `cutover` | **yes** | about 1 min | The migration (stop Z2M, preseed, ZHA flow, plan, apply, verify) |
| `plan` | no | no | Maps every Z2M entity to its ZHA successor, shows blocking rows |
| `apply --yes` | **yes** | no | Renames, moves helpers and Battery Notes, then runs `polish` |
| `polish` | **yes** | no | Makes friendly names, icons and device classes match the capture |
| `battery-notes --yes` | **yes** | no | Re-runs only the Battery Notes move (it is part of `apply`) |
| `nudge --yes` | **yes** | no | Writes captured values back to config entities that stay unknown |
| `verify` | no | no | Checks important entities, estimates the energy gap |
| `orphans` | with `--yes` | no | Lists (removes) ZHA entities that ZHA no longer provides |
| `rollback --yes` | **yes** | about 1 min | Back to Zigbee2MQTT with the same ids |

`backup`, `preflight`, `listen-test`, `cutover` and `rollback` **detach themselves** (own session, output
in `state/<name>.log`). A dropped SSH session can therefore never leave Zigbee2MQTT stopped
half-way. Follow them with `tail -f state/<name>.log` until the `RESULT` line. Add
`--foreground` to stay attached.

## Step by step

### 1. Prepare (Z2M keeps running, no outage)

1. **Back up:** `backup --yes`. It copies the whole Z2M data dir (without logs) to
   `<HA config>/z2m2zha-backups/` and creates a full HA backup with your automatic backup
   settings (your locations, your encryption choice). Without automatic settings it makes a local,
   unencrypted backup. The state dir and that folder also hold network backups and captures
   (mode 0600, network backups contain the key).
2. **Clean up Z2M.** Every device should have a complete interview. `build-test` lists devices it
   had to skip. A device that never finished its interview in Z2M will not work in ZHA either.
3. **Check references:** `scan-refs`. Entity id references survive. Anything listed under
   `device_ids_BREAK` (device triggers, conditions and actions), `entity_uuids_BREAK`,
   `ieee_BREAK` or raw `zigbee2mqtt/` MQTT topics must be rewritten, ideally into entity based
   triggers and actions. `plan` and `verify` use this report too.
4. **ZHA settings:** `suggest-zha-config`. It prints the `zha:` block that reproduces your Z2M
   setup. **Light vs switch**: ZHA decides from the endpoint's device type, and many relays
   announce "on/off light". Without a pin, `switch.x` would become `light.x`, its history could
   not continue and any `switch_as_x` helper built on it would break. **TX power**: Z2M's
   `advanced.transmit_power` becomes `zigpy_config.network.tx_power`. **Quirks**: the custom
   quirks path, when the folder exists. Merge it into `configuration.yaml` and **restart HA once,
   before the cutover**:
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
* `orphans`: lists ZHA entities ZHA no longer provides (a `device_config` pin added after ZHA
  already ran leaves the old `light.*`, plugs leave sensors for attributes they do not support).
  `orphans --yes` removes them. Migrated ids are never touched.
* Keep Z2M installed, stopped and on manual boot for a couple of weeks. Uninstalling it is fine
  later, but **deleting the MQTT integration deletes the tombstones** the rollback relies on.

### 4. Later: things that work differently in ZHA

These came up in the days after the real migration. None of them needs this tool, but they save
time.

* **Tuya devices that report only on change.** Besides `nudge`, the usual cause of a value that
  stays `unknown` is a generic ZCL entity the device cannot fill (thermostat
  `pi_heating_demand`, `setpoint_change_source`, the On/Off client "Opening" sensor of TS0203
  contacts). Drop them in the quirk with `prevent_default_entity_creation` and remove the old
  registry entries (`orphans --yes`).
* **Sleepy battery devices** often never send a battery report on their own. Reading the power
  configuration cluster right after the device sends an event (it is awake then) fills it in.
  See the read on wake in `examples/quirks/tuya_ts0203_contact.py`.
* **Setpoint steps.** ZHA offers 0.1 degree steps on every thermostat. A device that only knows
  0.5 degree steps then shows values its own buttons cannot reach. Round the writes in the quirk
  (see `setpoint_to_dp` in the X5H example) and set `target_temp_step: 0.5` in
  `homeassistant: customize:` for the climate entity.
* **Text settings such as schedules.** ZHA has no text entities. The quirk can show the value as
  a sensor and accept writes through the `zha.set_zigbee_cluster_attribute` action. For editing
  from the UI, an `input_text` helper plus an automation that validates and writes it works well.
* **Re-pairing a device later is safe for the ids.** When a removed device joins again with the
  same IEEE, HA gives its entities their old entity ids, names and area back, so history
  continues. A `switch_as_x` helper on one of its switches deletes itself on the removal and
  must be created again (under its old entity id).
* **Firmware updates (OTA).** Z2M's OTA index does not carry over. ZHA uses zigpy's providers.
  Local images go in `zha: zigpy_config: ota: extra_providers:` as `type: z2m_local` with an
  `index_file` in the Z2M index format. Each entry needs `url` even when `path` is set (the
  schema rejects it otherwise), the index is cached until HA restarts, and the update entity
  only shows a new version after the device asks for one (sending an Image Notify with
  `zha.issue_zigbee_cluster_command`, cluster 25, client command 0, prompts that).
  Sleepy end devices may never fetch the blocks, so an update can time out on them while
  routers update fine. An update can also leave a device with broken attribute reporting:
  "Reconfigure" on its device page fixes that.
* **Quirks upstream.** The X5H thermostat quirk from `examples/quirks/` was submitted to
  zha-device-handlers ([#5694](https://github.com/zigpy/zha-device-handlers/pull/5694)). Once a
  zha-quirks release contains a device, remove the custom copy. The unique ids stay the same, so
  the entities keep their ids.

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
examples/quirks/  zha-quirks v2 quirks written for this migration: Tuya X5H thermostat (with
                  week, schedule sensor, factory reset button and a clock fix), Tuya PJ-1203
                  clamp meter, Tuya relays and plugs with the Z2M settings (countdown, switch
                  type, 4 value indicator), TS0203 contact sensors (tamper, battery from
                  voltage read on wake, no dead "Opening" sensor), TS0210 vibration
                  (sensitivity, battery read on wake)
```

Nothing is copied into the HA container. Files HA's Python must read are staged in
`<HA config>/.z2m2zha/` (mode 0700) and removed afterwards. Output from HA's Python is masked so
nothing that looks like a key is ever printed.

## License

MIT

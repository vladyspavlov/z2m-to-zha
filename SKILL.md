---
name: z2m-to-zha
description: Migrate a Home Assistant Zigbee network from Zigbee2MQTT to ZHA on the same coordinator without re-pairing devices, keeping every entity_id so history, long-term statistics, Energy, automations and helpers continue. Use when the user wants to switch from Zigbee2MQTT (Z2M) to ZHA, check whether that is possible, test it, run it, or roll it back.
---

# Zigbee2MQTT to ZHA, same coordinator, same entity ids

This folder is both the skill and the tool. `README.md` is the full guide (why each step exists,
traps, rollback, configuration). Read it before the first run. This file is the procedure.

**Maturity.** The method was proven end to end on one production house (Z-Stack, about 40
devices, 64 s outage). This generic tool was verified read-only on that house. The writing and
outage steps reuse that logic but this exact code has not done a full cutover yet. Say so to the
user, and read each step's output carefully instead of assuming success.

## Ground rules

* **Read-only by default.** These commands do not change HA, Z2M or the radio (they only
  write the tool's own files, staging and capture copies): `detect`, `scan-refs`,
  `suggest-zha-config`, `build-test`, `capture`, `plan`, `verify`, `orphans` (without `--yes`).
  Run them freely.
* **Ask the user right before each step that causes a Zigbee outage or writes**, say what it
  does and how long Zigbee is down, and wait for an explicit yes. Approval for one step is not
  approval for the next. These steps are: `backup`, `preflight` (about 1 min outage),
  `listen-test` (2 to 4 min), `cutover` (about 1 min, changes the registry), `apply`, `polish`,
  `nudge`, `orphans --yes`, `rollback`, any edit of `configuration.yaml` or quirk files, any
  Home Assistant restart, and any change to backup settings.
* **Never print secrets.** Z2M's `configuration.yaml`, `coordinator_backup.json`, network
  backups in the state dir and `secrets.yaml` contain the network key or credentials. The tool
  masks key-like output. Do not cat these files, redact when quoting.
* **Never** use ZHA's "Remove device" (it sends a real leave), never delete the MQTT integration
  (it holds the tombstones the rollback needs), never pick ZHA's "recommended" setup by hand (it
  can form a new network), never hand-edit `.storage` files.
* **Keep a change log for the user**: after every writing step, note what changed, why, which
  files or ids, and how to undo it (in the user's notes file if they have one, otherwise in your
  final report).

## Before you start: access

Find out, ask only what you cannot detect:

1. **How to reach Home Assistant.** On HA OS or Supervised: an SSH add-on (Advanced SSH & Web
   Terminal) with **protection mode off**, restarted once after switching it off (only then
   `$SUPERVISOR_TOKEN` and Docker are available). Ask the user for the SSH host alias or
   address and test `ssh HOST 'bash -lc "echo ok"'`. Use `bash -lc` (a login shell) for every
   command so the token is loaded. Other installs: a host with access to the HA config and Z2M
   files, a long-lived access token, and the config keys in README section Configuration.
2. **Python 3** on that host (the add-on has it). The tool uses the standard library only. The
   zigpy parts run in HA's own Python automatically.
3. The user's **goal and constraints**: when an outage is acceptable, which devices or
   automations matter most, whether the Energy dashboard uses a Z2M power sensor.

If you work from another machine, copy this folder to the box and run it there (replace `HOST`):

```bash
SKILL_DIR="<path of this skill folder>"
tar czf - -C "$SKILL_DIR" --exclude=.git --exclude=state . | ssh HOST 'rm -rf /tmp/z2m-to-zha && mkdir -p /tmp/z2m-to-zha && tar xzf - -C /tmp/z2m-to-zha'
ssh HOST 'bash -lc "cd /tmp/z2m-to-zha && python3 -m z2m2zha detect"'
```

Use `ssh HOST 'bash -lc "cd /tmp/z2m-to-zha && python3 -m z2m2zha <command>"'` for every
command below. **Keep `/tmp/z2m-to-zha/state/`** between steps (capture, plan, references, logs,
network backups). Re-copying with the command above deletes it, so re-copy only before step 1
or copy the state dir aside first. `/tmp` is lost when the add-on restarts, so `capture` also
writes a durable copy to `<HA config>/z2m2zha-backups/capture-<time>.json` (the rollback needs it).

**Detached steps.** `backup`, `preflight`, `listen-test`, `cutover` and `rollback` start in the
background and print a log path (`state/<name>.log`). Poll it with `tail -n 40` every 20 to 60
seconds until a line starting with `RESULT` appears. A dropped SSH session does not stop them.

## Procedure

### 1. Assess (read-only)

1. `detect`. Check: HA reachable, ZHA entries 0 (ignored discoveries are fine, the cutover
   removes them), Z2M found, Z2M device count, "Z2M network backup: present", radio port and
   type (`znp` is the proven path), package versions. If the type is `ezsp`, `deconz`, `zigate`
   or `xbee`, tell the user that path is untested. If a real ZHA entry exists, stop and ask.
2. `scan-refs`. Entity id references survive. Report everything under `device_ids_BREAK`,
   `entity_uuids_BREAK`, `ieee_BREAK` and `indirect_CHECK` (device triggers/actions, raw
   `zigbee2mqtt/` topics). They must be rewritten. Offer to convert them to entity based
   triggers/actions (with approval, back up the file, validate the config, reload).
   Then read its config entry list. `PROTECT`, `RELINK`, `HANDLED` and `KEEP` are taken care of.
   For each `MANUAL` entry tell the user what will need repointing after the cutover. A custom
   integration that deletes or rewrites itself when its source entity disappears belongs in
   `protect_domains`, one whose options flow is a plain form with the device field in
   `relink_options_domains` (ask before changing the config). Mention the HomeKit note if shown.
3. `suggest-zha-config`. It prints the `zha:` block that reproduces the Z2M setup: switch
   pins (`device_config`, else those switches become lights), TX power from Z2M, and the
   custom quirks path. With approval, merge it into `configuration.yaml` (back the file up,
   keep a single `zha:` key, validate with `ha core check` or Developer Tools, Check
   configuration) and ask the user to restart HA. Do it now, BEFORE the cutover.
4. `build-test`. Wants `RESULT: OK` with built == loaded, 0 problems, backup present. Review
   the quirk column: Tuya `_TZE...` devices with "no quirk" usually get few or no entities in
   ZHA. For each one that matters, see **Writing a quirk** below.
5. Ask about the energy chain: if a Z2M power sensor feeds a Riemann integration / utility
   meter for the Energy dashboard, put both ids in `config.json` under `energy`. Put the most
   important entities in `must_exist`.

Summarise the findings, risks and the plan, then propose the next step.

### 2. Back up and prove the radio (ask first for each)

1. `backup --yes`: copies the Z2M data dir to `<HA config>/z2m2zha-backups/` and creates a full
   HA backup with the user's automatic backup settings (their locations and encryption). Wants
   `RESULT: PASS`. Never change backup encryption or passwords on your own.
2. `preflight` (about 1 min outage). Wants `RESULT: PASS - ZHA will see the same network`. A
   FAIL means do not migrate. Report what differed (channel, PAN, extended PAN, key).
3. Optional `listen-test` (2 to 4 min outage). Routers should mostly be `OK`. Sleepy devices
   are often asleep, which is normal. Radios other than Z-Stack need `--allow-write`, explain
   why (no read-only start) and ask.

### 3. Cutover (ask first, about 1 min outage)

1. Confirm: backup PASS, `zha:` block in place and HA restarted, the user agrees, and nobody
   relies on Zigbee for the next 10 minutes.
2. `cutover`, then poll `state/cutover.log`. Possible endings:
   * `RESULT: CUTOVER APPLIED`: go to step 4.
   * `RESULT: ABORTED - <reason>`: Z2M was started again and nothing else changed. Report the
     reason. Typical: preseed not clean (fix in Z2M or quirks), STOP signal
     `choose_automatic_backup` (the radio differs from the backup, never force it), radio probe
     problems (check `radio.type`, the port and that nothing else holds it).
   * `RESULT: PLAN HAS BLOCKING ROWS`: ZHA runs, the registry is untouched, Z2M is stopped, so
     Zigbee works but automations see the new ZHA ids only. Fix the rows quickly (below), then
     `plan` and, with approval, `apply --yes`. If it takes long, offer `rollback --yes`.
   * `RESULT: ZHA NOT LOADED`: investigate the HA log. If it cannot be fixed quickly, offer
     `rollback --yes`.

**Fixing blocking plan rows** (`plan` prints each row with its status):
* `NOMATCH ... ZHA made it a light`: a `device_config` pin is missing. Add it, HA restart,
  `plan` again, and later `orphans` for the left-over `light.*`.
* `BLOCK counter has no value yet`: wait a few minutes for a report, `plan` again.
* `BLOCK unit` or `state_class` change, or `counter jump`: statistics would break. Usually a
  quirk or scaling problem (missing metering divisor: `tools/fix_attribute_cache.py`, HA
  stopped). Do not override these without explaining the consequence.
* `NOMATCH ambiguous` or a wrong guess: write `state/overrides.json` as
  `{"<old entity_id>": "<zha entity_id>"}`. Use `null` to drop an entity on purpose, only after
  the user agrees, because its history stops there.
* `BLOCK referenced in config but no ZHA successor`: something uses that entity. Propose a
  replacement entity and edit the reference, or an override, with approval.

### 4. After the cutover

1. `verify`. `BAD` battery devices are usually fine until they report. Anything else `BAD`
   needs a look. A suffix warning means a rename did not land. The `links:` part lists config
   entries that still point at removed Z2M devices or entity UUIDs (repoint them in the UI with
   the user, the old and new device are printed) and helpers that deleted themselves.
2. If config entities (numbers, selects, thermostats) stay `unknown`, offer `nudge --yes`. It
   writes the captured values back and never toggles relays.
3. The energy estimate in `verify` prints a `utility_meter.calibrate` action. Show it, run it
   only with approval.
4. `orphans`: ZHA entities that ZHA no longer provides (for example after a late
   `device_config` pin, or attributes a plug turned out not to support). Offer `orphans --yes`.
5. Rewrite the device based automations found by `scan-refs` if not done yet.
6. Check one dashboard, one automation trace and the Energy dashboard with the user. Remind
   them: Z2M stays installed, stopped and on manual boot for a couple of weeks. Do not delete the
   MQTT integration.
7. Report: outage length, mapped and dropped counts, what is still unknown, what needs manual
   follow-up, the change log, how to roll back.

### Rollback (ask first)

`rollback --yes` brings Z2M back with the same entity ids and device ids. It works before or
after `apply` and needs `state/capture.json`. Then run `verify`. Offer to remove the `zha:`
block afterwards (HA restart needed).

## Writing a quirk (when a Tuya device has none)

1. Find the device's Zigbee2MQTT definition in zigbee-herdsman-converters (search the
   manufacturer string, for example `_TZE200_xxxxxxxx`, in `src/devices/tuya.ts` or
   `lib/legacy`). Note each datapoint: id, meaning, type, scaling.
2. Check zha-quirks first: the manufacturer string may already be listed under another quirk,
   possibly with a wrong signature or wrong scaling (`build-test` shows which one matches).
3. Write a v2 quirk with `zhaquirks.tuya.builder.TuyaQuirkBuilder` (see `examples/quirks/` for a
   thermostat and a clamp meter). Use the **Z2M property names** as attribute names and
   translation keys where you can, so `plan` maps them. Keep the same units and scaling as Z2M,
   or the statistics break.
4. Put it in the custom quirks folder, run `build-test` until the device shows the new quirk,
   then HA restart (with approval) before the cutover.

## Troubleshooting

| Symptom | Meaning and fix |
|---|---|
| `no Home Assistant access` | Not a login shell (use `bash -lc`), or protection mode on, or set `ha.url` and `HA_TOKEN` |
| `cannot find a Python with zigpy + zha` | Set `ha_python`, e.g. `docker exec -i homeassistant python3` |
| `cannot control Zigbee2MQTT` | No add-on found. Set `z2m.stop_cmd` and `z2m.start_cmd` |
| `serial port still held` | Another process (often a stuck HA probe) has the port. Wait, `detect` shows who |
| Z2M log: "Cannot lock port" | Same cause. The tool waits for the port before starting Z2M |
| First radio read times out | Normal right after Z2M releases the port. The tool retries |
| SSH drops during `backup` | Normal (add-ons freeze). It runs detached. Reconnect and poll the log |
| Many `unknown` thermostats/selects | Report-on-change Tuya devices. `nudge --yes` |
| Energy 100x too high on a plug | Missing metering divisor. `tools/fix_attribute_cache.py` with HA stopped |
| Door sensor blinks but its entity never changes | The old id sits on the On/Off client "Opening" sensor (`-1-6`), the contact is the IAS zone sensor (`-1-1280`). `plan` prefers IAS now. To fix an existing install, swap the two ids without moving history (rename the dead one onto an id that already has recorder history) |
| Sleepy sensor battery stays `unknown` for days | It rarely reports the battery (Z2M often had no value either). Read the voltage right after the device sends an event, see the read on wake in `examples/quirks/tuya_ts0203_contact.py` |
| Thermostat sensors `pi_heating_demand`, `setpoint_change_source` always `unknown` | Generic ZCL entities a Tuya MCU thermostat cannot fill. Drop them in the quirk with `prevent_default_entity_creation` |
| Lost `state/` (add-on restarted) | Restore `capture.json` from the durable copy before `rollback` or `verify` |
| Thermostat shows 21.4 and its buttons jump to 20.9 / 21.9 | The device only knows 0.5 C steps. Round setpoint writes in the quirk and set `target_temp_step: 0.5` in `customize` |
| User wants to edit a text setting (schedule) | ZHA has no text entities. Show it as a sensor, write it with `zha.set_zigbee_cluster_attribute`, offer an `input_text` helper plus an automation for the UI |
| Firmware update never offered after the move | Z2M's OTA index does not carry over. Add a `z2m_local` provider (each entry needs `url`, cached until HA restarts), then send an Image Notify (cluster 25, client command 0) so the device asks |
| OTA ends with TIMEOUT (148) | No block request for 30 s. Common on sleepy end devices, which may never update this way. Routers are fine |
| Relay switches with a lag or its state never updates after an OTA or re-pair | Attribute reporting was not set up (busy mesh). Run "Reconfigure" on the device page, then check one on/off |
| A device was removed and joined again | HA restores its old entity ids, names and area. Recreate any `switch_as_x` helper that wrapped it, under its old entity id |

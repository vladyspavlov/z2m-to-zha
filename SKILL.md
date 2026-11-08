---
name: z2m-to-zha
description: Migrate a Home Assistant Zigbee network from Zigbee2MQTT to ZHA on the same coordinator without re-pairing devices, keeping every entity_id so history, long-term statistics, Energy, automations and helpers continue. Use when the user wants to switch from Zigbee2MQTT (Z2M) to ZHA, check whether that is possible, test it, run it, or roll it back.
---

# Zigbee2MQTT to ZHA, same coordinator, same entity ids

This folder is both the skill and the tool. `README.md` is the full guide (why each step exists,
traps, rollback, configuration). Read it before the first run. This file is the procedure.

## Ground rules

* **Read-only by default.** These commands only read: `detect`, `scan-refs`,
  `suggest-device-config`, `build-test`, `capture`, `plan`, `verify`. Run them freely.
* **Ask the user right before each step that causes a Zigbee outage or writes**, say what it
  does and how long Zigbee is down, and wait for an explicit yes. Approval for one step is not
  approval for the next. These steps are: `preflight` (about 1 min outage), `listen-test`
  (2 to 4 min), `cutover` (about 1 min, changes the registry), `apply`, `polish`, `nudge`,
  `rollback`, any edit of `configuration.yaml`, and any Home Assistant restart.
* **Never print secrets.** Z2M's `configuration.yaml`, `coordinator_backup.json`, the state dir
  network backups and `secrets.yaml` contain the network key or credentials. The tool masks
  key-like output. Do not cat these files.
* **Never** use ZHA's "Remove device" (it sends a real leave), never delete the MQTT integration
  (it holds the tombstones the rollback needs), never pick ZHA's "recommended" setup by hand (it
  can form a new network), never hand-edit `.storage` files.
* Make sure a full HA backup and a copy of the Z2M data dir exist before `cutover`. Ask the user
  to confirm, or offer to create them.

## Where it runs

The tool must run where it can reach the HA API and read the HA config and Z2M files. On HA OS
or Supervised that is an SSH add-on (Advanced SSH & Web Terminal with protection mode off), in a
**login shell** so `$SUPERVISOR_TOKEN` is set. Everything is standard library Python. The zigpy
parts run inside HA's own Python automatically.

If you work from another machine, copy this folder to the box and run it there (replace `HOST`):

```bash
SKILL_DIR="<path of this skill folder>"
tar czf - -C "$SKILL_DIR" --exclude=.git --exclude=state . | ssh HOST 'rm -rf /tmp/z2m-to-zha && mkdir -p /tmp/z2m-to-zha && tar xzf - -C /tmp/z2m-to-zha'
ssh HOST 'bash -lc "cd /tmp/z2m-to-zha && python3 -m z2m2zha detect"'
```

Use the same `ssh HOST 'bash -lc "cd /tmp/z2m-to-zha && python3 -m z2m2zha <command>"'` pattern
for every command below. Re-copy after changing files. Keep `/tmp/z2m-to-zha/state/` between
steps: it holds `capture.json`, `plan.json`, `refs.json`, logs and network backups.

For HA without Supervisor (Container, Core), create `config.json` from `config.example.json`
with `ha.url`, a `HA_TOKEN` env var, `ha_python` and `z2m.stop_cmd`/`z2m.start_cmd`. See the
README section Configuration.

**Detached steps.** `preflight`, `listen-test`, `cutover` and `rollback` start in the
background and print a log path (`state/<name>.log`). Poll it with `tail -n 40` every 20 to 60
seconds until a line starting with `RESULT` appears. A dropped SSH session does not stop them.

## Procedure

### 1. Assess (read-only)

1. `detect`. Check: HA reachable, Z2M found (add-on slug or commands), Z2M device count,
   "Z2M network backup: present", radio port and type (`znp` is the tested path), HA Python
   package versions. If the radio type is `ezsp`, `deconz`, `zigate` or `xbee`, tell the user
   that path is untested. If ZHA entries is not 0, stop and ask (a ZHA entry must not exist).
2. `scan-refs`. Entity id references survive. Report everything under `device_ids_BREAK`,
   `entity_uuids_BREAK`, `ieee_BREAK` and `indirect_CHECK` (device triggers/actions, raw
   `zigbee2mqtt/` topics). They must be rewritten (offer to convert them to entity based
   triggers/actions), before or right after the cutover.
3. `suggest-device-config`. If it prints a `zha: device_config` block, those Z2M switches
   would become ZHA lights. Show it to the user. With approval, merge it into
   `configuration.yaml` (back the file up first, keep a single `zha:` key, validate the config)
   and ask the user to restart HA, or restart only if they tell you to.
4. `build-test`. Wants `RESULT: OK` with built == loaded, 0 problems, backup present. Review
   the per-device quirk column: Tuya `_TZE...` devices with "no quirk" usually get few or no
   entities in ZHA. Tell the user which ones. Custom quirks go in `/config/custom_zha_quirks/`
   with `zha: custom_quirks_path` (examples in `examples/quirks/`).
5. Ask about the energy chain: if a Z2M power sensor feeds a Riemann integration / utility
   meter for the Energy dashboard, put both ids in `config.json` under `energy`. Put the most
   important entities in `must_exist`.

Summarise the findings and the risks, then propose the next step.

### 2. Prove the radio (outage steps, ask first)

1. `preflight` (about 1 min outage). Wants `RESULT: PASS - ZHA will see the same network`. A
   FAIL means do not migrate. Report what differed (channel, PAN, extended PAN, key).
2. Optional `listen-test` (2 to 4 min outage, `--seconds 120` default). Routers should mostly
   be `OK`. Sleepy devices are often asleep, which is normal. Radios other than Z-Stack need
   `--allow-write`, explain why (no read-only start) and ask.

### 3. Cutover (ask first, about 1 min outage)

1. Confirm backups exist, the user agrees, and it is a good moment (nobody relies on Zigbee
   for the next 10 minutes).
2. `cutover`, then poll `state/cutover.log`. Possible endings:
   * `RESULT: CUTOVER APPLIED`: go to step 4.
   * `RESULT: ABORTED - <reason>`: Z2M was started again and nothing else changed. Report the
     reason. Typical: preseed not clean (fix in Z2M or quirks), STOP signal
     `choose_automatic_backup` (the radio differs from the backup, do not force it), radio probe
     problems (check `radio.type`, the port and that nothing else holds it).
   * `RESULT: PLAN HAS BLOCKING ROWS`: ZHA runs, the registry is untouched, Z2M is stopped.
     Fix the rows (below), then `plan` and, with approval, `apply --yes`.
   * `RESULT: ZHA NOT LOADED`: investigate the HA log. If it cannot be fixed quickly, offer
     `rollback --yes`.

**Fixing blocking plan rows** (`plan` prints each row with its status):
* `NOMATCH ... ZHA made it a light`: a `device_config` entry is missing. Add it, HA restart,
  `plan` again.
* `BLOCK counter has no value yet`: wait a few minutes for a report, `plan` again.
* `BLOCK unit` or `state_class` change, or `counter jump`: statistics would break. Usually a
  quirk or scaling problem (see `tools/fix_attribute_cache.py` for a missing metering divisor).
  Do not override these without explaining the consequence to the user.
* `NOMATCH ambiguous` or a wrong guess: write `state/overrides.json` as
  `{"<old entity_id>": "<zha entity_id>"}`. Use `null` to drop an entity on purpose, but only
  after the user agrees, because its history stops there.
* `BLOCK referenced in config but no ZHA successor`: something uses that entity. Propose a
  replacement entity and edit the reference, or an override, with the user's approval.

### 4. After the cutover

1. `verify`. `BAD` battery devices are usually fine until they report. Anything else `BAD`
   needs a look. A suffix warning means a rename did not land.
2. If config entities (numbers, selects, thermostats) stay `unknown`, offer `nudge --yes`. It
   writes the captured values back and never toggles relays.
3. The energy estimate in `verify` prints a `utility_meter.calibrate` action. Show it, run it
   only with approval.
4. Check one dashboard and one automation trace with the user. Remind them: Z2M stays
   installed, stopped and on manual boot for a couple of weeks. Do not delete the MQTT
   integration.
5. Report: outage length, mapped/dropped counts, what is still unknown, what needs manual
   follow-up, how to roll back.

### Rollback (ask first)

`rollback --yes` brings Z2M back with the same entity ids and device ids. It works before or
after `apply`. Then run `verify`. If the user added a `zha:` block only for this migration,
offer to remove it afterwards (HA restart needed).

## Troubleshooting

| Symptom | Meaning and fix |
|---|---|
| `no Home Assistant access` | Not a login shell (use `bash -lc`), or set `ha.url` and `HA_TOKEN` |
| `cannot find a Python with zigpy + zha` | Set `ha_python`, e.g. `docker exec -i homeassistant python3` |
| `cannot control Zigbee2MQTT` | No add-on found. Set `z2m.stop_cmd` and `z2m.start_cmd` |
| `serial port still held` | Another process (often a stuck HA probe) has the port. Wait or find it with `detect` |
| Z2M log: "Cannot lock port" | Same cause. The tool waits for the port before starting Z2M |
| First radio read times out | Normal right after Z2M releases the port. The tool retries |
| Many `unknown` thermostats/selects | Report-on-change Tuya devices. `nudge --yes` |
| Energy 100x too high on a plug | Missing metering divisor. `tools/fix_attribute_cache.py` with HA stopped |

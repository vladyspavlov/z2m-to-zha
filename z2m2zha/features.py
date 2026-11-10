"""Zigbee2MQTT features that do not carry over to ZHA by themselves.

READ-ONLY report (`scan-refs`, `capture`), plus the Z2M group move (`groups --yes`, run by `cutover`).

ZHA reads the devices from the radio and from zigbee.db. Everything Z2M did in software (value
calibration, rounding, occupancy timers, debounce), its groups, its external converters and
anything outside Home Assistant that listens to the zigbee2mqtt/ MQTT topics is not part of
that. This module finds those things, so the agent can tell the user before the cutover.
"""
import json
import os
import re

# Z2M device options, by what happens to them in ZHA
SHIFTS_VALUES = re.compile(r"_(calibration|precision)$")
BEHAVIOUR = {"occupancy_timeout", "no_occupancy_since", "invert_cover", "cover_position_tilt_disable",
             "simulated_brightness", "transition", "debounce", "debounce_ignore", "throttle",
             "filtered_attributes", "filtered_cache", "filtered_optimistic", "state_action", "color_sync",
             "unfreeze_support", "legacy", "illuminance_raw", "measurement_poll_interval", "invert",
             "exposes_disable", "disabled"}
MQTT_ONLY = {"retain", "optimistic", "qos", "retention", "icon", "friendly_name", "description"}
GROUP_UID = r"^(\d+)_(.+)"  # Z2M group entities: <group id>_<object id><suffix>
ACTION_PROPS = re.compile(r"^(action|click|action_.+)$")
CONSUMER_ADDONS = re.compile(r"node.?red|appdaemon|homebridge|pyscript|n8n|openhab|domoticz|iobroker|mqtt.?io", re.I)
SCAN_EXT = (".json", ".yaml", ".yml", ".py", ".js", ".ts", ".conf", ".txt")


def group_id(unique_id, sfx):
    """Z2M group id from an entity unique_id, else None."""
    m = re.match(GROUP_UID + re.escape(sfx) + "$", unique_id)
    return int(m.group(1)) if m else None


def zha_group_uid(platform, gid):
    return f"{platform}_zha_group_0x{gid:04x}"


def option_kind(key, value):
    if key in MQTT_ONLY:
        return None
    if key == "homeassistant":
        return "HA_OVERRIDE" if value else None  # empty {} is the Z2M default
    if SHIFTS_VALUES.search(key):
        return None if value in (0, None, "") else "SHIFTS_VALUES"
    if key in BEHAVIOUR:
        return "BEHAVIOUR"
    return "OTHER"


def classify_options(dev_options, names, global_options=None):
    """[(device name, option, value, kind)] for every option that matters in ZHA."""
    out = []
    for key, val in (global_options or {}).items():
        kind = option_kind(key, val)
        if kind:
            out.append(("(all devices)", key, val, kind))
    for ieee, opts in sorted((dev_options or {}).items()):
        for key, val in sorted(opts.items()):
            kind = option_kind(key, val)
            if kind:
                out.append((names.get(ieee, ieee), key, val, kind))
    return out


def groups_from_db(records):
    """Groups from Z2M's database.db (zigbee-herdsman format): {id: [(ieee 0x.., endpoint)]}."""
    out = {}
    for r in records:
        if r.get("type") == "Group":
            gid = r.get("groupID", r.get("id"))
            out[int(gid)] = [(m.get("deviceIeeeAddr"), int(m.get("endpointID", 1))) for m in r.get("members") or []]
    return out


def green_power(records):
    return [r for r in records if r.get("type") == "GreenPower" or str(r.get("modelId") or r.get("modelID") or "")
            .startswith("GreenPower")]


def ieee_colon(z):
    h = z[2:] if z.startswith("0x") else z
    return ":".join(h[i:i + 2] for i in range(0, 16, 2))


def collect(env, z2m_entities, sfx, refs=None):
    """Everything the report and the group move need. z2m_entities are entity registry rows."""
    st = env.z2m.settings()
    records = env.z2m.devices()
    names = env.z2m.friendly_names()
    db_groups = groups_from_db(records)
    yaml_groups = st.get("groups") or {}
    referenced = set((refs or {}).get("entity_ids", []))
    gents = {}
    for e in z2m_entities:
        gid = group_id(e["unique_id"], sfx)
        if gid is not None:
            gents.setdefault(gid, []).append(e["entity_id"])
    groups = []
    for gid in sorted(set(db_groups) | {int(k) for k in yaml_groups} | set(gents)):
        y = yaml_groups.get(str(gid)) or {}
        groups.append({"id": gid, "name": y.get("friendly_name") or f"group {gid}",
                       "members": [{"ieee": ieee_colon(i), "endpoint_id": ep, "name": names.get(i, i)}
                                   for i, ep in db_groups.get(gid, [])],
                       "entities": sorted(gents.get(gid, [])), "options": y.get("options") or {}})
    actions = []
    for e in z2m_entities:
        m = re.match(r"(?:bridge_)?0x[0-9a-f]{16}_(.+)" + re.escape(sfx) + "$", e["unique_id"])
        prop = m.group(1) if m else ""
        if e["entity_id"].startswith("event.") or ACTION_PROPS.match(prop):
            actions.append({"entity_id": e["entity_id"], "referenced": e["entity_id"] in referenced})
    return {"options": classify_options(st.get("device_options"), names, st.get("global_device_options")),
            "groups": groups, "actions": actions, "availability": st.get("availability"),
            "external_converters": sorted(set(st.get("external_converters") or [])
                                          | set(st.get("external_converter_files") or [])),
            "green_power": [r.get("ieeeAddr") for r in green_power(records)],
            "consumers": consumers(env)}


def consumers(env):
    """Other software that may listen to zigbee2mqtt/ topics: add-ons by name, and add-on config files."""
    out = []
    try:
        for a in env.sup.get("/addons").get("addons", []):
            if CONSUMER_ADDONS.search(a.get("name", "") + " " + a.get("slug", "")) and a.get("state") == "started":
                out.append(f"add-on {a['name']} is running")
    except Exception:  # noqa: BLE001 (no Supervisor)
        pass
    base = os.path.join(os.path.dirname(env.ha_config_dir.rstrip("/")), "addon_configs")
    z2m_dirs = {os.path.realpath(env.z2m.data_dir)}
    for root in (base, "/share"):
        if not os.path.isdir(root):
            continue
        for dp, ds, fs in os.walk(root):
            ds[:] = [d for d in ds if d not in ("node_modules", ".git", "__pycache__")
                     and os.path.realpath(os.path.join(dp, d)) not in z2m_dirs and "zigbee2mqtt" not in d]
            for f in fs:
                p = os.path.join(dp, f)
                if not f.endswith(SCAN_EXT) or os.path.getsize(p) > 5_000_000:
                    continue
                try:
                    if "zigbee2mqtt/" in open(p, encoding="utf-8", errors="ignore").read():
                        out.append(f"{p} uses zigbee2mqtt/ topics")
                except OSError:
                    continue
    return out


def report(f):
    """Print what the user must know. Returns the number of items that need a decision."""
    n = 0
    if f["groups"]:
        print("Z2M groups (recreated as ZHA groups with the same id by the cutover, or `groups --yes`):")
        for g in f["groups"]:
            hint = "" if len(g["members"]) >= 2 else "  NOTE: ZHA creates a group entity only with 2+ members"
            print(f"  {g['id']:>5} {g['name']}: {len(g['members'])} members, entities {g['entities'] or '-'}{hint}")
            if g["options"]:
                print(f"        Z2M group options {g['options']} are not applied by ZHA")
        n += sum(1 for g in f["groups"] if len(g["members"]) < 2)
    if f["actions"]:
        print("Remote / button action entities. ZHA has no action sensor: buttons fire zha_event and device "
              "triggers instead. Automations on these must be rewritten (device trigger or zha_event):")
        for a in f["actions"]:
            print(f"  {a['entity_id']}{'  <- referenced in config, REWRITE' if a['referenced'] else ''}")
            n += a["referenced"]
    if f["options"]:
        print("Z2M device options that Z2M applied in software. ZHA does NOT apply them:")
        for name, key, val, kind in f["options"]:
            what = {"SHIFTS_VALUES": "readings will change (statistics see a step). Use the device's own "
                                     "calibration entity if the quirk has one, or a template sensor",
                    "BEHAVIOUR": "behaviour changes (timers, debounce, inversion). Check the ZHA quirk or "
                                 "rebuild it in an automation",
                    "HA_OVERRIDE": "Z2M discovery override (name, device class, unit). Set it in the HA "
                                   "entity settings instead",
                    "OTHER": "unknown option, check its meaning"}[kind]
            print(f"  {name}: {key}={json.dumps(val)}  -> {what}")
            n += kind in ("SHIFTS_VALUES", "BEHAVIOUR")
    av = f.get("availability")
    if av:
        print(f"Z2M availability {json.dumps(av)}: ZHA marks mains devices unavailable after 2 h and battery "
              "devices after 6 h without a message (consider_unavailable_mains / _battery in the ZHA options). "
              "Automations that react to 'unavailable' fire at different times")
    if f["external_converters"]:
        print("Z2M external converters (the devices they cover need a ZHA quirk, see build-test):",
              ", ".join(f["external_converters"]))
        n += 1
    if f["green_power"]:
        print("Green Power devices (not supported by ZHA, they stop working):", ", ".join(f["green_power"]))
        n += len(f["green_power"])
    if f["consumers"]:
        print("Outside Home Assistant, something may use the zigbee2mqtt/ MQTT topics (they go silent):")
        for c in f["consumers"]:
            print("  " + c)
        n += len(f["consumers"])
    if not any(f[k] for k in ("groups", "actions", "options", "external_converters", "green_power", "consumers")):
        print("No Z2M groups, action entities, software device options, converters or outside MQTT users found.")
    return n


def create_groups(ha, groups, zha_ieees, do=None):
    """Create every Z2M group in ZHA with the SAME group id (devices already hold that membership).

    Members not in ZHA are skipped. A group that ZHA already has is left alone. Returns created ids.
    """
    have = {g["group_id"] for g in ha("zha/groups")}
    made = []
    for g in groups:
        members = [{"ieee": m["ieee"], "endpoint_id": m["endpoint_id"]} for m in g["members"]
                   if m["ieee"] in zha_ieees]
        if g["id"] in have:
            print(f"  group {g['id']} {g['name']}: already in ZHA")
            continue
        if not members:
            print(f"  group {g['id']} {g['name']}: no member is in ZHA, skipped")
            continue
        res = ha("zha/group/add", group_name=g["name"], group_id=g["id"], members=members)
        if do:
            do.note("zha_group", group_id=g["id"], name=g["name"], members=members)
        print(f"  group {g['id']} {g['name']}: created with {len(members)} of {len(g['members'])} members "
              f"({len((res or {}).get('members') or [])} confirmed)")
        made.append(g["id"])
    return made

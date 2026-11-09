"""Entity/device registry side of the cutover: capture, plan, apply, polish, nudge, verify, rollback.

Why this keeps history: recorder history and long-term statistics are keyed by the entity_id
string. When the new ZHA entity gets the old entity_id, it appends to the same states_meta and
statistics_meta rows, so history and statistics continue. Statistics only continue cleanly when
unit and state_class match and total_increasing counters continue from the same value, so
`plan` blocks any mismatch.

Ordering traps handled here:
  * switch_as_x deletes itself (and its wrapped entity) when its source entity is REMOVED from
    the registry, and follows renames. It is disabled while the old entities go away.
  * integration (Riemann), utility_meter, generic_thermostat and similar helpers follow renames
    but unlink on removal, so the old Z2M entities are removed (frees the ids), never renamed.
  * The Z2M bridge device has the coordinator's IEEE. Its name must not be copied to the ZHA
    coordinator.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from . import links, util

TOTALS = ("total", "total_increasing")


def z2m_prop(unique_id, suffix):
    m = re.match(r"(?:bridge_)?(0x[0-9a-f]{16})_(.+)" + re.escape(suffix) + "$", unique_id)
    return (m.group(1), m.group(2)) if m else (None, None)


def ieee_colon(z):
    h = z[2:]
    return ":".join(h[i:i + 2] for i in range(0, 16, 2))


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class Logged:
    """Every write goes through here and is appended to <state>/<name>.log as JSON lines."""

    def __init__(self, env, ha, name):
        self.ha, self.f = ha, open(env.state(name), "a")

    def __call__(self, type_, **kw):
        self.note(type_, **kw)
        return self.ha(type_, **kw)

    def note(self, type_, **kw):
        """Log a write that does not go through the websocket (REST flows)."""
        self.f.write(json.dumps({"t": time.time(), "cmd": type_, "kw": kw}) + "\n")
        self.f.flush()


# ---------------------------------------------------------------- capture
def capture(env):
    """READ-ONLY snapshot of everything Z2M-related, taken while Z2M still runs."""
    ha = env.ha()
    sfx = env.cfg["z2m"]["unique_id_suffix"]
    ents = ha("config/entity_registry/list")
    devs = {d["id"]: d for d in ha("config/device_registry/list")}
    states = {s["entity_id"]: s for s in ha("get_states")}
    mqtt = ha("config_entries/get", domain="mqtt")
    if not mqtt:
        raise SystemExit("no MQTT config entry: is this a Zigbee2MQTT install?")
    old = [e for e in ents if e["platform"] == "mqtt" and e["unique_id"].endswith(sfx)]
    det = {e["entity_id"]: e for e in
           ha("config/entity_registry/get_entries", entity_ids=[e["entity_id"] for e in old]).values() if e}
    out_e = []
    for e in old:
        ieee, prop = z2m_prop(e["unique_id"], sfx)
        d, st = det.get(e["entity_id"], e), states.get(e["entity_id"], {})
        at = st.get("attributes", {})
        out_e.append({k: d.get(k) for k in ("entity_id", "unique_id", "device_id", "name", "icon", "area_id",
                                             "disabled_by", "hidden_by", "aliases", "labels", "options",
                                             "original_device_class", "device_class")}
                     | {"ieee": ieee, "prop": prop, "domain": e["entity_id"].split(".")[0],
                        "bridge": e["unique_id"].startswith("bridge_"), "state": st.get("state"),
                        "unit": at.get("unit_of_measurement"), "state_class": at.get("state_class"),
                        "attrs": {k: at.get(k) for k in ("friendly_name", "icon", "device_class",
                                                         "temperature", "hvac_modes", "options") if k in at}})
    dev_ids = {e["device_id"] for e in old if e.get("device_id")}
    paired = {r["ieeeAddr"] for r in env.z2m.devices()}
    out_d = []
    for i in dev_ids:
        if i not in devs:
            continue
        mine = [e for e in out_e if e["device_id"] == i]
        ieee = next((e["ieee"] for e in mine if e["ieee"]), None)
        out_d.append({"device_id": i, "ieee": ieee, "name": devs[i]["name"], "name_by_user": devs[i].get("name_by_user"),
                      "area_id": devs[i].get("area_id"), "labels": devs[i].get("labels") or [],
                      "bridge": any(e["bridge"] for e in mine), "in_z2m": ieee in paired})
    stale = [d["name_by_user"] or d["name"] for d in out_d if not d["in_z2m"] and not d["bridge"]]
    if stale:
        print("HA still has MQTT devices that Z2M no longer has paired (dropped, not blocking):", stale)
    z2m_eids = {e["entity_id"] for e in old}
    cfg = env.storage("core.config_entries")["data"]["entries"]
    sax = [{"entry_id": c["entry_id"], "title": c["title"], "disabled_by": c.get("disabled_by"),
            "source": c["options"].get("entity_id")}
           for c in cfg if c["domain"] == "switch_as_x" and c["options"].get("entity_id") in z2m_eids]
    energy = {k: states.get(v, {}).get("state") for k, v in env.cfg["energy"].items() if v}
    bn = capture_battery_notes(env, cfg, dev_ids, ents)
    lks = links.find(cfg, old, dev_ids, env.cfg["protect_domains"], env.cfg["relink_options_domains"])
    att = links.attached(ents, dev_ids)
    env.save_state("capture.json", {"ts": time.time(), "mqtt_entry": mqtt[0]["entry_id"], "entities": out_e,
                                    "devices": out_d, "switch_as_x": sax, "energy": energy, "battery_notes": bn,
                                    "links": lks, "attached": att})
    durable = f"{env.ha_config_dir}/z2m2zha-backups"
    util.fs("mkdir", "-p", durable)
    util.fs("chmod", "700", durable)
    copy = f"{durable}/capture-{time.strftime('%Y%m%d-%H%M%S')}.json"
    util.write_bytes(copy, util.read_bytes(env.state("capture.json")), 0o600)
    print(f"captured {len(out_e)} Z2M entities on {len(out_d)} devices, {len(sax)} switch_as_x helpers, "
          f"{len(bn['notes'])} battery notes, {len(lks)} other linked config entries "
          f"-> {env.state('capture.json')} (durable copy {copy})")
    print("Other integrations that point at Z2M entities or devices:")
    links.report(lks, att, {d["device_id"]: d["name_by_user"] or d["name"] for d in out_d})


# ---------------------------------------------------------------- Battery Notes (custom integration)
# Battery Notes keeps one subentry per device, keyed by the HA device_id, plus a store with
# battery_last_replaced per device. ZHA devices get NEW device_ids and the subentry reconfigure
# flow cannot change the device. So: capture, delete the old subentry, create a new one on the
# ZHA device (same settings), rename its entities to the old ids, restore the replaced date.
def capture_battery_notes(env, cfg, dev_ids, ents):
    entry = next((c for c in cfg if c["domain"] == "battery_notes"), None)
    if not entry:
        return {"entry_id": None, "notes": []}
    try:
        store = {d["device_id"]: d for d in env.storage("battery_notes.storage")["data"]["devices"]}
    except Exception:  # noqa: BLE001  store layout differs between versions
        store = {}
    notes = []
    for s in entry.get("subentries", []):
        did = s.get("data", {}).get("device_id")
        if did in dev_ids:
            mine = [e for e in ents if e.get("config_subentry_id") == s["subentry_id"]]
            notes.append({"subentry_id": s["subentry_id"], "title": s["title"], "data": s["data"], "device_id": did,
                          "keys": {e["unique_id"].split(f"bn_{did}_", 1)[-1]: e["entity_id"] for e in mine},
                          "last_replaced": store.get(did, {}).get("battery_last_replaced")})
    return {"entry_id": entry["entry_id"], "notes": notes}


def create_battery_note(ha, entry_id, n, new_dev, do):
    """Create a Battery Notes subentry on device new_dev from captured note n."""
    base = "/config/config_entries/subentries/flow"
    r = ha.rest("POST", base, {"handler": [entry_id, "battery_note"], "show_advanced_options": True})
    for _ in range(6):
        if r["type"] == "menu":
            r = ha.rest("POST", f"{base}/{r['flow_id']}", {"next_step_id": "device"})
        elif r["type"] == "form" and r["step_id"] == "device":
            r = ha.rest("POST", f"{base}/{r['flow_id']}", {"device_id": new_dev})
        elif r["type"] == "form" and r["step_id"] == "manual":
            r = ha.rest("POST", f"{base}/{r['flow_id']}", {})
        elif r["type"] == "form" and r["step_id"] == "battery":
            # Top-level fields plus expandable sections. HA rejects None in template selectors
            # (HTTP 400), so None is dropped everywhere and missing required fields get defaults.
            body = {}
            for f in r.get("data_schema", []):
                k = f.get("name")
                if f.get("type") == "expandable":
                    sub = {kk: vv for kk, vv in (n["data"].get(k) or {}).items() if vv is not None}
                    if sub:
                        body[k] = sub
                elif n["data"].get(k) is not None:
                    body[k] = n["data"][k]
                elif f.get("required") and "default" in f:
                    body[k] = f["default"]
            r = ha.rest("POST", f"{base}/{r['flow_id']}", body)
        else:
            break
    if r["type"] != "create_entry":
        print(f"  battery note '{n['title']}': flow ended {r.get('type')} {r.get('reason') or r.get('errors')}"
              ", recreate it by hand")
        return False
    time.sleep(2)
    for e in ha("config/entity_registry/list"):
        if e["platform"] == "battery_notes" and e.get("device_id") == new_dev:
            tgt = n["keys"].get(e["unique_id"].split(f"bn_{new_dev}_", 1)[-1])
            if tgt and tgt != e["entity_id"]:
                do("config/entity_registry/update", entity_id=e["entity_id"], new_entity_id=tgt)
    if n["last_replaced"]:
        do("call_service", domain="battery_notes", service="set_battery_replaced",
           service_data={"device_id": new_dev, "datetime_replaced": n["last_replaced"]})
    print(f"  battery note '{n['title']}' moved, entity ids kept")
    return True


def migrate_battery_notes(ha, cap, devmap, do):
    bn = cap.get("battery_notes") or {}
    zha_of = {d["old_device"]: d["zha_device"] for d in devmap}
    for n in bn.get("notes", []):
        new_dev = zha_of.get(n["device_id"])
        if not new_dev:
            print(f"  battery note '{n['title']}': device not in ZHA, left as is")
            continue
        try:
            do("config_entries/subentries/delete", entry_id=bn["entry_id"], subentry_id=n["subentry_id"])
            time.sleep(1)
        except Exception as e:  # noqa: BLE001  already deleted by an interrupted run
            print(f"  battery note '{n['title']}': old subentry already gone ({str(e)[:60]})")
        try:
            create_battery_note(ha, bn["entry_id"], n, new_dev, do)
        except Exception as e:  # noqa: BLE001  never abort the whole apply on one note
            print(f"  battery note '{n['title']}': FAILED {e}, recreate it by hand")


# ---------------------------------------------------------------- plan
# Z2M property -> how to recognise the ZHA successor on the same IEEE:
#   (domain, predicates). The first predicate that yields exactly one candidate wins.
# ZHA unique_id: <ieee>-<endpoint>[-<cluster>[-<suffix>]]. Clusters: 6 on/off, 8 level,
# 1026 temperature, 1029 humidity, 1030 occupancy, 1280 IAS zone, 1794 metering, 2820 electrical.
def P_dc(*dcs):
    return lambda z: (z.get("original_device_class") or z.get("device_class")) in dcs


def P_tk(*tks):
    return lambda z: z.get("translation_key") in tks


def P_uid(rx):
    return lambda z: re.search(rx, z["unique_id"] or "") is not None


ANY = [lambda z: True]
RULES = {
    "switch": ("switch", [P_uid(r"^[0-9a-f:]{23}-1$"), P_uid(r"-1-6$"), P_tk("switch")]),
    "light": ("light", [P_uid(r"^[0-9a-f:]{23}-1$"), P_uid(r"-1-6$"), *ANY]),
    "cover": ("cover", ANY), "lock": ("lock", ANY), "fan": ("fan", ANY), "climate": ("climate", ANY),
    "update": ("update", ANY),
    # IAS zone first: many contact sensors (Tuya TS0203) also have an On/Off CLIENT cluster that
    # ZHA shows as an "Opening" binary sensor which never changes. Lumi magnets have no IAS zone
    # and report on On/Off, so they fall through to the next predicates.
    "contact": ("binary_sensor", [P_uid(r"-1-1280$"), P_dc("opening", "door", "window"), P_uid(r"-1-6$")]),
    "occupancy": ("binary_sensor", [P_uid(r"-1-1030$"), P_dc("occupancy"), P_dc("motion", "presence")]),
    "presence": ("binary_sensor", [P_dc("occupancy", "presence", "motion")]),
    "vibration": ("binary_sensor", [P_dc("vibration", "moving"), P_uid(r"-1-1280$")]),
    "water_leak": ("binary_sensor", [P_dc("moisture")]),
    "smoke": ("binary_sensor", [P_dc("smoke")]),
    "gas": ("binary_sensor", [P_dc("gas")]),
    "carbon_monoxide": ("binary_sensor", [P_dc("carbon_monoxide")]),
    "tamper": ("binary_sensor", [P_dc("tamper")]),
    "battery_low": ("binary_sensor", [P_dc("battery")]),
    "battery": ("sensor", [P_dc("battery")]),
    "temperature": ("sensor", [P_uid(r"-1026$"), P_dc("temperature")]),
    "device_temperature": ("sensor", [P_dc("temperature")]),
    "local_temperature": ("sensor", [P_dc("temperature")]),
    "humidity": ("sensor", [P_dc("humidity")]),
    "pressure": ("sensor", [P_dc("pressure", "atmospheric_pressure")]),
    "illuminance": ("sensor", [P_dc("illuminance")]),
    "illuminance_lux": ("sensor", [P_dc("illuminance")]),
    "co2": ("sensor", [P_dc("carbon_dioxide")]),
    "pm25": ("sensor", [P_dc("pm25")]),
    "voc": ("sensor", [P_dc("volatile_organic_compounds", "volatile_organic_compounds_parts")]),
    "power": ("sensor", [P_dc("power")]),
    "energy": ("sensor", [lambda z: P_dc("energy")(z) and "receiv" not in (z["unique_id"] + str(z.get("translation_key"))),
                          P_tk("summation_delivered")]),
    "voltage": ("sensor", [P_dc("voltage")]),
    "current": ("sensor", [P_dc("current")]),
    "power_factor": ("sensor", [P_dc("power_factor")]),
    "ac_frequency": ("sensor", [P_dc("frequency")]),
    "linkquality": ("sensor", [P_tk("lqi")]),
    "child_lock": ("switch", [P_tk("child_lock")]),
    "switch_child_lock": ("switch", [P_tk("child_lock")]),
    "local_temperature_calibration": ("number", [P_tk("local_temperature_calibration")]),
    # Config entities. Tuya on/off attributes (ZHA shows them for quirks exposing the Tuya plug feature)
    "power_outage_memory": ("select", [P_uid(r"-6-power_on_state$")]),
    "power_on_behavior": ("select", [P_uid(r"-6-power_on_state$"), P_uid(r"-6-start_up_on_off$")]),
    "indicator_mode": ("select", [P_uid(r"-6-backlight_mode$")]),
    "identify": ("button", [P_uid(r"-1-3$"), P_dc("identify")]),
}
# Mapped when present, dropped (never blocking) when ZHA has no equivalent on that device.
OPTIONAL = {"voltage", "tamper", "child_lock", "switch_child_lock", "linkquality", "battery_low", "update",
            "device_temperature", "power_factor", "ac_frequency", "power_outage_memory", "power_on_behavior",
            "indicator_mode", "identify"}
# Any other number/select/switch/button: a quirk entity whose unique_id suffix (or translation key) is
# the Z2M property name, the convention for custom quirks (see README, Writing a quirk). Not blocking.
GENERIC_DOMAINS = {"number", "select", "switch", "button"}


def is_generic(prop, domain):
    """True when only the generic quirk-suffix rule applies (such rows never block)."""
    return (prop not in RULES and domain in GENERIC_DOMAINS
            and not re.fullmatch(r"(switch|state|light)_l\d+", prop or ""))


def rule_for(prop, domain=None):
    if prop in RULES:
        return RULES[prop]
    m = re.fullmatch(r"(switch|state|light)_l(\d+)", prop)  # multi-gang: Z2M l1/l2 -> ZHA endpoint 1/2
    if m:
        dom = "light" if m.group(1) == "light" else "switch"
        ep = m.group(2)
        return dom, [P_uid(rf"^[0-9a-f:]{{23}}-{ep}$"), P_uid(rf"-{ep}-6$")]
    if domain in GENERIC_DOMAINS and prop:
        return domain, [P_uid(rf"-{re.escape(prop)}$"), P_tk(prop)]
    return None


def plan(env):
    """READ-ONLY. Maps every captured Z2M entity to its ZHA successor. Writes plan.json."""
    cap = env.load_state("capture.json")
    ovr = env.load_state("overrides.json", {})
    refs = set(env.load_state("refs.json", {}).get("entity_ids", []))
    soft = set(env.cfg["soft_counters"])
    ha = env.ha()
    ents = ha("config/entity_registry/list")
    zha_ids = [e["entity_id"] for e in ents if e["platform"] == "zha"]
    zdet = {e["entity_id"]: e for e in ha("config/entity_registry/get_entries", entity_ids=zha_ids).values() if e}
    states = {s["entity_id"]: s for s in ha("get_states")}
    zdev = {}
    for d in ha("config/device_registry/list"):
        for dom, ident in d["identifiers"]:
            if dom == "zha":
                zdev[ident.lower()] = d
    by_ieee = {}
    for z in zdet.values():
        by_ieee.setdefault(z["unique_id"].split("-")[0].lower(), []).append(z)
    paired = {d["ieee"] for d in cap["devices"] if d.get("in_z2m")}

    rows, used, blocks = [], set(), 0
    for o in cap["entities"]:
        r = {"old": o["entity_id"], "prop": o["prop"], "new": None, "status": "", "note": ""}
        rule = rule_for(o["prop"] or "", o["domain"])
        if o["entity_id"] in ovr:
            r["new"] = ovr[o["entity_id"]]
            r["status"] = "OVERRIDE" if r["new"] else "DROP (override)"
        elif o["ieee"] is None or o["bridge"]:
            r["status"] = "DROP (Z2M bridge entity, no ZHA equivalent)"
        else:
            ie = ieee_colon(o["ieee"])
            cands = [z for z in by_ieee.get(ie, []) if rule and z["entity_id"].split(".")[0] == rule[0]
                     and z["entity_id"] not in used]
            if ie not in zdev:
                r["status"] = ("BLOCK device not in ZHA" if o["ieee"] in paired
                               else "DROP (device not paired in Z2M any more)")
            elif not rule:
                r["status"] = "DROP (Z2M-only attribute, old history stays readable)"
            elif rule[0] != o["domain"]:
                # Z2M often has a read-only sensor twin of a number/select/switch. Only the entity
                # of the SAME domain may take the ZHA successor.
                r["status"] = f"DROP (Z2M {o['domain']} twin of a {rule[0]})"
            else:
                for pred in rule[1]:
                    hit = [z for z in cands if pred(z)]
                    if len(hit) == 1:
                        r["new"] = hit[0]["entity_id"]
                        break
                    if len(hit) > 1:
                        r["note"] = "ambiguous: " + ", ".join(z["entity_id"] for z in hit)
                optional = o["prop"] in OPTIONAL or is_generic(o["prop"], o["domain"])
                if not r["new"] and optional and not r["note"].startswith("ambiguous"):
                    r["status"] = "DROP (no ZHA equivalent on this device, old history stays readable)"
                elif not r["new"]:
                    hint = ""
                    if o["domain"] == "switch" and any(z["entity_id"].startswith("light.") for z in by_ieee.get(ie, [])):
                        hint = " ZHA made it a light: add zha device_config type switch (see suggest-zha-config)"
                    r["status"] = "NOMATCH " + r["note"] + hint
        if r["new"]:
            used.add(r["new"])
            zs = states.get(r["new"], {})
            za = zs.get("attributes", {})
            nu, nsc, nv, ov = za.get("unit_of_measurement"), za.get("state_class"), num(zs.get("state")), num(o["state"])
            problems = []
            if o["unit"] != nu:
                problems.append(f"unit {o['unit']}->{nu}")
            if o["state_class"] != nsc and o["entity_id"] in soft:
                r["note"] += f" (state_class {o['state_class']}->{nsc} accepted, soft counter)"
            elif o["state_class"] != nsc:
                problems.append(f"state_class {o['state_class']}->{nsc}")
            if o["state_class"] in TOTALS and ov is not None and o["entity_id"] not in soft:
                if nv is None:
                    problems.append("counter has no value yet (wait for a report)")
                elif not (ov * 0.98 - 0.05 <= nv <= ov * 1.05 + 0.5):
                    problems.append(f"counter jump {ov}->{nv}")
            elif o["state_class"] == "measurement" and ov and nv and max(abs(ov), abs(nv)) > 5:
                if not 0.2 <= abs(nv) / max(abs(ov), 1e-9) <= 5:
                    r["note"] += f" WARN scale? {ov}->{nv}"
            if zdet.get(r["new"], {}).get("disabled_by") and not o["disabled_by"]:
                r["note"] += " (will be enabled)"
            if problems and o["state_class"]:  # only entities WITH statistics can be corrupted
                r["status"] = "BLOCK " + ", ".join(problems)
            else:
                r["status"] = "MAP" + (f" (note: {', '.join(problems)})" if problems else "")
        if r["status"].startswith("DROP") and o["entity_id"] in refs and o["entity_id"] not in ovr:
            r["status"] = "BLOCK referenced in config but no ZHA successor (" + r["status"] + ")"
        r["blocking"] = r["status"].startswith(("BLOCK", "NOMATCH")) and o["prop"] not in OPTIONAL \
            and not is_generic(o["prop"], o["domain"])
        blocks += r["blocking"]
        rows.append(r)
    devmap = []
    for d in cap["devices"]:
        z = zdev.get(ieee_colon(d["ieee"])) if d["ieee"] else None
        devmap.append({"old_device": d["device_id"], "zha_device": z and z["id"], "bridge": d["bridge"],
                       "name": d["name_by_user"] or d["name"], "area_id": d["area_id"], "labels": d["labels"]})
    env.save_state("plan.json", {"rows": rows, "devices": devmap, "blocks": blocks})
    for r in sorted(rows, key=lambda r: (r["status"][:4], r["old"])):
        print(f"{r['status'][:62]:62} {r['old']:55} -> {r['new'] or '-'} {r['note']}")
    print(f"\n{sum(r['status'].startswith(('MAP', 'OVERRIDE')) for r in rows)} mapped, "
          f"{sum(r['status'].startswith('DROP') for r in rows)} dropped, {blocks} blocking")
    print("Fix blocking rows with state/overrides.json ({\"old.entity_id\": \"zha.entity_id\" or null}), "
          "or wait for a report, then plan again." if blocks else "Plan OK. Next: apply --yes")
    return blocks


# ---------------------------------------------------------------- apply
def apply(env):
    util.confirm()
    cap, pl = env.load_state("capture.json"), env.load_state("plan.json")
    if pl["blocks"]:
        raise SystemExit(f"refusing: plan has {pl['blocks']} blocking rows")
    if env.z2m.is_running():
        raise SystemExit("refusing: Zigbee2MQTT is running. Stop it (and keep it stopped) first")
    ha = env.ha()
    do = Logged(env, ha, "apply.log")
    # 1. helpers that follow their source entity off, so removing the source cannot delete them
    #    (switch_as_x, statistics, trend, history_stats) or unlink them (see links.py)
    prot = links.protect_list(cap)
    links.set_disabled(do, prot, True, "1. disabled")
    time.sleep(3)
    # 2. remove the Z2M devices from MQTT (frees the entity ids, keeps registry tombstones)
    for d in cap["devices"]:
        try:
            do("config/device_registry/remove_config_entry", device_id=d["device_id"],
               config_entry_id=cap["mqtt_entry"])
        except Exception as e:  # noqa: BLE001
            print("   remove failed (already gone?):", str(e)[:100])
    time.sleep(5)
    sfx = env.cfg["z2m"]["unique_id_suffix"]
    left = [e["entity_id"] for e in ha("config/entity_registry/list")
            if e["platform"] == "mqtt" and e["unique_id"].endswith(sfx)]
    print("2. removed the Z2M devices" + (f", WARNING still present: {left}" if left else ""))
    # 3. ZHA devices get the Z2M device name, area and labels. Never the coordinator (bridge).
    for d in pl["devices"]:
        if d["zha_device"] and not d["bridge"]:
            do("config/device_registry/update", device_id=d["zha_device"], name_by_user=d["name"],
               area_id=d["area_id"], labels=d["labels"])
    print("3. device names/areas copied")
    # 4. ZHA entities onto the old entity ids, with the user's customisations
    old = {e["entity_id"]: e for e in cap["entities"]}
    n = 0
    for r in pl["rows"]:
        if not r["new"] or not r["status"].startswith(("MAP", "OVERRIDE")):
            continue
        o = old[r["old"]]
        kw = {"entity_id": r["new"], "area_id": o["area_id"], "aliases": o.get("aliases") or [],
              "labels": o.get("labels") or [], "disabled_by": "user" if o["disabled_by"] else None,
              "hidden_by": "user" if o["hidden_by"] else None}
        if r["new"] != r["old"]:
            kw["new_entity_id"] = r["old"]
        try:
            do("config/entity_registry/update", **kw)
            n += 1
        except Exception as e:  # noqa: BLE001
            print("   FAILED", r["old"], e)
            continue
        for dom, opts in (o.get("options") or {}).items():
            if dom not in ("mqtt", "zha") and opts:  # display precision, unit, voice exposure...
                try:
                    do("config/entity_registry/update", entity_id=r["old"], options_domain=dom, options=opts)
                except Exception as e:  # noqa: BLE001
                    print("   options not copied", r["old"], str(e)[:100])
    print(f"4. {n} ZHA entities now use the old entity ids")
    migrate_battery_notes(ha, cap, pl["devices"], do)
    links.set_disabled(do, prot, False, "5. re-enabled")
    zha_of = {d["old_device"]: d["zha_device"] for d in pl["devices"] if d.get("zha_device")}
    links.relink(env, ha, do, cap, zha_of)
    reload_helpers(env, ha)
    n = links.reload_linked(ha, cap, skip_ids={c["entry_id"] for c in prot})
    print(f"6. reloaded {n} other config entries that reference Z2M entities")
    names = {d["device_id"]: d["name_by_user"] or d["name"] for d in cap["devices"]}
    for lk in [x for x in cap.get("links", []) if x["action"] == "MANUAL"]:
        devs = ", ".join(f"{names.get(d, d)}: {d} -> {zha_of.get(d, 'no ZHA device')}" for d in lk["device_ids"])
        print(f"   MANUAL {lk['domain']}: {lk['title']} | {devs or ''} {lk['entity_uuids'] or ''}".rstrip())
    time.sleep(3)
    polish(env, ha)
    print("apply done. Next: verify, then nudge if some entities stay unknown")


def reload_helpers(env, ha):
    for dom in env.cfg["helper_reload_domains"]:
        for c in ha("config_entries/get", domain=dom):
            try:
                ha.rest("POST", f"/config/config_entries/entry/{c['entry_id']}/reload")
            except Exception as e:  # noqa: BLE001
                print("   reload failed", c["title"], str(e)[:80])


def polish(env, ha=None):
    """Make friendly names, icons and device classes look exactly like before.

    HA shows an entity as "<device name> <entity name>". ZHA names entities differently from Z2M,
    so the old friendly_name (captured from the state) is split into device name + entity name.
    """
    ha = ha or env.ha()
    cap = env.load_state("capture.json")
    do = Logged(env, ha, "apply.log")
    devs = {d["id"]: d for d in ha("config/device_registry/list")}
    reg = {e["entity_id"]: e for e in ha("config/entity_registry/list")}
    states = {s["entity_id"]: s for s in ha("get_states")}
    changed = 0
    for o in cap["entities"]:
        r, was = reg.get(o["entity_id"]), o.get("attrs") or {}
        if not r or r["platform"] != "zha":
            continue
        now = states.get(o["entity_id"], {}).get("attributes", {})
        kw = {}
        if was.get("icon") and was["icon"] != now.get("icon"):
            kw["icon"] = was["icon"]
        if was.get("device_class") and was["device_class"] != now.get("device_class"):
            kw["device_class"] = was["device_class"]
        ofn = was.get("friendly_name")
        if ofn and ofn != now.get("friendly_name"):
            d = devs.get(r.get("device_id"), {})
            dn = d.get("name_by_user") or d.get("name") or ""
            if ofn.startswith(dn + " "):
                kw["name"] = ofn[len(dn) + 1:]
            elif ofn != dn:
                kw["name"] = ofn
        if kw:
            try:
                do("config/entity_registry/update", entity_id=o["entity_id"], **kw)
                changed += 1
            except Exception as e:  # noqa: BLE001
                print("   polish failed", o["entity_id"], str(e)[:100])
    for d in devs.values():  # coordinator trap: Z2M bridge name copied by an older run
        if any(i[0] == "zha" for i in d["identifiers"]) and d.get("name_by_user") == "Zigbee2MQTT Bridge":
            do("config/device_registry/update", device_id=d["id"], name_by_user=None)
    time.sleep(2)
    states = {s["entity_id"]: s for s in ha("get_states")}
    diff = []
    for o in cap["entities"]:
        if reg.get(o["entity_id"], {}).get("platform") != "zha":
            continue
        now = states.get(o["entity_id"], {}).get("attributes", {})
        d = {k: (v, now.get(k)) for k, v in (o.get("attrs") or {}).items()
             if k in ("friendly_name", "icon", "device_class") and v != now.get(k)}
        if d:
            diff.append((o["entity_id"], d))
    print(f"polish: {changed} entities adjusted, {len(diff)} still differ")
    for e, d in diff:
        print("   ", e, d)


# ---------------------------------------------------------------- nudge
def nudge(env):
    """Write the captured values back to config entities that stay unknown after the cutover.

    Some Tuya devices only report on change and ignore the data query, so their numbers,
    selects and thermostat state stay unknown in ZHA until something changes. Writing the same
    value makes them report. Only entities that are unknown NOW are touched.
    """
    util.confirm()
    cap = env.load_state("capture.json")
    ha = env.ha()
    states = {s["entity_id"]: s for s in ha("get_states")}
    for o in cap["entities"]:
        eid, v = o["entity_id"], o["state"]
        cur = states.get(eid)
        if not cur or cur["state"] not in ("unknown",) or v in (None, "unknown", "unavailable"):
            continue
        dom = eid.split(".")[0]
        try:
            if dom == "number":
                ha("call_service", domain="number", service="set_value", service_data={"entity_id": eid, "value": float(v)})
            elif dom == "select":
                opts = cur["attributes"].get("options") or []
                opt = next((x for x in opts if x.lower() == v.lower()), None)  # ZHA capitalises options
                if opt is None:
                    print("   skip", eid, "no matching option for", v)
                    continue
                ha("call_service", domain="select", service="select_option", service_data={"entity_id": eid, "option": opt})
            elif dom == "climate":
                ha("call_service", domain="climate", service="set_hvac_mode", service_data={"entity_id": eid, "hvac_mode": v})
                t = (o.get("attrs") or {}).get("temperature")
                if t is not None and v != "off":
                    ha("call_service", domain="climate", service="set_temperature",
                       service_data={"entity_id": eid, "temperature": t})
            # relays are never toggled, only config switches (child lock, sound, ...)
            elif dom == "switch" and not re.fullmatch(r"(switch|state)(_l\d+)?", o["prop"] or "switch"):
                ha("call_service", domain="switch", service=f"turn_{v}", service_data={"entity_id": eid})
            else:
                continue
            print("   wrote", eid, "=", v)
            time.sleep(1)
        except Exception as e:  # noqa: BLE001
            print("   FAILED", eid, v, str(e)[:100])


# ---------------------------------------------------------------- verify
def verify(env):
    cap = env.load_state("capture.json")
    pl = env.load_state("plan.json", {"rows": []})
    ha = env.ha()
    reg = {e["entity_id"]: e for e in ha("config/entity_registry/list")}
    states = {s["entity_id"]: s for s in ha("get_states")}
    must = {r["old"] for r in pl["rows"] if r["new"]} | set(env.cfg["must_exist"])
    must |= {v for v in env.cfg["energy"].values() if v}
    must |= {e["entity_id"] for e in reg.values() if e["platform"] == "switch_as_x"}
    must |= set(env.load_state("refs.json", {}).get("entity_ids", [])) & {e["entity_id"] for e in cap["entities"]}
    bad = 0
    for eid in sorted(must):
        e, s = reg.get(eid), states.get(eid)
        good = e is not None and s is not None and s["state"] not in ("unavailable", "unknown")
        bad += not good
        print(f"{'OK ' if good else 'BAD'} {eid:58} {e['platform'] if e else 'MISSING':12} {s['state'] if s else '-'}")
    print(f"\n{bad} of {len(must)} not OK (battery devices may stay unknown until they report)")
    # A ZHA entity named like an old Z2M id plus _2 means the rename onto the old id did not happen
    old_ids = {e["entity_id"] for e in cap["entities"]}
    dup = [e for e in reg if reg[e]["platform"] == "zha" and e not in old_ids and re.sub(r"_\d+$", "", e) in old_ids]
    if dup:
        print("ZHA entities that look like an old id plus a suffix (rename did not land):", dup)
    if "links" in cap:
        print()
        links.check(ha, env, cap, {d["old_device"]: d["zha_device"] for d in pl.get("devices", []) if d.get("zha_device")})
    energy_gap(env, ha, states, cap)


def energy_gap(env, ha, states, cap):
    """Estimate the energy not integrated while the power sensor was unavailable.

    A Riemann integration helper does not integrate across a period where its source is
    unavailable. The gap is estimated as a trapezoid between the last valid power value before
    and the first valid value after it (the method the Riemann helper uses).
    """
    pw, um = env.cfg["energy"].get("power"), env.cfg["energy"].get("utility_meter")
    if not pw:
        print("\nENERGY: set energy.power (and energy.utility_meter) in the config to measure the gap")
        return
    since = cap["ts"]
    if os.path.exists(env.state("rollback.json")):
        since = max(since, env.load_state("rollback.json")["ts"])
    t0 = datetime.fromtimestamp(since - 600, timezone.utc).isoformat()
    hist = ha.rest("GET", f"/history/period/{t0}?filter_entity_id={pw}&minimal_response&no_attributes")
    pts = [(datetime.fromisoformat(x.get("last_changed") or x["last_updated"]), num(x["state"]))
           for x in (hist[0] if hist else [])]
    gaps = []
    for (ta, va), (tb, vb) in zip(pts, pts[1:]):
        if va is not None and vb is None:
            nxt = next(((t, v) for t, v in pts if t > tb and v is not None), None)
            if nxt and nxt[0] - ta > timedelta(minutes=2):
                gaps.append((ta, va, nxt[0], nxt[1]))
    if not gaps:
        print(f"\nENERGY: no unavailable gap in {pw} history (yet)")
        return
    ta, va, tb, vb = max(gaps, key=lambda g: g[2] - g[0])
    hours = (tb - ta).total_seconds() / 3600
    kwh = round((va + vb) / 2 * hours / 1000, 3)
    print(f"\nENERGY: {pw} gap {ta:%H:%M:%S}->{tb:%H:%M:%S} UTC ({hours * 60:.1f} min), {va:.0f} W -> {vb:.0f} W, "
          f"about {kwh} kWh not integrated (assumes W)")
    u = num(states.get(um, {}).get("state")) if um else None
    if u is not None and kwh > 0.005:
        print("To book it (review first, one time):\n"
              f"  action: utility_meter.calibrate\n  target: {um}\n  value: {round(u + kwh, 3)}")


# ---------------------------------------------------------------- rollback (ZHA back to Z2M)
# Why this is safe:
#  * Deleting the ZHA config entry only unloads ZHA. It sends no leave to devices and does not
#    touch the radio. (NEVER use "Remove device" in ZHA: that one sends a leave.)
#  * The radio keeps PAN, extended PAN, channel and keys, so zigbee-herdsman resumes the network
#    like on every normal Z2M restart.
#  * When apply removed the MQTT devices, HA kept registry tombstones (deleted_entities and
#    deleted_devices). They do not expire while the MQTT entry exists. When Z2M publishes
#    discovery again, HA restores the SAME device_id and entity_id with the customisations.
def rollback(env):
    util.confirm()
    cap = env.load_state("capture.json")
    ha = env.ha()
    do = Logged(env, ha, "rollback.log")
    zha_entries = ha("config_entries/get", domain="zha")
    reg = {e["entity_id"]: e for e in ha("config/entity_registry/list")}
    applied = any(reg.get(e["entity_id"], {}).get("platform") == "zha" for e in cap["entities"])
    print(f"ZHA entries: {len(zha_entries)}, registry cutover applied: {applied}")
    t_start = time.time()
    if zha_entries and zha_entries[0]["state"] == "loaded":
        try:
            b = ha("zha/network/backups/create")
            env.save_state(f"zha_network_backup_{int(t_start)}.json", b, 0o600)
            print("0. fresh ZHA network backup saved in the state dir (0600, contains the key)")
        except Exception as e:  # noqa: BLE001
            print("0. WARNING no ZHA network backup:", str(e)[:100])
    prot = links.protect_list(cap)
    links.set_disabled(do, prot, True, "1. disabled")
    time.sleep(3)
    bn = cap.get("battery_notes") or {}
    if bn.get("entry_id") and applied:
        zha_devs = {d["id"] for d in ha("config/device_registry/list") if any(i[0] == "zha" for i in d["identifiers"])}
        entry = next((c for c in env.storage("core.config_entries")["data"]["entries"]
                      if c["entry_id"] == bn["entry_id"]), {"subentries": []})
        for s in entry.get("subentries", []):
            if s.get("data", {}).get("device_id") in zha_devs:
                do("config_entries/subentries/delete", entry_id=bn["entry_id"], subentry_id=s["subentry_id"])
        print("2. removed the Battery Notes on ZHA devices")
    for z in zha_entries:
        ha.rest("DELETE", f"/config/config_entries/entry/{z['entry_id']}")
    print(f"3. deleted {len(zha_entries)} ZHA config entries (no leave sent, radio untouched)")
    env.wait_port_free(60)
    env.z2m.start()
    print("4. Zigbee2MQTT started, waiting for its MQTT discovery to bring the entities back")
    want = {e["unique_id"]: e for e in cap["entities"]}
    deadline = time.time() + 600
    while True:
        cur = {e["unique_id"]: e for e in ha("config/entity_registry/list")
               if e["platform"] == "mqtt" and e["unique_id"] in want}
        if len(cur) >= len(want) or time.time() > deadline:
            break
        time.sleep(15)
    missing = sorted(want[u]["entity_id"] for u in set(want) - set(cur))
    print(f"   {len(cur)}/{len(want)} Z2M entities back" + (f", MISSING: {missing}" if missing else ""))
    for u, e in cur.items():  # tombstones should restore everything. Re-assert anyway.
        o, kw = want[u], {}
        if e["entity_id"] != o["entity_id"]:
            kw["new_entity_id"] = o["entity_id"]
        for k in ("name", "icon", "area_id"):
            if e.get(k) != o.get(k):
                kw[k] = o.get(k)
        if bool(e.get("disabled_by")) != bool(o["disabled_by"]):
            kw["disabled_by"] = "user" if o["disabled_by"] else None
        if bool(e.get("hidden_by")) != bool(o["hidden_by"]):
            kw["hidden_by"] = "user" if o["hidden_by"] else None
        if kw:
            try:
                do("config/entity_registry/update", entity_id=e["entity_id"], **kw)
            except Exception as err:  # noqa: BLE001
                print("   FAILED to fix", o["entity_id"], str(err)[:80])
    dev_now = {want[u]["device_id"]: e["device_id"] for u, e in cur.items()
               if want[u].get("device_id") and e.get("device_id")}
    devs = {d["id"]: d for d in ha("config/device_registry/list")}
    for d in cap["devices"]:
        nd = devs.get(dev_now.get(d["device_id"], ""))
        if nd and (nd.get("name_by_user") != d["name_by_user"] or nd.get("area_id") != d["area_id"]):
            do("config/device_registry/update", device_id=nd["id"], name_by_user=d["name_by_user"], area_id=d["area_id"])
    print(f"5. devices back: {len(dev_now)}/{len(cap['devices'])}, "
          f"{sum(k == v for k, v in dev_now.items())} with their original device_id")
    if bn.get("entry_id") and applied:
        for n in bn["notes"]:
            if n["device_id"] in dev_now:
                create_battery_note(ha, bn["entry_id"], n, dev_now[n["device_id"]], do)
    if os.path.exists(env.state("relinked.json")):
        links.relink(env, ha, do, cap, dev_now, undo=True)
    links.set_disabled(do, prot, False, "6. re-enabled")
    reload_helpers(env, ha)
    links.reload_linked(ha, cap, skip_ids={c["entry_id"] for c in prot})
    env.save_state("rollback.json", {"ts": t_start})
    print("RESULT: rollback done. Run verify (the energy gap is measured from the rollback time)")


def battery_notes(env):
    """Re-run only the Battery Notes move (after an interrupted apply)."""
    util.confirm()
    ha = env.ha()
    migrate_battery_notes(ha, env.load_state("capture.json"), env.load_state("plan.json")["devices"],
                          Logged(env, ha, "apply.log"))


def orphans(env):
    """List (and with --yes remove) ZHA entities that ZHA no longer provides.

    Typical case: a device_config override added after ZHA already ran leaves the old light.*
    entity behind as "no longer provided" (state restored, unavailable). Entities that now carry
    a migrated Z2M entity_id are never touched.
    """
    cap = env.load_state("capture.json", {"entities": []})
    keep = {e["entity_id"] for e in cap["entities"]}
    ha = env.ha()
    states = {s["entity_id"]: s for s in ha("get_states")}
    found = [e for e in ha("config/entity_registry/list") if e["platform"] == "zha" and e["entity_id"] not in keep
             and not e.get("disabled_by")
             and states.get(e["entity_id"], {}).get("attributes", {}).get("restored")]
    for e in found:
        print("   orphan:", e["entity_id"], "| unique_id", e["unique_id"])
    if not found:
        print("no orphaned ZHA entities")
        return
    if "--yes" not in sys.argv:
        print(f"{len(found)} orphaned ZHA entities. Remove them with: orphans --yes")
        return
    do = Logged(env, ha, "apply.log")
    for e in found:
        do("config/entity_registry/remove", entity_id=e["entity_id"])
    print(f"removed {len(found)} orphaned ZHA entities")

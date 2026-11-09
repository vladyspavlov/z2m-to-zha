"""Other integrations that point at Z2M entities or devices, and what the cutover does with them.

Entity id strings survive the cutover (the ZHA entities are renamed onto them). Entity registry
UUIDs and device ids do not: the ZHA entities and devices are new registry entries. So every
config entry outside MQTT and ZHA (its data, options and subentries) is searched for Z2M entity
ids, Z2M entity UUIDs and Z2M device ids, and each hit gets one of these actions:

  PROTECT   A helper that follows its source entity (HA core: switch_as_x, statistics, trend,
            history_stats, derivative, integration, utility_meter, threshold, generic_thermostat,
            generic_hygrostat, mold_indicator, plus `protect_domains`). switch_as_x, statistics,
            trend and history_stats DELETE their own config entry when the source entity is
            removed, the others unlink their entities from the device. The entry is disabled
            while the Z2M entities are removed and enabled again afterwards. Its setup then finds
            the source under the same entity id and links to the ZHA device by itself.
  RELINK    Stores a Z2M device id in its options, domain in `relink_options_domains` (default
            template). The options flow is run with the ZHA device id. Undone by rollback.
  HANDLED   Has its own handler in registry.py (Battery Notes: one subentry per device).
  MANUAL    Stores a Z2M device id or entity UUID that cannot be moved through an API. Reported
            with the old device and its ZHA successor, so the user can repoint it in the UI.
  KEEP      References entity ids only. Nothing to do, the entry is reloaded after apply so it
            picks up the renamed entities.

HomeKit bridges are reported separately: HomeKit derives an accessory id from the entity unique
id, so Apple Home sees migrated entities as new accessories (rooms and scenes in the Home app
have to be redone for them). Nothing in HA can carry the old accessory id over.

To support another integration that keys on device ids (like Battery Notes), add its domain to
HANDLED_DOMAINS and give it capture and migrate functions in registry.py, or, when its options
flow is a single form with the device field, just add it to `relink_options_domains`.
"""
import re
import time

SELF_DELETING = {"switch_as_x", "statistics", "trend", "history_stats"}
SOURCE_TRACKING = SELF_DELETING | {"derivative", "integration", "utility_meter", "threshold",
                                   "generic_thermostat", "generic_hygrostat", "mold_indicator"}
HANDLED_DOMAINS = {"battery_notes"}
SKIP_DOMAINS = {"mqtt", "zha"}
HEX32 = re.compile(r"\b[0-9a-f]{32}\b")


def _eid_regex(eids):
    if not eids:
        return None
    return re.compile(r"(?<![a-z0-9_.])(" + "|".join(map(re.escape, sorted(eids, key=len, reverse=True)))
                      + r")(?![a-z0-9_])")


def _scan(obj, path, ctx):
    """Collect Z2M entity ids, entity UUIDs and device ids found anywhere in obj."""
    rx, uuids, dev_ids, hits, dev_keys = ctx
    if isinstance(obj, dict):
        for k, v in obj.items():
            _scan(v, path + [k], ctx)
    elif isinstance(obj, list):
        for v in obj:
            _scan(v, path, ctx)
    elif isinstance(obj, str):
        if rx:
            hits["entity_ids"] |= set(rx.findall(obj))
        for h in HEX32.findall(obj):
            if h in uuids:
                hits["entity_uuids"].add(h)
            if h in dev_ids:
                hits["device_ids"].add(h)
                if len(path) == 2 and path[0] == "options" and obj == h:
                    dev_keys.add(path[1])  # a top level option that is exactly the device id


def find(cfg_entries, old_entities, dev_ids, protect_domains=(), relink_domains=()):
    """Return one record per config entry that points at a Z2M entity or device.

    cfg_entries: core.config_entries entries. old_entities: registry entries of the Z2M entities
    (need entity_id and id). dev_ids: the Z2M device ids.
    """
    rx = _eid_regex({e["entity_id"] for e in old_entities})
    uuids = {e["id"]: e["entity_id"] for e in old_entities if e.get("id")}
    dev_ids = set(dev_ids)
    protect = SOURCE_TRACKING | set(protect_domains)
    out = []
    for c in cfg_entries:
        dom = c["domain"]
        if dom in SKIP_DOMAINS:
            continue
        hits = {"entity_ids": set(), "entity_uuids": set(), "device_ids": set()}
        dev_keys = set()
        ctx = (rx, uuids, dev_ids, hits, dev_keys)
        _scan(c.get("data") or {}, ["data"], ctx)
        _scan(c.get("options") or {}, ["options"], ctx)
        for s in c.get("subentries") or []:
            _scan(s.get("data") or {}, ["subentries"], ctx)
        if not any(hits.values()):
            continue
        refs_entities = hits["entity_ids"] or hits["entity_uuids"]
        if dom in HANDLED_DOMAINS:
            action = "HANDLED"
        elif dom in relink_domains and hits["device_ids"] and dev_keys and not hits["entity_uuids"]:
            action = "RELINK"
        elif hits["device_ids"] or hits["entity_uuids"]:
            action = "MANUAL"
        else:
            action = "KEEP"
        out.append({"entry_id": c["entry_id"], "domain": dom, "title": c.get("title"),
                    "disabled_by": c.get("disabled_by"), "action": action,
                    "protect": dom in protect and bool(refs_entities),
                    "self_deleting": dom in SELF_DELETING,
                    "relink_keys": sorted(dev_keys),
                    "entity_ids": sorted(hits["entity_ids"]),
                    "entity_uuids": sorted(f"{u} ({uuids[u]})" for u in hits["entity_uuids"]),
                    "device_ids": sorted(hits["device_ids"]),
                    "homekit": dom == "homekit" and bool(refs_entities)})
    return out


def attached(entities, dev_ids):
    """Registry entities of other integrations that sit on a Z2M device (they lose it)."""
    dev_ids = set(dev_ids)
    return [{"entity_id": e["entity_id"], "platform": e["platform"], "config_entry_id": e.get("config_entry_id"),
             "device_id": e["device_id"]}
            for e in entities if e.get("device_id") in dev_ids and e["platform"] not in ("mqtt", "zha")]


def report(links, att, dev_names=None, indent="  "):
    """Print what the cutover will do with each linked config entry."""
    dev_names = dev_names or {}
    if not links and not att:
        print(f"{indent}no other integration points at Z2M entities or devices")
        return
    for lk in sorted(links, key=lambda x: (x["action"], x["domain"], x["title"] or "")):
        what = []
        if lk["entity_ids"]:
            what.append(f"{len(lk['entity_ids'])} entity ids")
        if lk["entity_uuids"]:
            what.append(f"entity UUIDs {lk['entity_uuids']}")
        if lk["device_ids"]:
            what.append("devices " + ", ".join(dev_names.get(d, d) for d in lk["device_ids"]))
        tag = lk["action"] + ("+PROTECT" if lk["protect"] else "")
        note = ""
        if lk["self_deleting"]:
            note = " (deletes itself when its source goes away, disabled during the swap)"
        elif lk["protect"]:
            note = " (follows its source entity, disabled during the swap)"
        elif lk["action"] == "RELINK":
            note = f" (options {lk['relink_keys']} moved to the ZHA device)"
        elif lk["action"] == "MANUAL":
            note = " (repoint it in the UI after apply, verify lists it)"
        print(f"{indent}{tag:16} {lk['domain']}: {lk['title']} | {', '.join(what)}{note}")
        if lk["homekit"]:
            print(f"{indent}{'':16} HomeKit: accessory ids follow the entity unique id, Apple Home will see the "
                  "migrated entities as new accessories")
    others = {}
    for a in att:
        others.setdefault(a["platform"], []).append(a["entity_id"])
    for plat, eids in sorted(others.items()):
        how = ("moved by its handler" if plat in HANDLED_DOMAINS else
               "they lose the device when the Z2M device goes away, the reload after apply links them to the "
               "ZHA device, verify checks it")
        print(f"{indent}{'ATTACHED':16} {plat}: {len(eids)} entities on Z2M devices {eids[:3]}"
              f"{' ...' if len(eids) > 3 else ''} ({how})")


def protect_list(cap):
    """Entries to disable during the swap. Old captures only know switch_as_x."""
    if "links" in cap:
        return [lk for lk in cap["links"] if lk["protect"] and not lk["disabled_by"]]
    return [{"entry_id": c["entry_id"], "title": c["title"], "domain": "switch_as_x"}
            for c in cap.get("switch_as_x", []) if not c["disabled_by"]]


def set_disabled(do, entries, disabled, label):
    n = 0
    for c in entries:
        try:
            do("config_entries/disable", entry_id=c["entry_id"], disabled_by="user" if disabled else None)
            n += 1
        except Exception as e:  # noqa: BLE001  entry may be gone already
            print(f"   {'disable' if disabled else 'enable'} failed {c.get('domain')}: {c.get('title')} {str(e)[:80]}")
    print(f"{label} {n} helpers that follow a Z2M entity "
          f"({', '.join(sorted({c.get('domain', '?') for c in entries})) or 'none'})")


def relink(env, ha, do, cap, mapping, undo=False):
    """Run the options flow of RELINK entries with the device ids swapped (mapping old -> new).

    Only top level options whose value is exactly a mapped device id are changed, every other
    option is sent back unchanged. A flow that is not one plain form is aborted and reported.
    Writes state/relinked.json so rollback can undo it.
    """
    done = env.load_state("relinked.json", []) if undo else []
    targets = done if undo else [lk for lk in cap.get("links", []) if lk["action"] == "RELINK"]
    entries = {c["entry_id"]: c for c in env.storage("core.config_entries")["data"]["entries"]}
    result = []
    for lk in targets:
        c = entries.get(lk["entry_id"])
        if not c:
            print(f"   relink: {lk['domain']}: {lk['title']} no longer exists")
            continue
        # undo: ZHA device -> the Z2M device as restored by the rollback (normally its old id)
        m = {v: mapping.get(k, k) for k, v in lk["map"].items()} if undo else mapping
        opts = dict(c.get("options") or {})
        changed = {k: m[v] for k, v in opts.items() if isinstance(v, str) and v in m}
        if not changed:
            print(f"   relink: {lk['domain']}: {lk['title']} nothing to change")
            continue
        flow = None
        try:
            flow = ha.rest("POST", "/config/config_entries/options/flow",
                           {"handler": c["entry_id"], "show_advanced_options": True})
            if flow.get("type") != "form":
                raise RuntimeError(f"flow is a {flow.get('type')}, not a plain form")
            names = {f.get("name") for f in flow.get("data_schema", [])}
            missing = set(changed) - names
            if missing:
                raise RuntimeError(f"options {sorted(missing)} are not in the form")
            body = {k: v for k, v in (opts | changed).items() if k in names and v is not None}
            r = ha.rest("POST", f"/config/config_entries/options/flow/{flow['flow_id']}", body)
            if r.get("type") != "create_entry":
                raise RuntimeError(f"flow ended {r.get('type')} {r.get('errors') or r.get('reason')}")
            flow = None
            do.note("options_flow", entry_id=c["entry_id"], domain=lk["domain"], changed=changed)
            result.append({"entry_id": c["entry_id"], "domain": lk["domain"], "title": lk["title"],
                           "map": {opts[k]: v for k, v in changed.items()}})
            print(f"   relinked {lk['domain']}: {lk['title']} {sorted(changed)} -> {'Z2M' if undo else 'ZHA'} device")
        except Exception as e:  # noqa: BLE001  never abort apply or rollback on one entry
            print(f"   relink {lk['domain']}: {lk['title']} FAILED ({str(e)[:120]}), repoint it in the UI")
            if flow and flow.get("flow_id"):
                try:
                    ha.rest("DELETE", f"/config/config_entries/options/flow/{flow['flow_id']}")
                except Exception:  # noqa: BLE001
                    pass
    if not undo:
        env.save_state("relinked.json", result)
    return result


def reload_linked(ha, cap, skip_ids=()):
    """Reload loaded entries that reference Z2M entities, so they pick up the renamed entities."""
    n = 0
    for lk in cap.get("links", []):
        if lk["entry_id"] in skip_ids or lk["disabled_by"] or lk["action"] == "HANDLED":
            continue
        try:
            ha.rest("POST", f"/config/config_entries/entry/{lk['entry_id']}/reload")
            n += 1
        except Exception as e:  # noqa: BLE001
            print(f"   reload failed {lk['domain']}: {lk['title']} {str(e)[:80]}")
    time.sleep(2)
    return n


def check(ha, env, cap, zha_of):
    """After apply: device ids that point nowhere, entities left without their device."""
    devs = {d["id"]: d for d in ha("config/device_registry/list")}
    entries = {c["entry_id"]: c for c in env.storage("core.config_entries")["data"]["entries"]}
    reg = {e["entity_id"]: e for e in ha("config/entity_registry/list")}
    bad = 0
    for lk in cap.get("links", []):
        if lk["action"] == "HANDLED":
            continue
        c = entries.get(lk["entry_id"])
        if c is None:
            if lk["self_deleting"]:
                print(f"BAD  {lk['domain']}: {lk['title']} is GONE (it deleted itself). Recreate it, "
                      f"it wrapped {lk['entity_ids']}")
                bad += 1
            continue
        blob = repr((c.get("data"), c.get("options"), [s.get("data") for s in c.get("subentries") or []]))
        dangling = [d for d in HEX32.findall(blob) if d in set(lk["device_ids"]) and d not in devs]
        if dangling:
            bad += 1
            hint = ", ".join(f"{d} -> ZHA device {zha_of.get(d, '?')}" for d in dangling)
            print(f"BAD  {lk['domain']}: {lk['title']} still points at removed Z2M devices: {hint}")
        if lk["entity_uuids"]:
            bad += 1
            print(f"BAD  {lk['domain']}: {lk['title']} references Z2M entity UUIDs {lk['entity_uuids']}, "
                  "repoint it in the UI")
    for a in cap.get("attached", []):
        e = reg.get(a["entity_id"])
        if e and e.get("device_id") and e["device_id"] not in devs:
            bad += 1
            print(f"BAD  {a['entity_id']} ({a['platform']}) sits on a removed device. Reload its integration or "
                  "repoint it in the UI")
        elif e and not e.get("device_id") and a["config_entry_id"] in entries:
            print(f"INFO {a['entity_id']} ({a['platform']}) has no device any more (was on a Z2M device)")
    print(f"links: {bad} problem(s)" if bad else "links: every linked integration points at live entities/devices")
    return bad

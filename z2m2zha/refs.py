"""READ-ONLY checks before the cutover: what references Z2M entities/devices, and light vs switch."""
import json
import os
import re
import subprocess
import sys

from . import radio, util

SKIP_DIRS = {"deps", "tts", "backups", "backup", "www", "custom_components", "esphome", "node_modules", ".cloud",
             "image", "media", "share", "themes", "log", "logs", "__pycache__", ".z2m2zha"}
SKIP_FILES = re.compile(r"(\.db(-shm|-wal)?$|\.log(\.\d+|\.old|\.fault)?$|core\.(entity|device|area)_registry|"
                        r"core\.restore_state|\.bak|backup|\.pyc$|\.(png|jpe?g|gif|svg|ico|woff2?|ttf|gz|zip|tar|bin)$|"
                        r"trace\.saved_traces|repairs\.issue_registry|core\.analytics)", re.I)


def scan_refs(env):
    """Find every reference to Z2M entities and devices in the HA config dir.

    entity_id strings survive (the ids are kept). device_id references (device triggers,
    conditions and actions), entity registry UUIDs, raw zigbee2mqtt/ MQTT topics and IEEE
    strings BREAK and must be rewritten. Writes state/refs.json, which plan and verify use.
    """
    root = env.ha_config_dir
    if not os.access(root, os.R_OK | os.X_OK) or not os.access(f"{root}/.storage", os.R_OK | os.X_OK):
        if util.have_sudo() and not os.environ.get("Z2M2ZHA_SUDO"):
            env_vars = [f"{k}={v}" for k, v in os.environ.items() if k.startswith(("Z2M2ZHA", "SUPERVISOR", "HA_"))]
            sys.exit(subprocess.call(["sudo", "-n", "env", "Z2M2ZHA_SUDO=1", *env_vars, sys.executable, "-m",
                                      "z2m2zha", *sys.argv[1:]], cwd=util.REPO_ROOT))
        raise SystemExit(f"cannot read {root}: run as a user that can")
    sfx = env.cfg["z2m"]["unique_id_suffix"]
    er = env.storage("core.entity_registry")["data"]["entities"]
    dr = env.storage("core.device_registry")["data"]["devices"]
    z2m = [e for e in er if e["platform"] == "mqtt" and e["unique_id"].endswith(sfx)]
    eids = {e["entity_id"] for e in z2m}
    uuids = {e["id"]: e["entity_id"] for e in z2m}
    z2m_dev_ids = {e["device_id"] for e in z2m if e.get("device_id")}
    dev = {d["id"]: (d.get("name_by_user") or d["name"]) for d in dr if d["id"] in z2m_dev_ids}
    ieees = {m.group(1) for e in z2m if (m := re.match(r"(?:bridge_)?(0x[0-9a-f]{16})", e["unique_id"]))}
    if not eids:
        raise SystemExit("no Zigbee2MQTT entities found in the entity registry")
    eid_rx = re.compile(r"(?<![a-z0-9_.])(" + "|".join(map(re.escape, sorted(eids, key=len, reverse=True)))
                        + r")(?![a-z0-9_])")
    hex_rx = re.compile(r"\b[0-9a-f]{32}\b")
    ind_rx = re.compile(r"integration_entities\(\s*['\"]mqtt|device_entities\(|device_attr\(|device_id\(|"
                        r"zigbee2mqtt/|homeassistant/[a-z_]+/0x[0-9a-f]{16}", re.I)
    z2m_dir = os.path.realpath(env.z2m.data_dir)
    report, referenced = {}, set()
    for dp, ds, fs in os.walk(root):
        ds[:] = [d for d in ds if d not in SKIP_DIRS and not d.startswith(".git")
                 and os.path.realpath(os.path.join(dp, d)) != z2m_dir]
        for f in fs:
            p = os.path.join(dp, f)
            rel = p[len(root) + 1:]
            if SKIP_FILES.search(rel) or os.path.getsize(p) > 20_000_000:
                continue
            try:
                t = open(p, encoding="utf-8", errors="strict").read()
            except (UnicodeDecodeError, OSError):
                continue
            found = set(eid_rx.findall(t))
            referenced |= found
            hexes = set(hex_rx.findall(t))
            hits = {"entity_ids": sorted(found),
                    "device_ids_BREAK": sorted(f"{h} ({dev[h]})" for h in hexes if h in dev),
                    "entity_uuids_BREAK": sorted(f"{h} ({uuids[h]})" for h in hexes if h in uuids),
                    "ieee_BREAK": sorted(i for i in ieees if i in t),
                    "indirect_CHECK": sorted({m.group(0) for m in ind_rx.finditer(t)})}
            hits = {k: v for k, v in hits.items() if v}
            if hits:
                report[rel] = hits
    out = {"z2m_entities": len(eids), "z2m_devices": len(dev), "entity_ids": sorted(referenced), "files": report}
    env.save_state("refs.json", out, 0o644)
    if os.environ.get("SUDO_UID"):
        os.chown(env.state("refs.json"), int(os.environ["SUDO_UID"]), int(os.environ.get("SUDO_GID", "0")))
    breaking = {f: {k: v for k, v in h.items() if k.endswith(("_BREAK", "_CHECK"))} for f, h in report.items()}
    breaking = {f: h for f, h in breaking.items() if h}
    print(f"{len(eids)} Z2M entities on {len(dev)} devices, {len(referenced)} of them referenced in "
          f"{len(report)} files (those survive, the ids are kept)")
    print("Things that will BREAK or need a look (device ids, registry UUIDs, raw MQTT topics, IEEEs):"
          if breaking else "Nothing references Z2M device ids, registry UUIDs, raw topics or IEEEs.")
    for f, h in sorted(breaking.items()):
        print(" ", f, json.dumps(h))
    print(f"full report: {env.state('refs.json')}")


def suggest_device_config(env):
    """Print the zha: device_config YAML that keeps Z2M switches as switches in ZHA."""
    ha = env.ha()
    sfx = env.cfg["z2m"]["unique_id_suffix"]
    ieees = set()
    for e in ha("config/entity_registry/list"):
        if e["platform"] == "mqtt" and e["entity_id"].startswith("switch.") and e["unique_id"].endswith(sfx):
            m = re.match(r"(0x[0-9a-f]{16})_(switch|state)(_l\d+)?" + re.escape(sfx) + "$", e["unique_id"])
            if m:
                ieees.add(m.group(1))
    st = env.staging
    util.write_bytes(f"{st}/z2m_database.db", util.read_bytes(env.z2m.file("database.db")), 0o600)
    try:
        print(env.zigpy_script("suggest_device_config.py", env.in_ha(f"{st}/z2m_database.db"),
                               input=json.dumps(sorted(ieees))), end="")
    finally:
        radio.cleanup_staging(env)
    print("# Merge this into configuration.yaml (one zha: key only) and restart HA BEFORE the cutover.")

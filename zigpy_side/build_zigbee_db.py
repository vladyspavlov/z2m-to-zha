"""Build a preseeded zigpy database (the zigbee.db ZHA opens) from Zigbee2MQTT's database.db.

    python3 build_zigbee_db.py <z2m database.db> <out zigbee.db> [<network backup .json>]

Runs with HA's Python. The schema is created by the installed zigpy itself, so the file always
matches the zigpy version ZHA will open it with (no migration on first start).

Why: on the same coordinator the network (PAN, extended PAN, channel, keys, frame counter) lives
in the radio, so no device leaves the network. What ZHA lacks is its own device table. Without
it, zigpy only learns a device when it next talks and then interviews it, which fails often for
sleepy battery devices. The preseed gives ZHA every device, endpoint, cluster and the Basic
manufacturer/model (what quirks match on) up front.

The optional network backup (Open Coordinator Backup JSON, e.g. Z2M's coordinator_backup.json)
is stored in zigpy's network_backups table. ZHA starts zigpy with auto_form=True. If the radio
were ever read as "not formed" and the DB had NO backup, zigpy would form a NEW random network
(every device re-paired). With the backup it restores this network instead, and if the radio
differs from the backup ZHA refuses to start rather than changing anything.
"""
import asyncio
import json
import os
import sqlite3
import sys
import time

import zigpy.appdb
import zigpy.application
import zigpy.backups

src, out = sys.argv[1], sys.argv[2]
backup_path = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else None
V = zigpy.appdb.DB_VERSION

LOGICAL = {"Coordinator": 0, "Router": 1, "EndDevice": 2}
MAC_ROUTER, MAC_SLEEPY = 0x8E, 0x80  # alloc address | rx on idle | mains | FFD, or alloc address only
POWER_SOURCE = {"Mains (single phase)": 1, "Mains (3 phase)": 2, "Battery": 3, "DC Source": 4,
                "Emergency mains constantly powered": 5, "Emergency mains and transfer switch": 6}
DEV_STATUS_ENDPOINTS_INIT = 2  # zigpy.device.Status.ENDPOINTS_INIT (fully initialized)
EP_STATUS_ZDO_INIT = 1         # zigpy.endpoint.Status.ZDO_INIT
# Scaling attributes zigbee-herdsman caches by name. ZHA needs them to show correct units/values.
SCALING = {
    "seMetering": (0x0702, {"unitOfMeasure": 0x0300, "multiplier": 0x0301, "divisor": 0x0302,
                            "summaFormatting": 0x0303, "summationFormatting": 0x0303, "meteringDeviceType": 0x0306}),
    "haElectricalMeasurement": (0x0B04, {"measurementType": 0x0000, "acVoltageMultiplier": 0x0600,
                                         "acVoltageDivisor": 0x0601, "acCurrentMultiplier": 0x0602,
                                         "acCurrentDivisor": 0x0603, "acPowerMultiplier": 0x0604,
                                         "acPowerDivisor": 0x0605}),
}


class SchemaOnlyApp(zigpy.application.ControllerApplication):
    """A radio-less app: only used to let zigpy create its own empty database."""

    async def connect(self): ...
    async def disconnect(self): ...
    async def start_network(self): ...
    async def force_remove(self, dev): ...
    async def add_endpoint(self, descriptor): ...
    async def permit_ncp(self, time_s=60): ...
    async def permit_with_link_key(self, node, link_key, time_s=60): ...
    async def write_network_info(self, *, network_info, node_info): ...
    async def load_network_info(self, *, load_devices=False): ...
    async def reset_network_info(self): ...
    async def send_packet(self, packet): ...


async def create_schema(path):
    app = SchemaOnlyApp({"database_path": path, "device": {"path": "/dev/null"}})
    await app._load_db()
    await app._dblistener.shutdown()


def ieee(z2m):
    h = z2m[2:].lower().rjust(16, "0")
    return ":".join(h[i:i + 2] for i in range(0, 16, 2))


def interviewed(r):
    return r.get("interviewCompleted") is True or r.get("interviewState") == "SUCCESSFUL"


if os.path.exists(out):
    sys.exit(f"refusing: {out} exists")
asyncio.run(create_schema(out))
recs = [json.loads(ln) for ln in open(src) if ln.strip()]
db = sqlite3.connect(out)
now = time.time()
n = skipped = scaling = 0
for r in recs:
    if r.get("type") in ("Coordinator", "Group"):
        continue  # zigpy writes the coordinator itself from the radio, groups are not devices
    if r.get("type") not in LOGICAL or not interviewed(r) or not r.get("endpoints"):
        print(f"SKIP {r.get('ieeeAddr')} ({r.get('modelId')}): type {r.get('type')}, interview incomplete "
              "or no endpoints in Z2M. Fix it in Z2M first or re-pair it later.")
        skipped += 1
        continue
    i = ieee(r["ieeeAddr"])
    last = (r.get("lastSeen") or now * 1000) / 1000
    db.execute(f"INSERT INTO devices_v{V} (ieee, nwk, status, last_seen) VALUES (?,?,?,?)",
               (i, r["nwkAddr"], DEV_STATUS_ENDPOINTS_INIT, last))
    mains = str(r.get("powerSource", "")).startswith("Mains") or r.get("type") == "Router"
    db.execute(
        f"INSERT INTO node_descriptors_v{V} (ieee, logical_type, complex_descriptor_available,"
        " user_descriptor_available, reserved, aps_flags, frequency_band, mac_capability_flags, manufacturer_code,"
        " maximum_buffer_size, maximum_incoming_transfer_size, server_mask, maximum_outgoing_transfer_size,"
        " descriptor_capability_field) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (i, LOGICAL[r["type"]], 0, 0, 0, 0, 0x08, MAC_ROUTER if mains else MAC_SLEEPY,
         r.get("manufId") or 0, 82, 82, 0x2C00, 82, 0),
    )

    def cache(ep, cluster, attr, value):
        db.execute(
            f"INSERT OR REPLACE INTO attributes_cache_v{V} (ieee, endpoint_id, cluster_type, cluster_id, attr_id,"
            " manufacturer_code, status, value, last_updated) VALUES (?,?,?,?,?,?,?,?,?)",
            (i, ep, 0, cluster, attr, None, 0, value, now))

    basic_ep = None
    for epid, ep in sorted(r["endpoints"].items(), key=lambda x: int(x[0])):
        epid = int(epid)
        db.execute(f"INSERT INTO endpoints_v{V} (ieee, endpoint_id, profile_id, device_type, status)"
                   " VALUES (?,?,?,?,?)", (i, epid, ep.get("profId", 260), ep.get("devId", 0), EP_STATUS_ZDO_INIT))
        for ctype, key in ((0, "inClusterList"), (1, "outClusterList")):
            for cid in dict.fromkeys(ep.get(key, [])):
                db.execute(f"INSERT INTO clusters_v{V} (ieee, endpoint_id, cluster_type, cluster_id)"
                           " VALUES (?,?,?,?)", (i, epid, ctype, cid))
        if basic_ep is None and 0 in ep.get("inClusterList", []):
            basic_ep = epid
        for cname, (cid, attrs) in SCALING.items():
            cached = ((ep.get("clusters") or {}).get(cname) or {}).get("attributes") or {}
            for aname, aid in attrs.items():
                if isinstance(cached.get(aname), int) and cid in ep.get("inClusterList", []):
                    cache(epid, cid, aid, cached[aname])
                    scaling += 1
    basic = {0x0004: r.get("manufName"), 0x0005: r.get("modelId"), 0x0000: r.get("zclVersion"),
             0x0001: r.get("appVersion"), 0x0002: r.get("stackVersion"), 0x0003: r.get("hwVersion"),
             0x0006: r.get("dateCode"), 0x0007: POWER_SOURCE.get(r.get("powerSource")), 0x4000: r.get("swBuildId")}
    for aid, val in basic.items():
        if basic_ep is not None and val is not None:
            cache(basic_ep, 0x0000, aid, val)
    n += 1

if backup_path:
    nb = zigpy.backups.NetworkBackup.from_open_coordinator_json(json.load(open(backup_path)))
    db.execute(f"INSERT INTO network_backups_v{V} (backup_json) VALUES (?)", (json.dumps(nb.as_dict()),))
    ni = nb.network_info
    print(f"seeded network backup: channel {ni.channel}, pan 0x{ni.pan_id:04x}, update id {ni.nwk_update_id}, "
          f"frame counter {ni.network_key.tx_counter}, {len(ni.children)} children, {len(ni.key_table)} link keys")
db.commit()
db.close()
print(f"zigpy schema v{V}: wrote {n} devices ({skipped} skipped, {scaling} scaling attributes copied) to {out}")

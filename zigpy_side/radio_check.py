"""Read the network settings from the coordinator with zigpy (read-only) and compare with a backup.

    python3 radio_check.py '<json args>'
    args: {"type": "znp"|"ezsp"|...|null, "port": ..., "baudrate": ..., "flow_control": ...,
           "compare": "<backup json>"|null, "save": "<path for the radio's own backup>"|null}

connect + load_network_info is exactly what ZHA's config flow and startup do to read the radio.
Nothing is written to the radio. With "save", the radio's current settings are written as an
Open Coordinator Backup (mode 0600, it contains the network key). They can seed the ZHA DB when
Zigbee2MQTT has no coordinator_backup.json. The key itself is never printed.
"""
import asyncio
import json
import os
import sys

import zigpy.backups
from zha.application.const import RadioType

a = json.loads(sys.argv[1])
dev = {"path": a["port"], "baudrate": a.get("baudrate") or 115200, "flow_control": a.get("flow_control")}


async def detect():
    for rt in RadioType:
        try:
            if await rt.controller.probe(dict(dev)):
                return rt
        except Exception:  # noqa: BLE001
            pass
    return None


def describe(label, b):
    ni = b.network_info
    print(f"{label:6}: coordinator {b.node_info.ieee}, channel {ni.channel}, pan 0x{ni.pan_id:04x}, "
          f"extended pan {ni.extended_pan_id}, update id {ni.nwk_update_id}, frame counter "
          f"{ni.network_key.tx_counter}, children {len(ni.children)}, link keys {len(ni.key_table)}")


async def read(rt):
    app = rt.controller({"device": dev, "database_path": None, "topo_scan_enabled": False, "ota": {"enabled": False}})
    try:
        await app.connect()
        await app.load_network_info(load_devices=True)
        return app.backups.from_network_state()
    finally:
        await app.disconnect()


async def main():
    rt = RadioType[a["type"]] if a.get("type") else await detect()
    if rt is None:
        print("RESULT: FAIL - no zigpy radio library answered on this port")
        sys.exit(1)
    print("radio type:", rt.name, "-", rt.description)
    radio = None
    for attempt in (1, 2, 3):
        try:
            radio = await read(rt)
            break
        except Exception as e:  # noqa: BLE001  the first open right after another program often times out
            print(f"read attempt {attempt} failed: {type(e).__name__}: {str(e)[:120]}")
            await asyncio.sleep(15)
    if radio is None:
        print("RESULT: FAIL - could not read the radio (not a network problem, see the errors above)")
        sys.exit(1)
    if not radio.network_info.extended_pan_id or radio.network_info.pan_id in (0, 0xFFFF):
        print("RESULT: FAIL - the radio reports NO formed network. Do not migrate.")
        sys.exit(1)
    describe("radio", radio)
    if a.get("save"):
        fd = os.open(a["save"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(radio.as_open_coordinator_json(), f)
        print("saved the radio's own backup to", a["save"], "(0600, contains the network key)")
    if a.get("compare") and os.path.exists(a["compare"]):
        ref = zigpy.backups.NetworkBackup.from_open_coordinator_json(json.load(open(a["compare"])))
        describe("backup", ref)
        same_key = radio.network_info.network_key.key == ref.network_info.network_key.key
        print("network key identical:", same_key)
        ok = radio.is_compatible_with(ref) and same_key
        print("RESULT:", "PASS - ZHA will see the same network (no re-pairing)" if ok
              else "FAIL - the radio differs from the backup. DO NOT MIGRATE, investigate")
        sys.exit(0 if ok else 1)
    print("RESULT: PASS - radio read OK (no backup to compare against)")


asyncio.run(main())

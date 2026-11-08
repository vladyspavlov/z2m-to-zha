"""Load a zigbee.db through the real zigpy loader and ZHA's quirk resolver (HA's own versions).

    python3 validate_zigbee_db.py <zigbee.db> [<custom quirks dir>] [<names.json>]

Prints per device: loaded OK, manufacturer/model, endpoints and the quirk ZHA would apply. The
last line is "SUMMARY {json}" for scripts. The network key is never printed.
"""
import asyncio
import json
import sys

import zigpy.application
import zigpy.device
import zhaquirks
from zha.quirks import DEVICE_REGISTRY, QUIRK_REGISTRY_ENTRY_ATTR

path = sys.argv[1]
custom = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
names = json.load(open(sys.argv[3])) if len(sys.argv) > 3 and sys.argv[3] else {}


class App(zigpy.application.ControllerApplication):
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


def quirk_name(dev):
    entry = getattr(dev, QUIRK_REGISTRY_ENTRY_ATTR, None)
    if entry is None:
        return "no quirk" if type(dev) is zigpy.device.Device else type(dev).__name__
    src = getattr(entry, "quirk_file", None) or getattr(entry, "source", None)
    if hasattr(src, "file"):  # zigpy QuirkSource(module, file, line, label)
        return f"{str(src.file).rsplit('/', 1)[-1]} ({getattr(src, 'label', '') or ''})"
    return str(src).split("site-packages/")[-1] if src else "quirk v2"


async def main():
    zhaquirks.setup(custom)
    app = App({"database_path": path, "device": {"path": "/dev/null"}})
    await app._load_db()
    bad = 0
    for dev in sorted(app.devices.values(), key=lambda d: str(d.ieee)):
        q = DEVICE_REGISTRY.resolve(dev)  # what ZHA's gateway uses as device resolver
        name = names.get("0x" + str(dev.ieee).replace(":", ""), "?")
        good = dev.status == zigpy.device.Status.ENDPOINTS_INIT and dev.manufacturer and dev.model
        bad += not good
        print(f"{'OK ' if good else 'BAD'} {dev.ieee} | {name[:26]:26} | {str(dev.manufacturer)[:18]:18} | "
              f"{str(dev.model)[:20]:20} | eps={sorted(e for e in dev.endpoints if e)} | {quirk_name(q)}")
    nb = app.backups.most_recent_backup()
    summary = {"devices": len(app.devices), "problems": bad, "backup": nb is not None}
    print(f"\n{len(app.devices)} devices loaded, {bad} problems")
    if nb is None:
        print("NETWORK BACKUP: NONE. ZHA could form a NEW network if it reads the radio as not formed.")
    else:
        ni = nb.network_info
        kids = {str(c) for c in ni.children}
        in_db = {str(d.ieee) for d in app.devices.values()}
        print(f"NETWORK BACKUP: present. channel {ni.channel}, pan 0x{ni.pan_id:04x}, extended pan "
              f"{ni.extended_pan_id}, update id {ni.nwk_update_id}, frame counter {ni.network_key.tx_counter}")
        print(f"  direct children known to the radio: {len(kids)}, of them in the DB: {len(kids & in_db)}")
        summary.update(channel=ni.channel, pan_id=ni.pan_id)
    await app._dblistener.shutdown()
    print("SUMMARY " + json.dumps(summary))


asyncio.run(main())

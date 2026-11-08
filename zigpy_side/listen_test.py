"""Run the real network on ZHA's radio stack for a short time and show which devices talk to it.

    python3 listen_test.py '<json args>'
    args: {"type", "port", "baudrate", "flow_control", "db": "<throwaway preseeded db>",
           "seconds": 120, "names": "<names.json>"|null, "allow_write": false}

No ZHA, no Home Assistant entities, no interviews (devices come from a throwaway preseeded DB).
With Z-Stack (znp), start_network(read_only=True) resumes the network from the radio's NVRAM
without writing settings. Other radio libraries have no read-only start, so they need
"allow_write": true (their normal start can write things like TX power or endpoints).

Passive: frames per device. Active: one Basic cluster read (manufacturer name) to every router,
which proves two-way traffic. Nothing else is sent.
"""
import asyncio
import collections
import inspect
import json
import sys
import time

from zha.application.const import RadioType

a = json.loads(sys.argv[1])
names = json.load(open(a["names"])) if a.get("names") else {}
dev_cfg = {"path": a["port"], "baudrate": a.get("baudrate") or 115200, "flow_control": a.get("flow_control")}


def nm(dev):
    return names.get("0x" + str(dev.ieee).replace(":", ""), str(dev.ieee))


async def main():
    rt = RadioType[a["type"]]
    app = rt.controller({"device": dev_cfg, "database_path": a["db"], "topo_scan_enabled": False,
                         "ota": {"enabled": False}})
    ro = "read_only" in inspect.signature(app.start_network).parameters
    if not ro and not a.get("allow_write"):
        print(f"RESULT: REFUSED - {rt.name} has no read-only network start. Re-run with --allow-write if you accept it.")
        sys.exit(2)
    await app._load_db()
    heard = collections.defaultdict(lambda: {"n": 0, "lqi": None, "rssi": None})
    unknown = collections.Counter()
    orig = app.packet_received

    def tap(packet):
        try:
            h = heard[app.get_device_with_address(packet.src).ieee]
            h["n"] += 1
            h["lqi"], h["rssi"] = packet.lqi, packet.rssi
        except (KeyError, ValueError):
            unknown[str(packet.src)] += 1
        return orig(packet)

    app.packet_received = tap
    for attempt in (1, 2, 3):
        try:
            await app.connect()
            break
        except Exception as e:  # noqa: BLE001  the first open right after Z2M often times out once
            print(f"connect attempt {attempt} failed: {type(e).__name__}, retrying in 10 s")
            await app.disconnect()
            await asyncio.sleep(10)
    else:
        print("RESULT: FAIL - could not connect to the radio")
        sys.exit(1)
    try:
        await app.load_network_info(load_devices=False)
        await (app.start_network(read_only=True) if ro else app.start_network())
        t0 = time.monotonic()
        ni = app.state.network_info
        print(f"network running on the zigpy stack: channel {ni.channel}, pan 0x{ni.pan_id:04x}"
              f"{' (read-only start)' if ro else ''}")
        others = [d for d in app.devices.values() if d.nwk != 0x0000]
        routers = [d for d in others if d.node_desc and d.node_desc.is_router]
        sleepy = [d for d in others if d not in routers]

        async def read_basic(d):
            ep = next((e for i, e in d.endpoints.items() if i and 0x0000 in e.in_clusters), None)
            if ep is None:
                return d, "no Basic cluster"
            try:
                async with asyncio.timeout(10):
                    res = await ep.in_clusters[0x0000].read_attributes([0x0004], allow_cache=False)
                return d, "reply" if res and 0x0004 in res[0] else "reply (unsupported attribute)"
            except Exception as e:  # noqa: BLE001
                return d, f"no reply ({type(e).__name__})"

        await asyncio.sleep(5)
        sem = asyncio.Semaphore(4)

        async def guarded(d):
            async with sem:
                return await read_basic(d)

        replies = dict(await asyncio.gather(*(guarded(d) for d in routers)))
        rest = a.get("seconds", 120) - (time.monotonic() - t0)
        if rest > 0:
            await asyncio.sleep(rest)
    finally:
        await app.disconnect()

    print(f"\nlistened {time.monotonic() - t0:.0f} s")
    ok_r = 0
    print("ROUTERS (active Basic read + passive frames):")
    for d in sorted(routers, key=nm):
        h, r = heard.get(d.ieee), replies.get(d, "-")
        good = r.startswith("reply") or bool(h and h["n"])
        ok_r += good
        print(f"  {'OK ' if good else 'NO '} {nm(d)[:28]:28} read: {r:24} frames {h['n'] if h else 0:3}"
              f"  lqi {h['lqi'] if h else '-'}  rssi {h['rssi'] if h else '-'}")
    ok_s = 0
    print("SLEEPY END DEVICES (passive only, silence is normal):")
    for d in sorted(sleepy, key=nm):
        h = heard.get(d.ieee)
        ok_s += bool(h)
        print(f"  {'HEARD ' if h else 'asleep'} {nm(d)[:28]:28} frames {h['n'] if h else 0}")
    if unknown:
        print("frames from addresses not in the DB:", dict(unknown))
    print(f"\nRESULT: routers talking {ok_r}/{len(routers)}, sleepy devices heard {ok_s}/{len(sleepy)}")


asyncio.run(main())

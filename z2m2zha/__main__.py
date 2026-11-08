"""z2m2zha: move a Zigbee2MQTT network to ZHA on the same coordinator, keeping entity ids and history.

Run from the repository root:  python3 -m z2m2zha <command> [options]
"""
import argparse
import os
import sys

from . import cutover, radio, refs, registry, util
from .env import Env

HELP = {
    "detect": "show what was auto-detected (read-only)",
    "scan-refs": "find references to Z2M entities/devices in the HA config (read-only)",
    "suggest-device-config": "print the zha: device_config that keeps Z2M switches as switches (read-only)",
    "build-test": "build + validate a throwaway preseeded DB while Z2M runs (read-only for Z2M/ZHA)",
    "preflight": "stop Z2M ~1 min, read the radio with zigpy, compare with Z2M's backup, start Z2M",
    "listen-test": "stop Z2M a few minutes, run the network on zigpy, list who talks, start Z2M",
    "capture": "snapshot all Z2M entities/devices/helpers (read-only, Z2M must run)",
    "cutover": "the migration itself (detached): stop Z2M, preseed, ZHA flow, plan, apply, verify",
    "plan": "map Z2M entities to ZHA entities (read-only)",
    "apply": "rename ZHA entities onto the old ids, move helpers and Battery Notes (--yes)",
    "polish": "make friendly names, icons and device classes match the capture",
    "nudge": "write captured values back to config entities that stay unknown (--yes)",
    "verify": "check that every important entity exists and has a state, estimate the energy gap",
    "battery-notes": "re-run only the Battery Notes move (--yes)",
    "rollback": "back to Zigbee2MQTT, same entity ids and device ids (--yes)",
}


def detect(env):
    print("config file          :", env.config_path or "(none, all auto-detected)")
    print("state dir            :", env.state_dir)
    print("HA config dir        :", env.ha_config_dir, "| seen by HA as", env.ha_config_dir_in_ha)
    print("HA Python (zigpy)    :", " ".join(env.ha_python))
    print("docker               :", " ".join(env.docker) if env.docker else "not available")
    ha = env.ha()
    conf = ha.rest("GET", "/config")
    print("Home Assistant       :", conf.get("version"), "| ZHA entries:", len(ha("config_entries/get", domain="zha")))
    z = env.z2m
    print("Zigbee2MQTT          :", f"add-on {z.slug} ({'running' if z.is_running() else 'stopped'})" if z.slug
          else f"commands: {env.cfg['z2m']['stop_cmd']} / {env.cfg['z2m']['start_cmd']}")
    print("Z2M data dir         :", z.data_dir)
    print("Z2M devices          :", sum(1 for r in z.devices() if r.get("type") != "Coordinator"))
    print("Z2M network backup   :", "present" if util.exists(z.file("coordinator_backup.json")) else "MISSING")
    r = env.radio
    print("radio                :", r["port"], "| type", r["type"] or "auto", "| baud", r["baudrate"],
          "| flow control", r["flow_control"] or "none")
    print("ZHA database         :", env.zha_database, "(exists)" if util.exists(env.zha_database) else "")
    print("custom quirks (HA)   :", env.custom_quirks_in_ha or "(none)")
    print("port held by         :", ", ".join(env.port_holders()) or "nobody")
    vers = env.zigpy_script("versions.py")
    print("HA Python packages   :", vers.strip())


def main():
    ap = argparse.ArgumentParser(prog="python3 -m z2m2zha", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="config JSON (default: ./config.json or $Z2M2ZHA_CONFIG)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, h in HELP.items():
        p = sub.add_parser(name, help=h, description=h)
        p.add_argument("--yes", action="store_true", help="confirm a writing step")
        p.add_argument("--foreground", action="store_true", help="do not detach")
        if name == "listen-test":
            p.add_argument("--seconds", type=int, default=120)
            p.add_argument("--allow-write", action="store_true",
                           help="allow a non read-only network start (radios other than Z-Stack)")
        if name == "cutover":
            p.add_argument("--reuse-capture", action="store_true", help="use a capture.json younger than 1 h")
    a = ap.parse_args()
    if a.config:  # the detached child runs from the repo root, so make the path absolute
        os.environ["Z2M2ZHA_CONFIG"] = os.path.abspath(a.config)
        sys.argv = [os.path.abspath(x) if x == a.config else x for x in sys.argv]
    env = Env(a.config and os.path.abspath(a.config))
    {
        "detect": detect,
        "scan-refs": refs.scan_refs,
        "suggest-device-config": refs.suggest_device_config,
        "build-test": radio.build_test,
        "preflight": radio.preflight,
        "listen-test": lambda e: radio.listen_test(e, a.seconds, a.allow_write),
        "capture": registry.capture,
        "cutover": lambda e: cutover.cutover(e, a.reuse_capture),
        "plan": registry.plan,
        "apply": registry.apply,
        "polish": registry.polish,
        "nudge": registry.nudge,
        "verify": registry.verify,
        "battery-notes": registry.battery_notes,
        "rollback": registry.rollback,
    }[a.cmd](env)


if __name__ == "__main__":
    sys.exit(main())

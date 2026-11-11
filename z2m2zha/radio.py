"""Radio side: stage Z2M files for HA's Python, build/validate the preseeded DB, preflight, listen test."""
import json
import re
import time

from . import util


def stage_inputs(env, seed=True):
    """Copy Z2M's database.db, friendly names and (optionally) a network backup into the staging
    dir inside the HA config dir, where HA's Python can read them. Returns HA-side paths."""
    st = env.staging
    util.write_bytes(f"{st}/z2m_database.db", util.read_bytes(env.z2m.file("database.db")), 0o600)
    util.write_bytes(f"{st}/names.json", json.dumps(env.z2m.friendly_names()), 0o600)
    out = {"z2m_db": env.in_ha(f"{st}/z2m_database.db"), "names": env.in_ha(f"{st}/names.json"), "seed": ""}
    if seed:
        z2m_backup = env.z2m.file("coordinator_backup.json")
        if util.exists(z2m_backup):
            util.write_bytes(f"{st}/network_backup.json", util.read_bytes(z2m_backup), 0o600)
            out["seed"] = env.in_ha(f"{st}/network_backup.json")
            out["seed_source"] = "Zigbee2MQTT coordinator_backup.json"
        elif util.exists(f"{st}/radio_backup.json"):
            out["seed"] = env.in_ha(f"{st}/radio_backup.json")
            out["seed_source"] = "radio backup saved by preflight"
    return out


def cleanup_staging(env):
    """Remove staged copies (some contain the network key). radio_backup.json from preflight stays
    (it is the seed fallback), so the folder is removed only when nothing else is left."""
    for base in ("z2m_database.db", "network_backup.json", "compare_backup.json", "names.json", "listen.db",
                 "zigbee.db.new", "zigbee.db.test"):
        for suffix in ("", "-wal", "-shm"):
            try:
                util.fs("rm", "-f", f"{env.staging}/{base}{suffix}")
            except RuntimeError:
                pass
    try:
        util.fs("rmdir", env.staging)  # fails (fine) while radio_backup.json is kept
    except RuntimeError:
        pass


def build(env, out_local, seed=True):
    """Build a preseeded DB at out_local (under the HA config dir) and validate it.

    Returns the validator summary: {"devices", "problems", "backup", "built", ...}.
    """
    p = stage_inputs(env, seed)
    if seed and not p["seed"]:
        raise RuntimeError("no network backup to seed the DB with: Z2M has no coordinator_backup.json. "
                           "Run preflight first (it saves the radio's own backup)")
    for suffix in ("", "-wal", "-shm"):
        util.fs("rm", "-f", out_local + suffix)
    out = env.zigpy_script("build_zigbee_db.py", p["z2m_db"], env.in_ha(out_local), p["seed"])
    print(util.mask(out), end="")
    m = re.search(r"wrote (\d+) devices", out)
    built = int(m.group(1)) if m else -1
    m = re.search(r"\((\d+) skipped", out)
    skipped = int(m.group(1)) if m else 0
    val = env.zigpy_script("validate_zigbee_db.py", env.in_ha(out_local), env.custom_quirks_in_ha, p["names"])
    print(util.mask(val), end="")
    line = next(ln for ln in val.splitlines() if ln.startswith("SUMMARY "))
    s = json.loads(line[len("SUMMARY "):])
    s["built"] = built
    s["skipped"] = skipped
    s["seed_source"] = p.get("seed_source")
    return s


def not_ready(s):
    """Why a build summary is not good enough to hand to ZHA (text), or None when it is."""
    if s["problems"] or s["devices"] != s["built"] or not s["backup"]:
        return (f"{s['built']} built, {s['devices']} loaded, {s['problems']} problems, "
                f"network backup {'present' if s['backup'] else 'MISSING'}")
    if s.get("skipped"):
        return (f"{s['skipped']} Z2M devices were skipped (interview incomplete, see the SKIP lines). ZHA would "
                "not know them and plan would block. Finish their interview in Z2M or remove them from Z2M")
    return None


def build_test(env):
    """READ-ONLY for Z2M and ZHA: build + validate a throwaway DB to see what ZHA would get."""
    out = f"{env.staging}/zigbee.db.test"
    try:
        s = build(env, out, seed=True)
    finally:
        cleanup_staging(env)
    problem = not_ready(s)
    print(f"\nRESULT: {'NOT READY' if problem else 'OK'} - {s['built']} devices built, {s['devices']} loaded, "
          f"{s['problems']} problems, {s['skipped']} skipped, "
          f"network backup {'present' if s['backup'] else 'MISSING'}"
          + (f" ({s['seed_source']})" if s.get("seed_source") else "") + (f". {problem}" if problem else ""))
    return 1 if problem else 0


def _radio_args(env, **extra):
    r = env.radio
    return json.dumps({"type": r["type"], "port": r["port"], "baudrate": r["baudrate"],
                       "flow_control": r["flow_control"], **extra})


def _with_z2m_stopped(env, settle, fn):
    """Stop Z2M, wait for the serial port, run fn, and ALWAYS start Z2M again (restoring boot)."""
    util.log("stopping Zigbee2MQTT")
    prev_boot = env.z2m.stop()
    try:
        if not env.wait_port_free(60):
            raise RuntimeError(f"serial port still held by {env.port_holders()} after 60 s")
        util.log(f"port free, settling {settle} s")
        time.sleep(settle)
        return fn()
    finally:
        util.log("starting Zigbee2MQTT again")
        env.z2m.start(boot=prev_boot)


def preflight(env):
    """About 1 minute of Zigbee downtime, no writes to the radio: read the radio with zigpy and
    compare it with Z2M's backup. Also saves the radio's own backup (seed fallback)."""
    util.detach("preflight", env.state_dir)
    st = env.staging
    cmp_local = f"{st}/compare_backup.json"
    z2m_backup = env.z2m.file("coordinator_backup.json")
    has_cmp = util.exists(z2m_backup)
    if has_cmp:
        util.write_bytes(cmp_local, util.read_bytes(z2m_backup), 0o600)
    else:
        print("Zigbee2MQTT has no coordinator_backup.json: only the radio itself is read")
    args = _radio_args(env, compare=env.in_ha(cmp_local) if has_cmp else None,
                       save=env.in_ha(f"{st}/radio_backup.json"))
    try:
        out = _with_z2m_stopped(env, 10, lambda: env.zigpy_script("radio_check.py", args, stream=True, check=False))
    finally:
        util.fs("rm", "-f", cmp_local)
    res = next((ln for ln in out.splitlines() if ln.startswith("RESULT")), "RESULT: FAIL - no result")
    print(res, flush=True)
    return 0 if res.startswith("RESULT: PASS") else 1


def listen_test(env, seconds=120, allow_write=False):
    """A few minutes of Zigbee downtime: run the network on zigpy and list who talks."""
    util.detach("listen-test", env.state_dir)
    if not env.radio["type"]:
        raise SystemExit("set radio.type in the config (znp, ezsp, deconz, zigate, xbee)")
    p = stage_inputs(env, seed=False)
    db = f"{env.staging}/listen.db"
    for suffix in ("", "-wal", "-shm"):
        util.fs("rm", "-f", db + suffix)
    env.zigpy_script("build_zigbee_db.py", p["z2m_db"], env.in_ha(db), "")
    args = _radio_args(env, db=env.in_ha(db), seconds=seconds, names=p["names"], allow_write=allow_write)
    try:
        out = _with_z2m_stopped(env, 10, lambda: env.zigpy_script("listen_test.py", args, stream=True, check=False,
                                                                  timeout=seconds + 300))
    finally:
        cleanup_staging(env)
    res = next((ln for ln in out.splitlines() if ln.startswith("RESULT")), "RESULT: FAIL - no result")
    print(res, flush=True)
    return 1 if res.startswith(("RESULT: FAIL", "RESULT: REFUSED")) else 0

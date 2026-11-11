"""The cutover: Zigbee2MQTT -> ZHA on the same radio, with the shortest possible Zigbee outage.

  1 capture (Z2M still up)                2 stop Z2M (boot=manual), wait until the port is free
  3 build + validate the preseeded zigbee.db from the just-saved Z2M files, put it in place
  4 ZHA config flow: port -> ADVANCED -> reuse_settings ONLY. Anything else aborts.
  5 wait until ZHA is loaded with every device              == Zigbee outage ends here ==
  6 plan loop (asks counters for a value) -> apply + verify only with 0 blocking rows

Any failure BEFORE the ZHA entry exists: abort the flow and start Z2M again (old state back).
The preseeded DB is test-built before Z2M is stopped, so a DB problem costs no outage.
"""
import os
import sys
import time
import traceback

from . import radio, registry, util


def _pick(options, want):
    """Options of a select are plain values or [value, label] pairs."""
    vals = [o[0] if isinstance(o, (list, tuple)) else o for o in options]
    return next((v for v in vals if want(str(v))), None)


def run_flow(env, ha):
    r_cfg = env.radio
    base = "/config/config_entries/flow"
    r = ha.rest("POST", base, {"handler": "zha", "show_advanced_options": True}, timeout=300)
    fid, port_tries = r["flow_id"], 0
    try:
        for _ in range(20):
            t, step = r.get("type"), r.get("step_id")
            util.log(f"flow: {t} {step} {r.get('menu_options') or ''} {r.get('errors') or ''}")
            schema = {f["name"]: f for f in r.get("data_schema") or []}
            if t == "create_entry":
                res = r.get("result")
                return res["entry_id"] if isinstance(res, dict) else res
            if t == "abort":
                raise RuntimeError(f"flow aborted: {r.get('reason')}")
            if t == "form" and step == "choose_serial_port":
                opts = (schema.get("path") or {}).get("options") or []
                val = _pick(opts, lambda v: v == r_cfg["port"]) or _pick(opts, lambda v: "manual" in v.lower())
                if val is None:
                    raise RuntimeError(f"port {r_cfg['port']} not offered and no manual option: {opts}")
                r = ha.rest("POST", f"{base}/{fid}", {"path": val}, timeout=300)
            elif t == "form" and step == "manual_pick_radio_type":
                if not r_cfg["type"]:
                    raise RuntimeError("HA could not probe the radio and radio.type is not set")
                opts = schema["radio_type"].get("options") or []
                val = _pick(opts, lambda v: v.lower().startswith(r_cfg["type"]))
                if val is None:
                    raise RuntimeError(f"radio type {r_cfg['type']} not offered: {opts}")
                r = ha.rest("POST", f"{base}/{fid}", {"radio_type": val}, timeout=300)
            elif t == "form" and step == "manual_port_config":
                port_tries += 1
                if port_tries > 4:
                    raise RuntimeError("the radio did not answer HA's probe 4 times")
                if port_tries > 1:
                    time.sleep(5)
                body = {"path": r_cfg["port"]}
                if "baudrate" in schema:
                    body["baudrate"] = r_cfg["baudrate"]
                if "flow_control" in schema:
                    fc_opts = schema["flow_control"].get("options") or []
                    fc = r_cfg["flow_control"] or _pick(fc_opts, lambda v: v.lower() in ("none", "no"))
                    if fc:
                        body["flow_control"] = fc
                r = ha.rest("POST", f"{base}/{fid}", body, timeout=300)
            elif t == "form" and step == "verify_radio":
                r = ha.rest("POST", f"{base}/{fid}", {}, timeout=300)
            elif t == "menu" and step == "choose_setup_strategy":
                # NEVER "recommended": on a radio without a ZHA backup it can form a new network
                r = ha.rest("POST", f"{base}/{fid}", {"next_step_id": "setup_strategy_advanced"}, timeout=300)
            elif t == "menu" and step == "choose_formation_strategy":
                opts = r["menu_options"]
                if "choose_automatic_backup" in opts:
                    raise RuntimeError(f"STOP: ZHA thinks the radio differs from the seeded backup ({opts})")
                if "reuse_settings" not in opts:
                    raise RuntimeError(f"STOP: the radio is not seen as formed ({opts})")
                r = ha.rest("POST", f"{base}/{fid}", {"next_step_id": "reuse_settings"}, timeout=300)
            elif t in ("progress", "progress_done"):
                time.sleep(2)
                r = (ha.rest("POST", f"{base}/{fid}", {}, timeout=300) if t == "progress_done"
                     else ha.rest("GET", f"{base}/{fid}", timeout=300))
            else:
                raise RuntimeError(f"unexpected flow step {t}/{step}")
        raise RuntimeError("the flow did not finish")
    except Exception:
        abort_flows(ha)
        raise


def abort_flows(ha):
    for f in ha("config_entries/flow/progress"):
        if f["handler"] == "zha":
            try:
                ha.rest("DELETE", f"/config/config_entries/flow/{f['flow_id']}")
            except Exception:  # noqa: BLE001
                pass


def cutover(env, reuse_capture=False):
    util.confirm()
    util.detach("cutover", env.state_dir)
    ha = env.ha()
    phase = {"stopped": False, "flow": False, "owned": False, "boot": None}
    try:
        return _cutover(env, ha, phase, reuse_capture)
    except BaseException as e:
        if not phase["stopped"] or phase["owned"]:
            raise
        reason = str(e) or type(e).__name__
        if isinstance(e, Exception):
            traceback.print_exc(file=sys.stdout)  # details first, the log must end with the RESULT line
        print(_recover(env, phase, reason), flush=True)
        if isinstance(e, Exception):
            sys.exit(1)
        raise  # SystemExit (SIGTERM) and KeyboardInterrupt keep their own exit code


def _recover(env, phase, reason):
    """Z2M was stopped and ZHA does not own the radio yet: bring Z2M back. Returns the RESULT line."""
    try:
        if phase["flow"]:
            ha = env.ha()  # a fresh connection, the old one may be the thing that failed
            abort_flows(ha)
            # A lost create-entry response can mean ZHA already owns the radio.
            if any(e.get("source") != "ignore" for e in ha("config_entries/get", domain="zha")):
                return (f"RESULT: ZHA OWNS THE RADIO - {reason}. The flow answer was lost but a ZHA entry exists, "
                        "so Zigbee2MQTT stays stopped and the registry is untouched. Check that ZHA is loaded, "
                        "then: plan, apply --yes. Or go back with: rollback --yes")
        if not env.wait_port_free(180):
            raise RuntimeError(f"the radio port is still held by {env.port_holders()}")
        env.z2m.start(boot=phase["boot"])
        return f"RESULT: ABORTED - {reason}. Zigbee2MQTT was started again, the registry is untouched"
    except BaseException as recovery_error:  # noqa: BLE001
        return (f"RESULT: RECOVERY REQUIRED - {reason}. Z2M was not safely restarted: {recovery_error}. "
                "Check ZHA entries, pending ZHA flows and the radio port before starting Z2M.")


def _cutover(env, ha, phase, reuse_capture=False):
    zha_entries = ha("config_entries/get", domain="zha")
    real = [e for e in zha_entries if e.get("source") != "ignore"]
    if real:
        raise SystemExit("refusing: a ZHA config entry already exists. Delete it first (no device is affected)")
    for e in zha_entries:  # an ignored USB discovery would make the new flow abort as already configured
        ha.rest("DELETE", f"/config/config_entries/entry/{e['entry_id']}")
        util.log("removed the ignored ZHA discovery entry", e["entry_id"])
    abort_flows(ha)  # a pending USB discovery flow holds the same unique id
    if not env.z2m.is_running() and env.z2m.slug:
        raise SystemExit("refusing: Zigbee2MQTT is not running (start it, capture needs it)")
    env.z2m.need_control()
    phase["boot"] = env.z2m.boot_mode()  # known before the stop, so every recovery restores it
    r_cfg = env.radio
    util.log(f"radio {r_cfg['port']} type {r_cfg['type'] or 'auto'} baud {r_cfg['baudrate']}")
    cap_path = env.state("capture.json")
    if reuse_capture and os.path.exists(cap_path) and time.time() - os.path.getmtime(cap_path) < 3600:
        util.log("== 1. reusing capture.json from", time.strftime("%H:%M:%S", time.localtime(os.path.getmtime(cap_path))))
    else:
        util.log("== 1. capture (Zigbee2MQTT still running)")
        registry.capture(env)

    util.log("== 1b. test build of zigbee.db (Zigbee2MQTT still running)")
    try:
        problem = radio.not_ready(radio.build(env, f"{env.staging}/zigbee.db.test", seed=True))
    except Exception as e:  # noqa: BLE001
        problem = f"build failed: {e}"
    finally:
        radio.cleanup_staging(env)
    if problem:
        raise SystemExit(f"refusing: the preseeded zigbee.db would not be clean ({problem}). "
                         "Zigbee2MQTT was not stopped. Run build-test and fix what it shows")

    util.log("== 2. stopping Zigbee2MQTT (boot=manual)      >>> ZIGBEE OUTAGE STARTS")
    t_out = time.time()
    phase["stopped"] = True  # set first: a stop that fails half way must still be recovered
    env.z2m.stop()
    if not env.wait_port_free(60):
        raise RuntimeError(f"serial port still held by {env.port_holders()} after 60 s")
    util.log("port free")

    util.log("== 3. build + validate zigbee.db")
    target = env.zha_database
    new = f"{env.staging}/zigbee.db.new"
    try:
        try:
            s = radio.build(env, new, seed=True)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"preseed build failed: {e}") from e
        problem = radio.not_ready(s)
        if problem:
            raise RuntimeError(f"preseed not clean: {problem}")
        expected = env.cfg["expected_devices"] or s["built"]
        try:
            if util.exists(target):
                db_backup = f"{target}.pre-z2m2zha-{int(time.time())}"
                util.fs("mv", target, db_backup)
                util.log("previous zigbee.db kept at", db_backup)
            util.fs("mv", new, target)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"could not put zigbee.db in place: {e}") from e
    finally:
        radio.cleanup_staging(env)  # also on failure: the staged copies contain the network key

    util.log("== 4. ZHA config flow (advanced -> reuse_settings)")
    time.sleep(3)
    phase["flow"] = True
    entry_id = run_flow(env, ha)
    phase["owned"] = True
    util.log("ZHA entry created:", entry_id)

    util.log("== 5. waiting for ZHA to load")
    st, n = "missing", 0
    for _ in range(120):
        es = ha("config_entries/get", domain="zha")
        st = es[0]["state"] if es else "missing"
        n = sum(1 for d in ha("config/device_registry/list") if any(i[0] == "zha" for i in d["identifiers"]))
        if st == "loaded" and n >= expected + 1:  # + the coordinator
            break
        time.sleep(2)
    util.log(f"ZHA state={st}, ZHA devices={n}      >>> ZIGBEE BACK (outage {time.time() - t_out:.0f} s)")
    if st != "loaded":
        print("RESULT: ZHA NOT LOADED - investigate. Z2M stays stopped. Rollback: python3 -m z2m2zha rollback --yes",
              flush=True)
        sys.exit(2)

    groups = (env.load_state("capture.json").get("features") or {}).get("groups") or []
    if groups:
        util.log("== 5b. Z2M groups -> ZHA groups (same group ids)")
        try:
            registry.groups(env, ha)
        except Exception as e:  # noqa: BLE001 (a group problem must not stop the cutover, plan shows it)
            util.log("group creation failed:", e)

    util.log("== 6. plan loop")
    deadline, blocks = time.time() + 600, None
    while time.time() < deadline:
        time.sleep(20)
        blocks = registry.plan(env)
        if not blocks:
            break
        ask_counters(env, ha)
    if blocks:
        print("RESULT: PLAN HAS BLOCKING ROWS - ZHA runs, the registry is NOT changed yet. Fix them "
              "(state/overrides.json or a zha device_config), then: plan, apply --yes", flush=True)
        sys.exit(3)

    util.log("== 7. apply")
    registry.apply(env)
    util.log("== 8. verify")
    bad = registry.verify(env)
    if bad:
        raise SystemExit(f"RESULT: CUTOVER INCOMPLETE - {bad} entities are missing or unavailable. "
                         "ZHA stays running. Review the verify output.")
    waiting = env.load_state("verify.json", {}).get("waiting", 0)
    util.log(f"done. Zigbee outage was {time.time() - t_out:.0f} s until the entities were renamed")
    print("RESULT: CUTOVER APPLIED - " + (f"{waiting} entities wait for their first report (sleepy devices, "
                                          "run verify again later). " if waiting else "")
          + "Review the verify output. Some config entities may need: nudge --yes", flush=True)


def ask_counters(env, ha):
    """Energy counters without a value yet: read current_summ_delivered now instead of waiting."""
    pl = env.load_state("plan.json")
    for r in pl["rows"]:
        if not (r["new"] and "no value yet" in r["status"]):
            continue
        e = ha("config/entity_registry/get", entity_id=r["new"])
        parts = e["unique_id"].split("-")
        if len(parts) < 3 or not parts[2].split("_")[0].isdigit() or int(parts[2].split("_")[0]) != 0x0702:
            continue
        try:
            ha("zha/devices/clusters/attributes/value", ieee=parts[0], endpoint_id=int(parts[1]), cluster_id=0x0702,
               cluster_type="in", attribute=0x0000)
            util.log(f"    read {r['new']}")
        except Exception as ex:  # noqa: BLE001
            util.log(f"    read failed {r['new']}: {str(ex)[:80]}")

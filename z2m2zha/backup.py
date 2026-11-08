"""Backups before the cutover: a copy of the Z2M data dir and a full Home Assistant backup."""
import time

from . import util


def backup(env):
    """Copy the Z2M data dir (without logs) into the HA config dir, then create a full HA backup
    with the user's own automatic backup settings (their locations and encryption choice).

    The Z2M copy lands inside the HA config, so the HA backup contains it as well. Creating a
    backup freezes add-ons for a while (an SSH add-on session can drop), so this runs detached.
    """
    util.confirm()
    util.detach("backup", env.state_dir)
    ts = time.strftime("%Y%m%d-%H%M%S")
    dst = f"{env.ha_config_dir}/z2m2zha-backups/z2m-data-{ts}"
    util.fs("mkdir", "-p", dst)
    util.fs("chmod", "700", f"{env.ha_config_dir}/z2m2zha-backups")
    util.fs("cp", "-a", env.z2m.data_dir + "/.", dst)
    util.fs("rm", "-rf", f"{dst}/log")
    util.fs("cp", "-a", f"{env.ha_config_dir}/configuration.yaml", f"{dst}/ha-configuration.yaml")
    util.fs("chmod", "700", dst)
    for f in ("database.db", "coordinator_backup.json"):
        if util.read_bytes(f"{dst}/{f}") != util.read_bytes(env.z2m.file(f)):
            print(f"RESULT: FAIL - the copy of {f} differs from the original (Z2M wrote it meanwhile?), run again")
            return
    util.log(f"Z2M data dir copied to {dst} (verified database.db and coordinator_backup.json)")

    ha = env.ha()
    before = {b["backup_id"] for b in ha("backup/info").get("backups", [])}
    t0 = time.time()
    try:
        ha("backup/generate_with_automatic_settings")
        how = "automatic backup settings (your locations and encryption choice)"
    except Exception as e:  # noqa: BLE001  automatic settings not configured, or an older HA
        util.log("automatic backup settings not usable:", str(e)[:120])
        agents = [a["agent_id"] for a in ha("backup/agents/info").get("agents", [])]
        local = [a for a in agents if a.endswith(".local")] or agents[:1]
        ha("backup/generate", agent_ids=local, include_homeassistant=True, include_all_addons=True,
           include_database=True, name=f"Before Z2M to ZHA {ts}")
        how = f"agents {local}, NOT encrypted (no password given)"
    util.log("full HA backup started with", how)
    new = None
    while time.time() - t0 < 3600:
        time.sleep(20)
        try:
            ha = env.ha()  # the connection can drop while add-ons are frozen
            new = [b for b in ha("backup/info").get("backups", []) if b["backup_id"] not in before]
        except Exception:  # noqa: BLE001
            continue
        if new:
            break
    if not new:
        print("RESULT: FAIL - no new HA backup appeared within 1 hour. Check Settings > System > Backups")
        return
    b = new[0]
    failed = b.get("failed_agent_ids") or []
    agents = b.get("agents") or {}  # {agent_id: {"size": bytes, "protected": bool}}
    size = max((a.get("size") or 0 for a in agents.values()), default=0)
    where = ", ".join(f"{k} ({'encrypted' if a.get('protected') else 'not encrypted'})" for k, a in agents.items())
    print(f"RESULT: {'PASS' if not failed else 'PARTIAL'} - HA backup {b['backup_id']} '{b.get('name')}' "
          f"{size / 1e9:.2f} GB on {where}. Z2M copy {dst}" + (f". FAILED agents {failed}" if failed else ""))

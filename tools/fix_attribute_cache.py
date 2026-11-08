#!/usr/bin/env python3
"""Set cached ZCL attributes in a zigpy database (zigbee.db). Standard library only.

Use case: a smart plug whose quirk does not cover its manufacturer string reports energy 100x
too high or without a unit, because ZHA never got the metering multiplier/divisor (Z2M had them
built into its converter, so they were never cached). Write them into the cache once:

    # HA must be STOPPED (ha core stop), the DB is in use otherwise
    sudo python3 tools/fix_attribute_cache.py /homeassistant/zigbee.db --manufacturer _TZ3000_xxxxxxxx \\
        --cluster 0x0702 --set 0x0300=0 --set 0x0301=1 --set 0x0302=100 --set 0x0306=0
    # unit kWh (0), multiplier 1, divisor 100, metering device type electric (0)

Select the device with --manufacturer (substring of the cached Basic manufacturer name, must
match exactly one device) or --ieee aa:bb:cc:dd:ee:ff:00:11. A copy of the DB (including the WAL,
via SQLite's backup API) is written next to it first.
"""
import argparse
import os
import sqlite3
import sys
import time


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db")
    who = ap.add_mutually_exclusive_group(required=True)
    who.add_argument("--manufacturer")
    who.add_argument("--ieee")
    ap.add_argument("--endpoint", type=int, default=1)
    ap.add_argument("--cluster", type=lambda x: int(x, 0), required=True)
    ap.add_argument("--set", action="append", required=True, metavar="ATTR=VALUE", help="ints, 0x.. allowed")
    a = ap.parse_args()

    c = sqlite3.connect(a.db)
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE name LIKE 'attributes_cache_v%'")]
    table = max(tables, key=lambda t: int(t.rsplit("_v", 1)[1]))
    if a.ieee:
        ieee = a.ieee.lower()
    else:
        rows = c.execute(f"SELECT DISTINCT ieee FROM {table} WHERE cluster_id=0 AND attr_id=4 AND value LIKE ?",
                         (f"%{a.manufacturer}%",)).fetchall()
        if len(rows) != 1:
            sys.exit(f"--manufacturer matched {len(rows)} devices, need exactly 1: {[r[0] for r in rows]}")
        ieee = rows[0][0]
    backup = f"{a.db}.pre-attrfix-{int(time.time())}"
    with sqlite3.connect(backup) as dst:
        c.backup(dst)
    os.chmod(backup, 0o600)
    now = time.time()
    for item in a.set:
        attr, val = (int(x, 0) for x in item.split("=", 1))
        n = c.execute(f"UPDATE {table} SET value=?, status=0, last_updated=? WHERE ieee=? AND endpoint_id=? "
                      "AND cluster_type=0 AND cluster_id=? AND attr_id=? AND manufacturer_code IS NULL",
                      (val, now, ieee, a.endpoint, a.cluster, attr)).rowcount
        if n == 0:
            c.execute(f"INSERT INTO {table} (ieee, endpoint_id, cluster_type, cluster_id, attr_id, manufacturer_code,"
                      " status, value, last_updated) VALUES (?,?,0,?,?,NULL,0,?,?)",
                      (ieee, a.endpoint, a.cluster, attr, val, now))
    c.commit()
    print(f"{ieee} ep{a.endpoint} cluster 0x{a.cluster:04x}:",
          c.execute(f"SELECT attr_id, value FROM {table} WHERE ieee=? AND endpoint_id=? AND cluster_id=? "
                    "ORDER BY attr_id", (ieee, a.endpoint, a.cluster)).fetchall())
    print("backup of the previous DB:", backup)


if __name__ == "__main__":
    main()

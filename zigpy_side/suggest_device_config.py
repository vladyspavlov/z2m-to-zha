"""Find Zigbee2MQTT switches that ZHA would expose as lights, and print the zha: device_config fix.

    python3 suggest_device_config.py <z2m database.db>     stdin: JSON list of "0x<ieee>" with a Z2M switch

ZHA decides light vs switch from the endpoint's (profile, device type). Many relays (for example
Tuya TS0001/TS0011) announce an "on/off light" device type, so ZHA creates light.* where Z2M had
switch.*. The entity_id would change domain and its history could not continue, and helpers
built on it (switch_as_x, groups) would break. The fix is a YAML override (needs one HA restart,
do it BEFORE the cutover):

    zha:
      device_config:
        "<ieee>-<endpoint>":
          type: switch
"""
import json
import sys

try:
    from zha.application.platforms.light import LIGHT_PROFILE_DEVICE_TYPES
except ImportError:  # older/newer zha layouts
    from zha.application.platforms.light.const import LIGHT_PROFILE_DEVICE_TYPES

switch_ieees = set(json.load(sys.stdin))
out = []
for line in open(sys.argv[1]):
    if not line.strip():
        continue
    r = json.loads(line)
    if r.get("ieeeAddr") not in switch_ieees:
        continue
    h = r["ieeeAddr"][2:].lower().rjust(16, "0")
    ieee = ":".join(h[i:i + 2] for i in range(0, 16, 2))
    for epid, ep in sorted((r.get("endpoints") or {}).items(), key=lambda x: int(x[0])):
        if 6 in ep.get("inClusterList", []) and (ep.get("profId", 260), ep.get("devId", 0)) in LIGHT_PROFILE_DEVICE_TYPES:
            out.append((f"{ieee}-{int(epid)}", r.get("modelId"), r.get("manufName")))
if not out:
    print("# nothing to do: ZHA keeps every Z2M switch a switch")
else:
    print("zha:\n  device_config:")
    for key, model, manuf in out:
        print(f'    "{key}":  # {manuf} {model}\n      type: switch')

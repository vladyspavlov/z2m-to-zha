"""Read Zigbee2MQTT's YAML settings. Prints JSON with ONLY the serial settings and friendly names.

stdin: JSON {"configuration.yaml": "<text>", "devices.yaml": "<text>", ...}
Nothing else (MQTT credentials, network key) is ever printed.
"""
import json
import sys

import yaml


class Loader(yaml.SafeLoader):
    pass


Loader.add_multi_constructor("!", lambda loader, suffix, node: None)  # !secret and friends

files = json.load(sys.stdin)
conf = yaml.load(files.get("configuration.yaml") or "{}", Loader=Loader) or {}
serial = conf.get("serial") or {}
out_serial = {k: serial.get(k) for k in ("port", "adapter", "baudrate", "rtscts") if serial.get(k) is not None}

devices = conf.get("devices") or {}
sources = devices if isinstance(devices, list) else [devices]
names = {}
for src in sources:
    if isinstance(src, str):
        src = yaml.load(files.get(src) or "{}", Loader=Loader) or {}
    for ieee, d in (src or {}).items():
        if isinstance(d, dict) and d.get("friendly_name"):
            key = f"0x{ieee:016x}" if isinstance(ieee, int) else str(ieee).lower()  # unquoted 0x.. is an int
            names[key] = d["friendly_name"]
print(json.dumps({"serial": out_serial, "names": names}))

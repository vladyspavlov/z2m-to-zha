"""Read Zigbee2MQTT's YAML settings. Prints JSON with the serial settings, friendly names, device and
group options, availability and the external converter list.

stdin: JSON {"configuration.yaml": "<text>", "devices.yaml": "<text>", ...}
The mqtt, frontend and advanced sections (credentials, network key) are never printed, except
advanced.transmit_power and advanced.channel.
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


def scalar(v):
    return v if isinstance(v, (bool, int, float)) or (isinstance(v, str) and len(v) < 80) else "<set>"


def options(d):
    """Device options without the name. Values only for plain scalars, nested settings as key names."""
    out = {}
    for k, v in (d or {}).items():
        if k in ("friendly_name", "description"):
            continue
        out[k] = sorted(v) if isinstance(v, dict) else (scalar(v) if not isinstance(v, list) else f"<{len(v)} items>")
    return out


def merged(key):
    """A section that may be inline or a list of extra YAML files (devices, groups)."""
    val = conf.get(key) or {}
    out = {}
    for src in (val if isinstance(val, list) else [val]):
        if isinstance(src, str):
            src = yaml.load(files.get(src) or "{}", Loader=Loader) or {}
        out.update(src if isinstance(src, dict) else {})
    return out


names, dev_options = {}, {}
for ieee, d in merged("devices").items():
    key = f"0x{ieee:016x}" if isinstance(ieee, int) else str(ieee).lower()  # unquoted 0x.. is an int
    if isinstance(d, dict) and d.get("friendly_name"):
        names[key] = d["friendly_name"]
    if isinstance(d, dict) and options(d):
        dev_options[key] = options(d)
groups = {}
for gid, g in merged("groups").items():
    if isinstance(g, dict):
        groups[str(gid)] = {"friendly_name": g.get("friendly_name"), "options": options(g)}
adv = conf.get("advanced") or {}
out_adv = {k: adv[k] for k in ("transmit_power", "channel") if isinstance(adv.get(k), int)}  # never the key
avail = conf.get("availability")
ext = conf.get("external_converters")
print(json.dumps({"serial": out_serial, "names": names, "advanced": out_adv, "device_options": dev_options,
                  "global_device_options": options(conf.get("device_options")), "groups": groups,
                  "availability": avail if isinstance(avail, (bool, dict)) else None,
                  "external_converters": ext if isinstance(ext, list) else []}, default=str))

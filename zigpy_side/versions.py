"""Print the versions of the Zigbee packages in HA's Python and zigpy's database schema version."""
import importlib.metadata as md

import zigpy.appdb

parts = []
for p in ("zha", "zigpy", "zha-quirks", "zigpy-znp", "bellows", "zigpy-deconz", "zigpy-zigate", "zigpy-xbee"):
    try:
        parts.append(f"{p} {md.version(p)}")
    except md.PackageNotFoundError:
        pass
print(", ".join(parts) + f", zigpy DB schema v{zigpy.appdb.DB_VERSION}")

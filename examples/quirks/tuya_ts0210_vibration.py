"""Tuya TS0210 vibration sensor (_TZ3000_bmfw9ykl) with the Z2M sensitivity setting, quirks v2.

Same behaviour as the zha-quirks v1 quirk ts0210.TuyaVibration_TO (vibration from the zone
status notifications, reset after 15 s), plus the "sensitivity" number Zigbee2MQTT exposes:
IAS zone attribute currentZoneSensitivityLevel 0x0013, 0 = most sensitive, 50 = least
(zigbee-herdsman-converters, devices/tuya.ts, TS0210_sensitivity).

The v1 vibration cluster keeps all writes local, so a write of 0x0013 is sent to the device
here. The sensor sleeps: press its button right before changing the value (as Z2M says too).
"""

import zigpy.zcl
from zigpy.zcl import foundation
from zigpy.zcl.clusters.security import IasZone

from zhaquirks import Bus
from zhaquirks.builder import QuirkBuilder
from zhaquirks.device import CustomZigpyDevice
from zhaquirks.tuya.ts0210 import VibrationCluster

SENSITIVITY_ID = IasZone.AttributeDefs.current_zone_sensitivity_level.id


class TuyaVibrationDevice(CustomZigpyDevice):
    """Provides the bus the vibration cluster signals motion on."""

    def __init__(self, *args, **kwargs):
        """Init device."""
        self.motion_bus = Bus()
        super().__init__(*args, **kwargs)


class VibrationSensitivityCluster(VibrationCluster):
    """v1 vibration cluster that sends the sensitivity to the device."""

    async def write_attributes(self, attributes, *args, **kwargs):
        """Send currentZoneSensitivityLevel over the air, keep everything else local."""
        remote = {}
        local = {}
        for key, value in attributes.items():
            if self.find_attribute(key).id == SENSITIVITY_ID:
                remote[key] = value
            else:
                local[key] = value

        result = [[foundation.WriteAttributesStatusRecord(foundation.Status.SUCCESS)]]
        if remote:
            result = await zigpy.zcl.Cluster.write_attributes(self, remote, *args, **kwargs)
        if local:
            await super().write_attributes(local, *args, **kwargs)
        return result


(
    QuirkBuilder("_TZ3000_bmfw9ykl", "TS0210")
    .zigpy_device_class(TuyaVibrationDevice)
    .replaces(VibrationSensitivityCluster)
    .number(
        IasZone.AttributeDefs.current_zone_sensitivity_level.name,
        IasZone.cluster_id,
        min_value=0,
        max_value=50,
        step=1,
        mode="box",
        unique_id_suffix="sensitivity",
        translation_key="sensitivity",
        fallback_name="Sensitivity",
    )
    .add_to_registry()
)

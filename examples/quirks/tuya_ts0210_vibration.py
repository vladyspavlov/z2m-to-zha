"""Tuya TS0210 vibration sensor (_TZ3000_bmfw9ykl) with the Z2M sensitivity setting, quirks v2.

Same behaviour as the zha-quirks v1 quirk ts0210.TuyaVibration_TO (vibration from the zone
status notifications, reset after 15 s), plus the "sensitivity" number Zigbee2MQTT exposes:
IAS zone attribute currentZoneSensitivityLevel 0x0013, 0 = most sensitive, 50 = least
(zigbee-herdsman-converters, devices/tuya.ts, TS0210_sensitivity).

The v1 vibration cluster keeps all writes local, so a write of 0x0013 is sent to the device
here. The sensor sleeps: press its button right before changing the value (as Z2M says too).

Battery: the sensor has no battery reporting set up, so the battery voltage is read right after a
vibration notification, while it is awake, at most once in 12 h. The percentage is computed from
the voltage (zhaquirks PowerConfigurationCluster, 1.5 V to 2.8 V, as for the TS0203 sensors),
because the device's own value is wrong (it said 1 % at 2.5 V).
"""

import time

import zigpy.zcl
from zigpy.zcl import foundation
from zigpy.zcl.clusters.security import IasZone

from zhaquirks import Bus, PowerConfigurationCluster
from zhaquirks.builder import QuirkBuilder
from zhaquirks.device import CustomZigpyDevice
from zhaquirks.tuya.ts0210 import VibrationCluster

SENSITIVITY_ID = IasZone.AttributeDefs.current_zone_sensitivity_level.id


class BatteryReadOnWake:
    """Reads the battery right after the sensor sent something, while it is still awake.

    These sleepy sensors almost never report the battery on their own (Z2M had reporting set up,
    yet it had no battery value for most of them). A read sent within a second of a zone status
    notification reaches the sensor before it sleeps again. At most one successful read per
    BATTERY_READ_INTERVAL, a failed read is retried on a later event after BATTERY_RETRY.
    """

    BATTERY_ATTRS: tuple[int, ...] = (0x0020,)
    BATTERY_READ_INTERVAL = 12 * 3600
    BATTERY_RETRY = 600
    _battery_last_ok = 0.0
    _battery_last_try = 0.0

    def handle_cluster_request(self, hdr, args, *, dst_addressing=None):
        """Handle the request as before, then maybe read the battery."""
        result = super().handle_cluster_request(hdr, args, dst_addressing=dst_addressing)
        self._maybe_read_battery()
        return result

    def _maybe_read_battery(self) -> None:
        power = getattr(self.endpoint, "power", None)
        if power is None:
            return
        now = time.monotonic()
        if self._battery_last_ok and now - self._battery_last_ok < self.BATTERY_READ_INTERVAL:
            return
        if self._battery_last_try and now - self._battery_last_try < self.BATTERY_RETRY:
            return
        self._battery_last_try = now
        self.create_catching_task(self._read_battery(power))

    async def _read_battery(self, power) -> None:
        try:
            success, _failure = await power.read_attributes(
                list(self.BATTERY_ATTRS), allow_cache=False
            )
        except Exception as exc:  # the sensor may already sleep again
            self.debug("battery read on wake failed: %r", exc)
            return
        if success:
            self._battery_last_ok = time.monotonic()
            self.debug("battery read on wake: %s", success)


class TuyaVibrationDevice(CustomZigpyDevice):
    """Provides the bus the vibration cluster signals motion on."""

    def __init__(self, *args, **kwargs):
        """Init device."""
        self.motion_bus = Bus()
        super().__init__(*args, **kwargs)


class VibrationSensitivityCluster(BatteryReadOnWake, VibrationCluster):
    """v1 vibration cluster that sends the sensitivity to the device and reads the battery."""

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
    .replaces(PowerConfigurationCluster)
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

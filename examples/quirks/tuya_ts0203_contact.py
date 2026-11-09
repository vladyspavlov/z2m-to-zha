"""Tuya TS0203 door and window sensors (_TZ3000_26fmupbb, _TZ3000_oxslv1c9), quirks v2.

Without a quirk ZHA shows the contact (IAS zone) and a battery percentage that comes only from
attribute 0x0021, which these sensors report rarely and wrongly. This quirk matches what
Zigbee2MQTT did for them (zigbee-herdsman-converters, devices/tuya.ts, model TS0203):
  * battery percentage from the voltage, linear 1.5 V to 2.8 V (Z2M "3V_1500_2800"). The
    zha-quirks PowerConfigurationCluster does exactly that and ignores 0x0021.
  * tamper: bit 2 of the IAS zone status (Z2M fz.ias_contact_alarm_1). ZHA writes every zone
    status change notification into the zone_status attribute, so this follows it live.
  * battery read on wake: the sensors almost never report the battery (even under Z2M most had
    no value), so the voltage is read right after each zone status notification, at most once
    in 12 h. Only the voltage is read, the percentage the device itself reports is wrong.
  * no "Opening" binary sensor from the On/Off client cluster: the sensor only uses it to send
    commands to a bound group, so that entity never changes (the contact is the IAS zone sensor).
Nothing else changes: same clusters, same unique_ids for the existing entities.
"""

import time

from zigpy.quirks import CustomCluster
from zigpy.zcl.clusters.general import OnOff
from zigpy.zcl.clusters.security import IasZone

from zhaquirks import PowerConfigurationCluster
from zhaquirks.builder import BinarySensorDeviceClass, EntityType, QuirkBuilder

TAMPER_BIT = 0x0004  # IasZone.ZoneStatus.Tamper


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


class ContactIasZone(BatteryReadOnWake, CustomCluster, IasZone):
    """Standard IAS zone, plus the battery read on wake (voltage only)."""

(
    QuirkBuilder("_TZ3000_26fmupbb", "TS0203")
    .applies_to("_TZ3000_oxslv1c9", "TS0203")
    .replaces(PowerConfigurationCluster)
    .replaces(ContactIasZone)
    .binary_sensor(
        IasZone.AttributeDefs.zone_status.name,
        IasZone.cluster_id,
        entity_type=EntityType.DIAGNOSTIC,
        device_class=BinarySensorDeviceClass.TAMPER,
        attribute_converter=lambda value: bool(int(value) & TAMPER_BIT),
        unique_id_suffix="tamper",
        fallback_name="Tamper",
    )
    .prevent_default_entity_creation(endpoint_id=1, cluster_id=OnOff.cluster_id)
    .add_to_registry()
)

"""Tuya TS0203 door and window sensors (_TZ3000_26fmupbb, _TZ3000_oxslv1c9), quirks v2.

Without a quirk ZHA shows the contact (IAS zone) and a battery percentage that comes only from
attribute 0x0021, which these sensors report rarely and wrongly. This quirk matches what
Zigbee2MQTT did for them (zigbee-herdsman-converters, devices/tuya.ts, model TS0203):
  * battery percentage from the voltage, linear 1.5 V to 2.8 V (Z2M "3V_1500_2800"). The
    zha-quirks PowerConfigurationCluster does exactly that and ignores 0x0021.
  * tamper: bit 2 of the IAS zone status (Z2M fz.ias_contact_alarm_1). ZHA writes every zone
    status change notification into the zone_status attribute, so this follows it live.
Nothing else changes: same clusters, same unique_ids for the existing entities.
"""

from zigpy.zcl.clusters.security import IasZone

from zhaquirks import PowerConfigurationCluster
from zhaquirks.builder import BinarySensorDeviceClass, EntityType, QuirkBuilder

TAMPER_BIT = 0x0004  # IasZone.ZoneStatus.Tamper

(
    QuirkBuilder("_TZ3000_26fmupbb", "TS0203")
    .applies_to("_TZ3000_oxslv1c9", "TS0203")
    .replaces(PowerConfigurationCluster)
    .binary_sensor(
        IasZone.AttributeDefs.zone_status.name,
        IasZone.cluster_id,
        entity_type=EntityType.DIAGNOSTIC,
        device_class=BinarySensorDeviceClass.TAMPER,
        attribute_converter=lambda value: bool(int(value) & TAMPER_BIT),
        unique_id_suffix="tamper",
        fallback_name="Tamper",
    )
    .add_to_registry()
)

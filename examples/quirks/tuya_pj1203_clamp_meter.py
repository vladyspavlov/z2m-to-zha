"""Tuya PJ-MGW1203 / PJ-1203-W clamp energy meter (_TZE204_cjbofhxw), zha-quirks v2 builder.

Not covered by zha-quirks 2.4.1 (checked 2026-11). DP map from the Zigbee2MQTT definition
(zigbee-herdsman-converters tuya.js, model PJ-MGW1203):
    DP 18 current /1000 A    DP 19 power /10 W    DP 20 voltage /10 V
    DP 101 energy per report interval /1000 kWh (NOT a running total)
For a house energy total, feed the power sensor into a Riemann integration helper.
"""

import zigpy.types as t

from zhaquirks.builder import (
    SensorDeviceClass,
    SensorStateClass,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
)
from zhaquirks.tuya.builder import TuyaQuirkBuilder

(
    TuyaQuirkBuilder("_TZE204_cjbofhxw", "TS0601")
    .applies_to("_TZE284_cjbofhxw", "TS0601")
    .tuya_sensor(
        dp_id=18,
        attribute_name="current",
        type=t.uint32_t,
        divisor=1000,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        unit=UnitOfElectricCurrent.AMPERE,
        fallback_name="Current",
    )
    .tuya_sensor(
        dp_id=19,
        attribute_name="power",
        type=t.int32s,
        divisor=10,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        unit=UnitOfPower.WATT,
        fallback_name="Power",
    )
    .tuya_sensor(
        dp_id=20,
        attribute_name="voltage",
        type=t.uint32_t,
        divisor=10,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        unit=UnitOfElectricPotential.VOLT,
        fallback_name="Voltage",
    )
    .tuya_sensor(
        dp_id=101,
        attribute_name="energy",
        type=t.uint32_t,
        divisor=1000,
        # DP101 is the energy used in the LAST REPORT INTERVAL (about 8 minutes), not a running
        # total. No state_class, so no bogus long-term statistics. A real energy total is the
        # Riemann integral of the DP19 power.
        device_class=SensorDeviceClass.ENERGY,
        state_class=None,
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        translation_key="energy_last_interval",
        fallback_name="Energy (last interval)",
    )
    .skip_configuration()
    .add_to_registry()
)

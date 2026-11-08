"""Beok TGR85-ZB / Tuya X5H-GB-B wall thermostat (_TZE200_2ekuz3dz), zha-quirks v2 builder.

Why a custom quirk (checked with zha-quirks 2.4.1): this fingerprint is listed under MoesBHT
(ts0601_electric_heating.py), but its v1 signature lacks the Green Power endpoint 242 that these
units expose, so it never matches. Even if it did, MoesBHT is wrong for the X5H:
  * setpoint DP16 is in deci-degrees on the X5H, MoesBHT treats it as whole degrees
    (21.5 C would read as 215 C and a 21 C write would send 2.1 C)
  * MoesBHT reads DP3 as schedule mode, on the X5H DP3 is the heating running state
  * MoesBHT has no temperature calibration entity

DP map and scaling follow the Zigbee2MQTT x5h_thermostat converter
(zigbee-herdsman-converters lib/legacy.js):
  1 state (bool)  2 mode (enum 0 manual, 1 program)  3 running (bool)  7 sound (bool)
  10 frost protection (bool)  16 setpoint (value /10)  19 upper temp ceiling (value, C)
  24 local temp (value /10, 16-bit signed)  27 calibration (value /10, -9.9..9.9)
  40 child lock (bool)  43 sensor select (enum 0 internal, 1 external, 2 both)
  101 deadzone (value /10)  102 heating temp limit (value, C)  104 backlight (enum 0..3)
Not exposed on purpose: 39 factory reset (dangerous), 30/31 weekly schedule (raw blob).

Entity names and translation keys match the Z2M property names, so z2m2zha can map them.
Note: this device only reports on change and ignores the Tuya data query. After a ZHA start its
entities can stay unknown until something changes. `python3 -m z2m2zha nudge --yes` fixes that.
"""

from zigpy.profiles import zha
import zigpy.types as t
from zigpy.zcl.clusters.hvac import RunningState, Thermostat

from zhaquirks.builder import UnitOfTemperature
from zhaquirks.tuya.builder import TuyaQuirkBuilder, TuyaTemperatureMeasurement
from zhaquirks.tuya.mcu import DPToAttributeMapping
from zhaquirks.tuya.tuya_trv import TuyaThermostatV2


class X5HMode(t.enum8):
    """Program mode (Z2M 'preset')."""

    Manual = 0x00
    Program = 0x01


class X5HSensor(t.enum8):
    """Which sensor regulates (Z2M 'sensor')."""

    Internal = 0x00
    External = 0x01
    Both = 0x02


class X5HBrightness(t.enum8):
    """Backlight brightness (Z2M 'brightness_state')."""

    Off = 0x00
    Low = 0x01
    Medium = 0x02
    High = 0x03


def _signed16(x: int) -> int:
    # Same sign handling as the Z2M converter for DP24
    return x - (1 << 16) + 1 if x & (1 << 15) and x < (1 << 16) else x


class X5HThermostat(TuyaThermostatV2):
    """Thermostat cluster with X5H setpoint limits (5..60 C, matching Z2M)."""

    _CONSTANT_ATTRIBUTES = {
        **TuyaThermostatV2._CONSTANT_ATTRIBUTES,
        Thermostat.AttributeDefs.abs_min_heat_setpoint_limit.id: 500,
        Thermostat.AttributeDefs.abs_max_heat_setpoint_limit.id: 6000,
        Thermostat.AttributeDefs.min_heat_setpoint_limit.id: 500,
        Thermostat.AttributeDefs.max_heat_setpoint_limit.id: 6000,
    }


(
    TuyaQuirkBuilder("_TZE200_2ekuz3dz", "TS0601")
    .replaces_endpoint(1, device_type=zha.DeviceType.THERMOSTAT)
    .tuya_dp(
        dp_id=1,
        ep_attribute=X5HThermostat.ep_attribute,
        attribute_name=X5HThermostat.AttributeDefs.system_mode.name,
        converter=lambda x: Thermostat.SystemMode.Heat if x else Thermostat.SystemMode.Off,
        dp_converter=lambda x: x == Thermostat.SystemMode.Heat,
    )
    .tuya_dp(
        dp_id=3,
        ep_attribute=X5HThermostat.ep_attribute,
        attribute_name=X5HThermostat.AttributeDefs.running_state.name,
        converter=lambda x: RunningState.Heat_State_On if x else RunningState.Idle,
    )
    .tuya_dp(
        dp_id=16,
        ep_attribute=X5HThermostat.ep_attribute,
        attribute_name=X5HThermostat.AttributeDefs.occupied_heating_setpoint.name,
        converter=lambda x: x * 10,          # deci-degree -> centi-degree
        dp_converter=lambda x: round(x / 10),  # centi-degree -> deci-degree (0.5 C steps survive)
    )
    # DP24 feeds BOTH the climate entity and a standalone temperature sensor, so the Z2M
    # local_temperature sensor (with long-term statistics) has a ZHA successor.
    .tuya_dp_multi(
        dp_id=24,
        attribute_mapping=[
            DPToAttributeMapping(
                X5HThermostat.ep_attribute,
                X5HThermostat.AttributeDefs.local_temperature.name,
                converter=lambda x: _signed16(x) * 10,      # deci -> centi-degree
            ),
            DPToAttributeMapping(
                TuyaTemperatureMeasurement.ep_attribute,
                "measured_value",
                converter=lambda x: _signed16(x) * 10,      # deci -> centi-degree
            ),
        ],
    )
    .adds(TuyaTemperatureMeasurement)
    .tuya_number(
        dp_id=27,
        type=t.int32s,
        attribute_name="local_temperature_calibration",
        min_value=-9.9,
        max_value=9.9,
        step=0.1,
        multiplier=0.1,
        unit=UnitOfTemperature.CELSIUS,
        translation_key="local_temperature_calibration",
        fallback_name="Local temperature calibration",
    )
    .tuya_number(
        dp_id=101,
        type=t.uint32_t,
        attribute_name="deadzone_temperature",
        min_value=0.5,
        max_value=9.5,
        step=0.5,
        multiplier=0.1,
        unit=UnitOfTemperature.CELSIUS,
        translation_key="deadzone_temperature",
        fallback_name="Deadzone temperature",
    )
    .tuya_number(
        dp_id=102,
        type=t.uint32_t,
        attribute_name="heating_temp_limit",
        translation_key="heating_temp_limit",
        min_value=5,
        max_value=60,
        step=1,
        unit=UnitOfTemperature.CELSIUS,
        fallback_name="Heating temp limit",
    )
    .tuya_number(
        dp_id=19,
        type=t.uint32_t,
        attribute_name="upper_temp",
        translation_key="upper_temp",
        min_value=35,
        max_value=95,
        step=1,
        unit=UnitOfTemperature.CELSIUS,
        fallback_name="Upper temp",
    )
    .tuya_enum(
        dp_id=2,
        attribute_name="preset_mode",
        translation_key="preset_mode",
        enum_class=X5HMode,
        fallback_name="Program mode",
    )
    .tuya_enum(
        dp_id=43,
        attribute_name="sensor_selection",
        translation_key="sensor_selection",
        enum_class=X5HSensor,
        fallback_name="Sensor",
    )
    .tuya_enum(
        dp_id=104,
        attribute_name="backlight_brightness",
        translation_key="backlight_brightness",
        enum_class=X5HBrightness,
        fallback_name="Brightness state",
    )
    .tuya_switch(
        dp_id=40,
        attribute_name="child_lock",
        translation_key="child_lock",
        fallback_name="Child lock",
    )
    .tuya_switch(
        dp_id=10,
        attribute_name="frost_protection",
        translation_key="frost_protection",
        fallback_name="Frost protection",
    )
    .tuya_switch(
        dp_id=7,
        attribute_name="sound",
        translation_key="sound",
        fallback_name="Sound",
    )
    .adds(X5HThermostat)
    .skip_configuration()
    .add_to_registry()
)

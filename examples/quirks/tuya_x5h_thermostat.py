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
  31 week program(enum 0 "5+2"/1 "6+1"/2 "7")
  30 schedule(raw, 8 periods of hour, minute, temperature x10 as 2 bytes big endian)
  39 factory reset(bool, write only). A button here (Z2M had an ON/OFF switch). It resets all
     settings to default and does not unpair the device. Wait about a minute after pressing.

Clock: the thermostat firmware reads only the "utc" field of the Tuya time reply, takes it as
Unix time and adds a fixed +8 h (China) without wrapping the day, see Koenkk/zigbee2mqtt#14037
and gist sevrugin/12e126d7659f0024f251e3dfc56df072. So both fields get
real UTC - (8 h - local UTC offset), with the offset taken live (DST aware). Like the fix that
users in #14037 confirmed, no time is sent between 00:00 and 07:59 local time, because the
display would then show 24:00 to 31:59. The device asks for the time only after power up, so
the time is also pushed on device traffic, at most once an hour (as the old Z2M converter did).

Schedule: ZHA has no text entities, so it is the attribute "schedule" (0xEF1E) on the Tuya
cluster 0xEF00, in the Z2M text format "06:00/20.5 08:00/15 ... (8 periods)". Write it with
zha.set_zigbee_cluster_attribute (cluster_id 61184, attribute 61214, value the text). Like under
Z2M, the thermostat does not report the schedule by itself (it ignores the Tuya data query too).
The read only "Schedule" sensor shows the last schedule sent or reported. To edit it from the UI,
pair it with an input_text helper and an automation that writes the helper with the action above
and copies the sensor back into the helper when they differ.

Not created: the generic ZCL thermostat sensors pi_heating_demand, setpoint_change_source and
setpoint_change_source_timestamp. The X5H has no such datapoints, so they could only be unknown
(Z2M did not have them either).

Entity names and translation keys match the Z2M property names, so z2m2zha can map them.
Note: this device only reports on change and ignores the Tuya data query. After a ZHA start its
entities can stay unknown until something changes. `python3 -m z2m2zha nudge --yes` fixes that.
"""

import datetime
import time

from zigpy.profiles import zha
import zigpy.types as t
from zigpy.zcl import foundation
from zigpy.zcl.clusters.hvac import RunningState, Thermostat

from zhaquirks.builder import EntityType, UnitOfTemperature
from zhaquirks.tuya.builder import TuyaQuirkBuilder, TuyaTemperatureMeasurement
from zhaquirks.tuya import (
    TUYA_CLUSTER_ID,
    TUYA_SET_TIME,
    TuyaCommand,
    TuyaData,
    TuyaDatapointData,
    TuyaTimePayload,
)
from zhaquirks.tuya.mcu import DPToAttributeMapping, TuyaMCUCluster
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


X5H_FIRMWARE_OFFSET = 8 * 3600  # the firmware always adds this to the time it gets
X5H_NO_SYNC_BEFORE_HOUR = 8  # local hour, see the module docstring
X5H_PUSH_INTERVAL = 3600


class X5HMCUCluster(TuyaMCUCluster):
    """Tuya MCU cluster that sends the X5H a clock it shows correctly."""

    _x5h_last_sync: float = 0.0

    def _x5h_send_time(self) -> bool:
        now = datetime.datetime.now().astimezone()
        if now.hour < X5H_NO_SYNC_BEFORE_HOUR:
            self.debug("X5H time sync skipped before %02d:00 local", X5H_NO_SYNC_BEFORE_HOUR)
            return False
        offset = int(now.utcoffset().total_seconds())
        stamp = int(time.time()) - (X5H_FIRMWARE_OFFSET - offset)
        payload = TuyaTimePayload()
        payload.extend(stamp.to_bytes(4, "big", signed=False))
        payload.extend(stamp.to_bytes(4, "big", signed=False))
        self._x5h_last_sync = time.monotonic()
        self.debug("X5H time sync, compensated stamp %s", stamp)
        self.create_catching_task(
            super(TuyaMCUCluster, self).command(TUYA_SET_TIME, payload, expect_reply=False)
        )
        return True

    def handle_set_time_request(self, payload: t.uint16_t) -> foundation.Status:
        """Answer the device's time request with the compensated time."""
        self._x5h_send_time()
        return foundation.Status.SUCCESS

    async def write_attributes(self, attributes, *args, **kwargs):
        """Send the schedule text as the raw DP30, the MCU write path only carries numbers."""
        schedule = None
        others = {}
        for key, value in attributes.items():
            if self.find_attribute(key).name == "schedule":
                schedule = str(value)
            else:
                others[key] = value
        result = [[foundation.WriteAttributesStatusRecord(foundation.Status.SUCCESS)]]
        if schedule is not None:
            raw = _schedule_to_raw(schedule)  # ValueError on a bad text, nothing is sent then
            command = TuyaCommand(
                status=0,
                tsn=self.endpoint.device.application.get_sequence(),
                datapoints=[TuyaDatapointData(SCHEDULE_DP, TuyaData(raw))],
            )
            await self.command(self.mcu_write_command, command, expect_reply=False)
            self.update_attribute("schedule", _schedule_from_raw(raw))
        if others:
            result = await super().write_attributes(others, *args, **kwargs)
        return result

    def handle_cluster_request(self, hdr, args, *, dst_addressing=None):
        """Handle the request, then push the time when the last sync is older than an hour."""
        result = super().handle_cluster_request(hdr, args, dst_addressing=dst_addressing)
        if (
            hdr.command_id != TUYA_SET_TIME
            and (not self._x5h_last_sync or time.monotonic() - self._x5h_last_sync > X5H_PUSH_INTERVAL)
        ):
            self._x5h_send_time()
        return result


class X5HWeek(t.enum8):
    """Week program (Z2M 'week': 5+2, 6+1, 7)."""

    Days_5_2 = 0x00
    Days_6_1 = 0x01
    Days_7 = 0x02


SCHEDULE_PERIODS = 8
SCHEDULE_DP = 30


class RawBytes(bytes):
    """Bytes sent as a Tuya RAW datapoint as they are."""

    def serialize(self) -> bytes:
        return bytes(self)


def _schedule_from_raw(raw: bytes) -> str:
    """Raw DP30 to the Z2M text, '06:00/20.5 08:00/15 ...'."""
    raw = bytes(raw)
    periods = []
    for i in range(min(SCHEDULE_PERIODS, len(raw) // 4)):
        hour, minute, hi, lo = raw[4 * i : 4 * i + 4]
        periods.append(f"{hour:02d}:{minute:02d}/{((hi << 8) | lo) / 10:g}")
    return " ".join(periods)


def _schedule_to_raw(text: str) -> RawBytes:
    """The Z2M text to raw DP30, with the same checks as the Z2M converter."""
    periods = str(text).split()
    if len(periods) != SCHEDULE_PERIODS:
        raise ValueError(f"schedule needs {SCHEDULE_PERIODS} periods, got {len(periods)}")
    out = bytearray()
    for period in periods:
        time_part, temp_part = period.split("/")
        hour, minute = (int(x) for x in time_part.split(":", 1))
        temp = float(temp_part)
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError(f"invalid time {time_part}")
        if not (5 <= temp <= 60) or (temp * 2) % 1:
            raise ValueError(f"invalid temperature {temp_part} (5 to 60, steps of 0.5)")
        value = round(temp * 10)
        out += bytes([hour, minute, value >> 8, value & 0xFF])
    return RawBytes(out)


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
    # DP24 feeds BOTH the climate entity and a standalone temperature sensor, so that
    # sensor.<thermostat>_local_temperature (which has long-term statistics under Z2M) has a ZHA
    # successor and its statistics continue.
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
    .tuya_enum(
        dp_id=31,
        attribute_name="week",
        enum_class=X5HWeek,
        translation_key="week",
        fallback_name="Week",
    )
    .tuya_dp_attribute(
        dp_id=SCHEDULE_DP,
        attribute_name="schedule",
        type=t.CharacterString,
        access=foundation.ZCLAttributeAccess.Read | foundation.ZCLAttributeAccess.Write,
        converter=_schedule_from_raw,
        dp_converter=_schedule_to_raw,
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
    .sensor(
        "schedule",
        TUYA_CLUSTER_ID,
        attribute_converter=str,
        entity_type=EntityType.DIAGNOSTIC,
        translation_key="schedule",
        fallback_name="Schedule",
    )
    .tuya_dp_attribute(
        dp_id=39,
        attribute_name="factory_reset",
        type=t.Bool,
        access=foundation.ZCLAttributeAccess.Write,
        converter=bool,
        dp_converter=bool,
    )
    .write_attr_button(
        "factory_reset",
        1,
        TUYA_CLUSTER_ID,
        translation_key="factory_reset",
        fallback_name="Factory reset",
    )
    .prevent_default_entity_creation(
        endpoint_id=1, cluster_id=Thermostat.cluster_id, unique_id_suffix="pi_heating_demand"
    )
    .prevent_default_entity_creation(
        endpoint_id=1, cluster_id=Thermostat.cluster_id, unique_id_suffix="setpoint_change_source"
    )
    .prevent_default_entity_creation(
        endpoint_id=1,
        cluster_id=Thermostat.cluster_id,
        unique_id_suffix="setpoint_change_source_timestamp",
    )
    .adds(X5HThermostat)
    .skip_configuration()
    .add_to_registry(replacement_cluster=X5HMCUCluster)
)

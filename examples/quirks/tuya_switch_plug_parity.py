"""Tuya relays and plugs with the settings Zigbee2MQTT exposed for them (quirks v2).

Devices (all keep the clusters of the zha-quirks v1 quirk they used before, so every existing
entity and unique_id stays the same):
  TS0001 _TZ3000_rmjr4ufz, _TZ3000_npzfdcof   relays (v1 was ts000x.Switch_1G_GPP)
  TS0011 _TZ3000_ji4araar                    relay without neutral (v1 was ts001x.Tuya_Single_No_N)
  TS011F _TZ3000_typdpbpg, _TZ3000_gvn91tmx, _TZ3000_hdopuwv6   plugs (v1 was ts011f_plug.Plug)
  TS011F _TZ3000_w0qqde0g                    plug that had no quirk at all

Added on top of what ZHA already showed, with the attributes Zigbee2MQTT uses
(zigbee-herdsman-converters, lib/tuya.ts):
  * countdown (all): genOnOff onTime 0x4001 is read and reported (every 60 s while running),
    and it is SET with the onWithTimedOff command (on_time = off_wait_time = seconds, 0 cancels),
    exactly like Z2M "on_off_countdown". A write of on_time is turned into that command.
  * switch type (relays): cluster 0xE001 attribute 0xD030, toggle 0, state 1, momentary 2.
  * switch type button (two plugs): the same attribute, release 0, press 1.
  * indicator mode (plugs): 0x8001 with the 4th Z2M value "on" (always lit). ZHA's select has
    only 3 options, so it is replaced by one with 4, keeping its unique_id.
  * power on behaviour, indicator and child lock for TS0011 and the w0qqde0g plug: exposing
    the Tuya plug feature makes ZHA create those entities (the same ones the other plugs have).
  * w0qqde0g metering: Tuya metering cluster with multiplier 1 and divisor 100, the usual TS011F scaling (kWh as under Z2M).
"""

from typing import Final

import zigpy.types as t
from zigpy.zcl import foundation
from zigpy.zcl.clusters.general import OnOff
from zigpy.zcl.foundation import ZCLAttributeDef

from zha.application.platforms.select import TuyaBacklightModeSelectEntity
from zha.quirks import TUYA_PLUG_ONOFF
from zhaquirks.builder import UnitOfTime
from zhaquirks.tuya import (
    TuyaZBE000Cluster,
    TuyaZBElectricalMeasurement,
    TuyaZBExternalSwitchTypeCluster,
    TuyaZBMeteringCluster,
    TuyaZBOnOffAttributeCluster,
)
from zhaquirks.tuya.builder import TuyaQuirkBuilder

COUNTDOWN_MAX = 43200  # 12 h, the limit in the Tuya documentation (Z2M enforces the same)


class IndicatorMode(t.enum8):
    """LED indicator mode, Z2M names: off, off/on, on/off, on."""

    Off = 0x00
    LightWhenOn = 0x01
    LightWhenOff = 0x02
    AlwaysOn = 0x03


class SwitchTypeButton(t.enum8):
    """When the plug button actuates, Z2M names: release, press."""

    Release = 0x00
    Press = 0x01


class TuyaOnOffCountdown(TuyaZBOnOffAttributeCluster):
    """Tuya on/off cluster, writing on_time starts the Tuya countdown."""

    class AttributeDefs(TuyaZBOnOffAttributeCluster.AttributeDefs):
        """Attribute definitions, the indicator mode with all 4 values."""

        backlight_mode: Final = ZCLAttributeDef(id=0x8001, type=IndicatorMode)

    async def write_attributes(self, attributes, *args, **kwargs):
        """Send on_time with onWithTimedOff (as Z2M does), everything else as a normal write."""
        on_time = None
        others = {}
        for key, value in attributes.items():
            if self.find_attribute(key).id == OnOff.AttributeDefs.on_time.id:
                on_time = int(value)
            else:
                others[key] = value

        result = [[foundation.WriteAttributesStatusRecord(foundation.Status.SUCCESS)]]
        if on_time is not None:
            on_time = max(0, min(COUNTDOWN_MAX, on_time))
            await self.on_with_timed_off(
                on_off_control=0, on_time=on_time, off_wait_time=on_time
            )
            self._update_attribute(OnOff.AttributeDefs.on_time.id, on_time)
        if others:
            result = await super().write_attributes(others, *args, **kwargs)
        return result


class TuyaButtonSwitchTypeCluster(TuyaZBExternalSwitchTypeCluster):
    """0xE001 with the plug button meaning of 0xD030."""

    class AttributeDefs(TuyaZBExternalSwitchTypeCluster.AttributeDefs):
        """Attribute definitions."""

        external_switch_type: Final = ZCLAttributeDef(id=0xD030, type=SwitchTypeButton)


class TuyaIndicatorModeSelect(TuyaBacklightModeSelectEntity):
    """ZHA's backlight mode select with the 4th (always on) value."""

    _enum = IndicatorMode


def countdown(builder: TuyaQuirkBuilder) -> TuyaQuirkBuilder:
    """Countdown number, Z2M 'countdown' (seconds)."""
    return builder.number(
        OnOff.AttributeDefs.on_time.name,
        OnOff.cluster_id,
        min_value=0,
        max_value=COUNTDOWN_MAX,
        step=1,
        unit=UnitOfTime.SECONDS,
        mode="box",
        unique_id_suffix="countdown",
        translation_key="countdown",
        fallback_name="Countdown",
    )


def relay(builder: TuyaQuirkBuilder) -> TuyaQuirkBuilder:
    """Relays: same clusters as the v1 quirks, plus countdown and switch type."""
    builder = (
        builder.tuya_enchantment()
        .exposes_feature(TUYA_PLUG_ONOFF)
        .replaces(TuyaOnOffCountdown)
        .replaces(TuyaZBE000Cluster)
        .replaces(TuyaZBExternalSwitchTypeCluster)
        .enum(
            TuyaZBExternalSwitchTypeCluster.AttributeDefs.external_switch_type.name,
            TuyaZBExternalSwitchTypeCluster.AttributeDefs.external_switch_type.type,
            TuyaZBExternalSwitchTypeCluster.cluster_id,
            unique_id_suffix="switch_type",
            translation_key="switch_type",
            fallback_name="Switch type",
        )
    )
    return countdown(builder)


def plug(builder: TuyaQuirkBuilder) -> TuyaQuirkBuilder:
    """Plugs: same clusters as the v1 Plug quirk, plus countdown and the 4 value indicator."""
    builder = (
        builder.tuya_enchantment()
        .exposes_feature(TUYA_PLUG_ONOFF)
        .replaces(TuyaOnOffCountdown)
        .replaces(TuyaZBMeteringCluster)
        .replaces(TuyaZBElectricalMeasurement)
        .replaces_entity(
            TuyaBacklightModeSelectEntity,
            TuyaIndicatorModeSelect,
            endpoint_id=1,
            cluster_id=OnOff.cluster_id,
        )
    )
    return countdown(builder)


# Relays with neutral (TS0001) and without (TS0011)
relay(
    TuyaQuirkBuilder("_TZ3000_rmjr4ufz", "TS0001").applies_to("_TZ3000_npzfdcof", "TS0001")
).add_to_registry()

relay(TuyaQuirkBuilder("_TZ3000_ji4araar", "TS0011")).add_to_registry()

# Plugs without the button setting (Z2M: switchTypeButton off for typdpbpg and w0qqde0g)
plug(TuyaQuirkBuilder("_TZ3000_typdpbpg", "TS011F")).replaces(
    TuyaZBE000Cluster
).replaces(TuyaZBExternalSwitchTypeCluster).add_to_registry()

# The w0qqde0g plug has no 0xE000 cluster
plug(TuyaQuirkBuilder("_TZ3000_w0qqde0g", "TS011F")).replaces(
    TuyaZBExternalSwitchTypeCluster
).add_to_registry()

# Plugs with the button setting
plug(
    TuyaQuirkBuilder("_TZ3000_gvn91tmx", "TS011F").applies_to("_TZ3000_hdopuwv6", "TS011F")
).replaces(TuyaZBE000Cluster).replaces(TuyaButtonSwitchTypeCluster).enum(
    TuyaButtonSwitchTypeCluster.AttributeDefs.external_switch_type.name,
    SwitchTypeButton,
    TuyaButtonSwitchTypeCluster.cluster_id,
    unique_id_suffix="switch_type_button",
    translation_key="switch_type_button",
    fallback_name="Switch type button",
).add_to_registry()

"""Unit tests for z2m2zha.features (stdlib only): python3 -m unittest discover -s tests -t ."""
import contextlib
import io
import json
import os
import subprocess
import sys
import unittest

from z2m2zha import features

SFX = "_zigbee2mqtt"


class PureTest(unittest.TestCase):
    def test_group_id(self):
        self.assertEqual(features.group_id("5_light" + SFX, SFX), 5)
        self.assertEqual(features.group_id("12_switch_l1" + SFX, SFX), 12)
        self.assertIsNone(features.group_id("0x00158d0001a2b3c4_light" + SFX, SFX))
        self.assertIsNone(features.group_id("bridge_0x00124b0012345678_restart" + SFX, SFX))

    def test_zha_group_uid(self):
        self.assertEqual(features.zha_group_uid("light", 2), "light_zha_group_0x0002")
        self.assertEqual(features.zha_group_uid("switch", 300), "switch_zha_group_0x012c")

    def test_classify_options(self):
        opts = {"0x01": {"temperature_calibration": -1.5, "retain": True, "optimistic": True},
                "0x02": {"occupancy_timeout": 60, "homeassistant": []},
                "0x03": {"humidity_calibration": 0, "homeassistant": ["name"], "custom_thing": 3}}
        got = features.classify_options(opts, {"0x01": "Bath sensor"}, {"retain": True, "transition": 1})
        self.assertIn(("(all devices)", "transition", 1, "BEHAVIOUR"), got)
        self.assertIn(("Bath sensor", "temperature_calibration", -1.5, "SHIFTS_VALUES"), got)
        self.assertIn(("0x02", "occupancy_timeout", 60, "BEHAVIOUR"), got)
        self.assertIn(("0x03", "homeassistant", ["name"], "HA_OVERRIDE"), got)
        self.assertIn(("0x03", "custom_thing", 3, "OTHER"), got)
        keys = {(n, k) for n, k, _, _ in got}
        self.assertNotIn(("Bath sensor", "retain"), keys)  # MQTT only
        self.assertNotIn(("0x03", "humidity_calibration"), keys)  # 0 changes nothing
        self.assertNotIn(("0x02", "homeassistant"), keys)  # empty override

    def test_groups_from_db(self):
        recs = [{"id": 1, "type": "Coordinator", "ieeeAddr": "0x00124b0000000001"},
                {"id": 7, "type": "Group", "groupID": 7, "members": [
                    {"deviceIeeeAddr": "0xa4c1380000000001", "endpointID": 1},
                    {"deviceIeeeAddr": "0xa4c1380000000002", "endpointID": 2}]},
                {"id": 8, "type": "Group", "groupID": 8, "members": []}]
        self.assertEqual(features.groups_from_db(recs), {7: [("0xa4c1380000000001", 1), ("0xa4c1380000000002", 2)],
                                                         8: []})

    def test_green_power(self):
        recs = [{"type": "GreenPower", "ieeeAddr": "0x1"}, {"type": "EndDevice", "modelId": "GreenPower_2",
                                                           "ieeeAddr": "0x2"}, {"type": "Router", "ieeeAddr": "0x3"}]
        self.assertEqual([r["ieeeAddr"] for r in features.green_power(recs)], ["0x1", "0x2"])

    def test_ieee_colon(self):
        self.assertEqual(features.ieee_colon("0xa4c138f0e1d2c3b4"), "a4:c1:38:f0:e1:d2:c3:b4")


class FakeZ2M:
    data_dir = "/nonexistent/zigbee2mqtt"

    def settings(self):
        return {"device_options": {"0xa4c1380000000001": {"occupancy_timeout": 60}},
                "global_device_options": {"retain": True}, "groups": {"7": {"friendly_name": "Hall", "options": {}}},
                "availability": {"enabled": True}, "external_converters": ["x5h.js"],
                "external_converter_files": ["x5h.js", "ts0601.js"]}

    def devices(self):
        return [{"type": "Group", "groupID": 7, "members": [{"deviceIeeeAddr": "0xa4c1380000000001", "endpointID": 1},
                                                             {"deviceIeeeAddr": "0xa4c1380000000002", "endpointID": 1}]}]

    def friendly_names(self):
        return {"0xa4c1380000000001": "Hall motion", "0xa4c1380000000002": "Hall lamp"}


class FakeSup:
    def get(self, path):
        return {"addons": [{"name": "Node-RED", "slug": "a0d7b954_nodered", "state": "started"},
                           {"name": "Mosquitto broker", "slug": "core_mosquitto", "state": "started"}]}


class FakeEnv:
    z2m, sup, ha_config_dir = FakeZ2M(), FakeSup(), "/nonexistent/homeassistant"


class CollectTest(unittest.TestCase):
    def setUp(self):
        ents = [{"entity_id": "light.hall", "unique_id": "7_light" + SFX},
                {"entity_id": "sensor.remote_action", "unique_id": "0xa4c1380000000003_action" + SFX},
                {"entity_id": "event.remote", "unique_id": "0xa4c1380000000003_event_action" + SFX},
                {"entity_id": "sensor.hall_temp", "unique_id": "0xa4c1380000000001_temperature" + SFX}]
        self.f = features.collect(FakeEnv(), ents, SFX, {"entity_ids": ["sensor.remote_action"]})

    def test_groups(self):
        g = self.f["groups"]
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0]["name"], "Hall")
        self.assertEqual(g[0]["entities"], ["light.hall"])
        self.assertEqual(g[0]["members"][1], {"ieee": "a4:c1:38:00:00:00:00:02", "endpoint_id": 1, "name": "Hall lamp"})

    def test_actions_and_rest(self):
        acts = {a["entity_id"]: a["referenced"] for a in self.f["actions"]}
        self.assertEqual(acts, {"sensor.remote_action": True, "event.remote": False})
        self.assertEqual(self.f["external_converters"], ["ts0601.js", "x5h.js"])
        self.assertEqual(self.f["consumers"], ["add-on Node-RED is running"])
        self.assertEqual(self.f["options"], [("Hall motion", "occupancy_timeout", 60, "BEHAVIOUR")])

    def test_report(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            n = features.report(self.f)
        text = out.getvalue()
        for s in ("Hall", "zha_event", "REWRITE", "occupancy_timeout", "x5h.js", "Node-RED", "2 h"):
            self.assertIn(s, text)
        self.assertEqual(n, 1 + 1 + 1 + 1)  # referenced action, behaviour option, converters, consumer

    def test_report_empty(self):
        empty = {"groups": [], "actions": [], "options": [], "availability": None, "external_converters": [],
                 "green_power": [], "consumers": []}
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(features.report(empty), 0)
        self.assertIn("No Z2M groups", out.getvalue())


class FakeHA:
    def __init__(self, existing=()):
        self.existing, self.calls = list(existing), []

    def __call__(self, t, **kw):
        self.calls.append((t, kw))
        if t == "zha/groups":
            return [{"group_id": g} for g in self.existing]
        if t == "zha/group/add":
            return {"group_id": kw["group_id"], "members": kw["members"]}
        raise AssertionError(t)


class CreateGroupsTest(unittest.TestCase):
    G = [{"id": 7, "name": "Hall", "members": [{"ieee": "a", "endpoint_id": 1, "name": "x"},
                                                {"ieee": "b", "endpoint_id": 2, "name": "y"},
                                                {"ieee": "gone", "endpoint_id": 1, "name": "z"}]},
         {"id": 9, "name": "Old", "members": [{"ieee": "gone", "endpoint_id": 1, "name": "z"}]},
         {"id": 3, "name": "Exists", "members": [{"ieee": "a", "endpoint_id": 1, "name": "x"}]}]

    def test_create(self):
        ha = FakeHA(existing=[3])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            made = features.create_groups(ha, self.G, {"a", "b"})
        self.assertEqual(made, [7])
        add = [kw for t, kw in ha.calls if t == "zha/group/add"]
        self.assertEqual(add, [{"group_name": "Hall", "group_id": 7,
                                "members": [{"ieee": "a", "endpoint_id": 1}, {"ieee": "b", "endpoint_id": 2}]}])
        self.assertIn("already in ZHA", out.getvalue())
        self.assertIn("no member is in ZHA", out.getvalue())


class YamlParserTest(unittest.TestCase):
    """zigpy_side/z2m_yaml.py runs in HA's Python. Here only when PyYAML is installed."""

    def test_parser(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not installed")
        files = {"configuration.yaml": "mqtt:\n  password: !secret pw\nadvanced:\n  network_key: [1, 2]\n"
                                       "  transmit_power: 15\ndevices: devices.yaml\ngroups: groups.yaml\n"
                                       "device_options:\n  retain: true\navailability:\n  enabled: true\n",
                 "devices.yaml": "'0xa4c1380000000001':\n  friendly_name: Bath\n  temperature_calibration: -1\n"
                                 "  homeassistant:\n    name: X\n",
                 "groups.yaml": "'7':\n  friendly_name: Hall\n  transition: 2\n"}
        script = os.path.join(os.path.dirname(__file__), "..", "zigpy_side", "z2m_yaml.py")
        out = json.loads(subprocess.run([sys.executable, script], input=json.dumps(files), capture_output=True,
                                        text=True, check=True).stdout)
        self.assertEqual(out["names"], {"0xa4c1380000000001": "Bath"})
        self.assertEqual(out["device_options"]["0xa4c1380000000001"],
                         {"temperature_calibration": -1, "homeassistant": ["name"]})
        self.assertEqual(out["groups"], {"7": {"friendly_name": "Hall", "options": {"transition": 2}}})
        self.assertEqual(out["advanced"], {"transmit_power": 15})
        self.assertNotIn("network_key", json.dumps(out))
        self.assertNotIn("password", json.dumps(out))


if __name__ == "__main__":
    unittest.main()

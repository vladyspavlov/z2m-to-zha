"""Unit tests for z2m2zha.links (stdlib only): python3 -m unittest discover -s tests"""
import contextlib
import io
import unittest

from z2m2zha import links

DEV_A = "a" * 32
DEV_B = "b" * 32
ZHA_A = "c" * 32
UUID_X = "d" * 32

OLD = [
    {"entity_id": "sensor.boiler_power", "id": UUID_X},
    {"entity_id": "switch.relay_stairs", "id": "e" * 32},
]


def entry(domain, entry_id, options=None, data=None, subentries=None, title=None, disabled_by=None):
    return {"domain": domain, "entry_id": entry_id, "title": title or entry_id, "options": options or {},
            "data": data or {}, "subentries": subentries or [], "disabled_by": disabled_by}


def by_id(found):
    return {f["entry_id"]: f for f in found}


class FindTest(unittest.TestCase):
    def setUp(self):
        self.cfg = [
            entry("switch_as_x", "sax", {"entity_id": "switch.relay_stairs", "target_domain": "light"}),
            entry("statistics", "stat", {"entity_id": "sensor.boiler_power"}),
            entry("utility_meter", "um", {"source": "sensor.boiler_power"}),
            entry("template", "tpl_dev", {"template_type": "sensor", "state": "{{ states('sensor.boiler_power') }}",
                                          "device_id": DEV_A}),
            entry("template", "tpl_ids", {"template_type": "sensor", "state": "{{ states('sensor.boiler_power_2') }}"}),
            entry("powercalc", "pc", {"entity_id": "switch.relay_stairs"}, data={"device": DEV_B}),
            entry("battery_notes", "bn", subentries=[{"subentry_id": "s1", "data": {"device_id": DEV_A}}]),
            entry("custom_thing", "uuid", {"source": UUID_X}),
            entry("homekit", "hk", {"filter": {"include_entities": ["switch.relay_stairs"]}}),
            entry("mqtt", "mq", {"device_id": DEV_A}),
            entry("group", "grp", {"entities": ["sensor.other"]}),
            entry("switch_as_x", "sax_off", {"entity_id": "switch.relay_stairs"}, disabled_by="user"),
        ]
        self.found = by_id(links.find(self.cfg, OLD, {DEV_A, DEV_B}, (), ("template",)))

    def test_classification(self):
        f = self.found
        self.assertEqual(f["sax"]["action"], "KEEP")
        self.assertTrue(f["sax"]["protect"] and f["sax"]["self_deleting"])
        self.assertTrue(f["stat"]["protect"] and f["stat"]["self_deleting"])
        self.assertTrue(f["um"]["protect"])
        self.assertFalse(f["um"]["self_deleting"])
        self.assertEqual(f["tpl_dev"]["action"], "RELINK")
        self.assertEqual(f["tpl_dev"]["relink_keys"], ["device_id"])
        self.assertEqual(f["pc"]["action"], "MANUAL")
        self.assertEqual(f["pc"]["device_ids"], [DEV_B])
        self.assertEqual(f["bn"]["action"], "HANDLED")
        self.assertEqual(f["uuid"]["action"], "MANUAL")
        self.assertTrue(f["uuid"]["entity_uuids"][0].startswith(UUID_X))
        self.assertTrue(f["hk"]["homekit"])

    def test_skipped_and_unrelated(self):
        self.assertNotIn("mq", self.found)  # MQTT itself
        self.assertNotIn("grp", self.found)  # no Z2M reference
        self.assertNotIn("tpl_ids", self.found)  # sensor.boiler_power_2 is not sensor.boiler_power

    def test_protect_list(self):
        prot = {c["entry_id"] for c in links.protect_list({"links": list(self.found.values())})}
        self.assertEqual(prot, {"sax", "stat", "um"})  # not the disabled one, not template/powercalc

    def test_protect_list_old_capture(self):
        cap = {"switch_as_x": [{"entry_id": "x", "title": "x", "disabled_by": None},
                               {"entry_id": "y", "title": "y", "disabled_by": "user"}]}
        self.assertEqual([c["entry_id"] for c in links.protect_list(cap)], ["x"])

    def test_protect_domains_extra(self):
        cfg = [entry("my_custom", "mc", {"entity": "sensor.boiler_power"})]
        self.assertFalse(links.find(cfg, OLD, set())[0]["protect"])
        self.assertTrue(links.find(cfg, OLD, set(), ("my_custom",))[0]["protect"])

    def test_relink_only_for_listed_domains(self):
        found = by_id(links.find(self.cfg, OLD, {DEV_A, DEV_B}, (), ()))
        self.assertEqual(found["tpl_dev"]["action"], "MANUAL")

    def test_attached(self):
        ents = [{"entity_id": "light.relay_stairs", "platform": "switch_as_x", "device_id": DEV_A,
                 "config_entry_id": "sax"},
                {"entity_id": "switch.relay_stairs", "platform": "mqtt", "device_id": DEV_A},
                {"entity_id": "sensor.x", "platform": "template", "device_id": "z" * 32}]
        self.assertEqual([a["entity_id"] for a in links.attached(ents, {DEV_A})], ["light.relay_stairs"])

    def test_report_runs(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            links.report(list(self.found.values()), [], {DEV_A: "Boiler plug"})
        text = out.getvalue()
        self.assertIn("RELINK", text)
        self.assertIn("Boiler plug", text)
        self.assertIn("Apple Home", text)


class FakeEnv:
    def __init__(self, entries):
        self.entries, self.state = entries, {}

    def storage(self, name):
        return {"data": {"entries": self.entries}}

    def load_state(self, name, default=None):
        return self.state.get(name, default)

    def save_state(self, name, data, mode=None):
        self.state[name] = data


class FakeHA:
    def __init__(self, entries, flow_type="form", schema=("template_type", "state", "device_id")):
        self.entries, self.flow_type, self.schema, self.calls = entries, flow_type, schema, []

    def rest(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "POST" and path.endswith("/options/flow"):
            return {"type": self.flow_type, "flow_id": "f1", "data_schema": [{"name": n} for n in self.schema]}
        if method == "POST" and "/options/flow/" in path:
            e = next(x for x in self.entries if x["entry_id"] == "tpl")
            e["options"] = dict(body)
            return {"type": "create_entry"}
        return {}


class FakeDo:
    def __init__(self):
        self.notes = []

    def note(self, type_, **kw):
        self.notes.append((type_, kw))


class RelinkTest(unittest.TestCase):
    def setUp(self):
        self.entries = [entry("template", "tpl", {"template_type": "sensor", "state": "{{ 1 }}", "device_id": DEV_A,
                                                  "unit": None})]
        self.cap = {"links": links.find(self.entries, OLD, {DEV_A}, (), ("template",))}
        self.env = FakeEnv(self.entries)

    def run_relink(self, ha, mapping=None, **kw):
        mapping = {DEV_A: ZHA_A} if mapping is None else mapping
        with contextlib.redirect_stdout(io.StringIO()) as out:
            res = links.relink(self.env, ha, FakeDo(), self.cap, mapping, **kw)
        return res, out.getvalue()

    def test_relink_and_undo(self):
        ha = FakeHA(self.entries)
        res, _ = self.run_relink(ha)
        self.assertEqual(self.entries[0]["options"]["device_id"], ZHA_A)
        self.assertEqual(self.entries[0]["options"]["state"], "{{ 1 }}")  # other options unchanged
        self.assertNotIn("unit", self.entries[0]["options"])  # None is never sent
        self.assertEqual(res[0]["map"], {DEV_A: ZHA_A})
        self.assertEqual(self.env.state["relinked.json"], res)
        # rollback: the Z2M device came back under its old id (tombstone)
        res2, _ = self.run_relink(FakeHA(self.entries), {DEV_A: DEV_A}, undo=True)
        self.assertEqual(self.entries[0]["options"]["device_id"], DEV_A)
        self.assertEqual(res2[0]["entry_id"], "tpl")

    def test_undo_to_new_z2m_device_id(self):
        self.run_relink(FakeHA(self.entries))
        self.run_relink(FakeHA(self.entries), {DEV_A: DEV_B}, undo=True)
        self.assertEqual(self.entries[0]["options"]["device_id"], DEV_B)

    def test_not_a_form_is_aborted(self):
        ha = FakeHA(self.entries, flow_type="menu")
        res, out = self.run_relink(ha)
        self.assertEqual(res, [])
        self.assertIn("FAILED", out)
        self.assertIn(("DELETE", "/config/config_entries/options/flow/f1", None), ha.calls)
        self.assertEqual(self.entries[0]["options"]["device_id"], DEV_A)

    def test_field_not_in_form(self):
        ha = FakeHA(self.entries, schema=("template_type", "state"))
        res, out = self.run_relink(ha)
        self.assertEqual(res, [])
        self.assertIn("not in the form", out)


class CheckTest(unittest.TestCase):
    def test_dangling_and_gone(self):
        entries = [entry("powercalc", "pc", data={"device": DEV_B})]
        cap = {"links": links.find(entries + [entry("statistics", "stat", {"entity_id": "sensor.boiler_power"})],
                                   OLD, {DEV_B}),
               "attached": [{"entity_id": "light.relay_stairs", "platform": "switch_as_x", "device_id": DEV_A,
                             "config_entry_id": "sax"}]}

        class HA:
            def __call__(self, t, **kw):
                if t == "config/device_registry/list":
                    return [{"id": ZHA_A}]
                return [{"entity_id": "light.relay_stairs", "device_id": DEV_A}]

        with contextlib.redirect_stdout(io.StringIO()) as out:
            bad = links.check(HA(), FakeEnv(entries), cap, {DEV_B: ZHA_A})
        text = out.getvalue()
        self.assertEqual(bad, 3)
        self.assertIn("removed Z2M devices", text)
        self.assertIn("GONE", text)  # statistics entry deleted itself
        self.assertIn("sits on a removed device", text)


if __name__ == "__main__":
    unittest.main()

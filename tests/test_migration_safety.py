import contextlib
import io
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from z2m2zha import backup, cutover, registry, util

IEEE = '00:11:22:33:44:55:66:77'
OLD = 'sensor.old_voltage'
NEW = 'sensor.new_voltage'

class Env:
    def __init__(self, root):
        self.state_dir = root
        self.ha_config_dir = root
        self.staging = root
        self.zha_database = root + '/zigbee.db'
        self.radio = {'port': 'fake-port', 'type': 'znp', 'baudrate': 115200}
        self.cfg = {'soft_counters': [], 'expected_devices': 1,
                    'z2m': {'unique_id_suffix': '_zigbee2mqtt'},
                    'helper_reload_domains': [], 'must_exist': [], 'energy': {}}
        self.old = {'entity_id': OLD, 'unique_id': 'old_uid', 'prop': 'voltage', 'domain': 'sensor',
                    'ieee': '0x0011223344556677', 'bridge': False, 'state': '230',
                    'unit': 'V', 'state_class': 'measurement', 'disabled_by': None,
                    'hidden_by': None, 'area_id': None, 'device_id': 'old_device', 'attrs': {}}
        self.device = {'device_id': 'old_device', 'ieee': '0x0011223344556677',
                       'in_z2m': True, 'bridge': False, 'name': 'Test', 'name_by_user': None,
                       'area_id': None, 'labels': []}
        self.saved = {'capture.json': {'entities': [self.old], 'devices': [self.device],
                                     'mqtt_entry': 'mqtt', 'switch_as_x': [
                                         {'entry_id': 'helper', 'title': 'Helper', 'disabled_by': None}]},
                      'refs.json': {'entity_ids': [OLD]},
                      'plan.json': {'blocks': 0, 'devices': [], 'rows': [
                          {'old': OLD, 'new': NEW, 'status': 'MAP'}]}}
        self.z2m = Mock(slug='z2m', data_dir=root)
        self.z2m.is_running.return_value = True
        self.z2m.stop.side_effect = lambda: setattr(self.z2m.is_running, 'return_value', False)
        self.wait_port_free = Mock(return_value=True)
        self.client = Mock()
    def state(self, name):
        return self.state_dir + '/' + name
    def load_state(self, name, default=None):
        return self.saved.get(name, default)
    def save_state(self, name, value, *args):
        self.saved[name] = value
    def ha(self):
        return self.client

class Verification(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = Env(self.tmp.name)
        self.output = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.output))
        self.enterContext(patch.object(sys, 'argv', ['z2m2zha', '--yes']))
        self.enterContext(patch('time.sleep'))

    def plan_client(self, entities=(), states=()):
        def call(cmd, **kw):
            return {'config/entity_registry/list': list(entities),
                    'config/entity_registry/get_entries': {x['entity_id']: x for x in entities},
                    'get_states': list(states),
                    'config/device_registry/list': [{'id': 'new_device', 'identifiers': [('zha', IEEE)]}]}[cmd]
        self.env.client.side_effect = call

    def test_referenced_optional_missing_blocks(self):
        self.plan_client()
        self.assertEqual(registry.plan(self.env), 1)
        row = self.env.saved['plan.json']['rows'][0]
        self.assertIn('BLOCK referenced in config', row['status'])
        self.assertTrue(row['blocking'])
        self.assertNotIn('Plan OK', self.output.getvalue())

    def test_optional_statistics_mismatch_blocks(self):
        self.plan_client([{'entity_id': NEW, 'platform': 'zha', 'unique_id': IEEE + '-1-2820',
                           'original_device_class': 'voltage'}],
                         [{'entity_id': NEW, 'state': '230',
                           'attributes': {'unit_of_measurement': 'kV', 'state_class': 'measurement'}}])
        self.assertEqual(registry.plan(self.env), 1)
        self.assertIn('BLOCK unit V->kV', self.env.saved['plan.json']['rows'][0]['status'])

    def test_backup_timeout_never_requests_fallback(self):
        calls = []
        infos = iter([{'backups': []}, {'backups': [{'backup_id': 'test', 'agents': {}}]}])
        def call(cmd, **kw):
            calls.append((cmd, kw))
            if cmd == 'backup/info':
                return next(infos)
            if cmd == 'backup/generate_with_automatic_settings':
                raise TimeoutError('injected lost response')
            if cmd == 'backup/agents/info':
                return {'agents': [{'agent_id': 'local.local'}]}
            if cmd == 'backup/generate':
                return None
            raise AssertionError(cmd)
        self.env.client.side_effect = call
        with patch.object(util, 'detach'), patch.object(util, 'fs'), patch.object(util, 'read_bytes', return_value=b'fake'):
            with self.assertRaises(SystemExit):
                backup.backup(self.env)
        self.assertFalse(any(cmd == 'backup/generate' for cmd, _ in calls))

    def prepare_cutover(self):
        def call(cmd, **kw):
            if cmd == 'config_entries/get':
                return []
            if cmd == 'config_entries/flow/progress':
                return []
            raise AssertionError(cmd)
        self.env.client.side_effect = call
        self.enterContext(patch.object(util, 'detach'))
        self.enterContext(patch.object(registry, 'capture'))

    def test_port_check_failure_recovers_when_port_becomes_free(self):
        self.prepare_cutover()
        self.env.wait_port_free.side_effect = [RuntimeError('injected port inspection failure'), True]
        with self.assertRaises(RuntimeError):
            cutover.cutover(self.env)
        self.env.z2m.stop.assert_called_once()
        self.env.z2m.start.assert_called_once()

    def test_sigterm_exit_during_build_recovers_z2m(self):
        self.prepare_cutover()
        with patch.object(cutover.radio, 'build', side_effect=SystemExit(143)):
            with self.assertRaises(SystemExit):
                cutover.cutover(self.env)
        self.env.z2m.stop.assert_called_once()
        self.env.z2m.start.assert_called_once()

    def test_failed_rename_keeps_helpers_disabled_and_prevents_success(self):
        self.prepare_cutover()
        loaded = False
        def flow(*args):
            nonlocal loaded
            loaded = True
            return 'zha'
        def call(cmd, **kw):
            if cmd == 'config_entries/get':
                return [{'entry_id': 'zha', 'state': 'loaded'}] if loaded else []
            if cmd == 'config_entries/flow/progress':
                return []
            if cmd == 'config/device_registry/list':
                return [{'id': 'device', 'identifiers': [('zha', IEEE)]},
                        {'id': 'coord', 'identifiers': [('zha', 'coordinator')]}]
            if cmd == 'config/entity_registry/list':
                return [{'entity_id': NEW, 'platform': 'zha', 'unique_id': 'fake'}]
            if cmd == 'get_states':
                return [{'entity_id': NEW, 'state': '230', 'attributes': {}}]
            if cmd == 'config/entity_registry/update':
                raise RuntimeError('injected rename failure')
            if cmd in ('config/device_registry/remove_config_entry', 'config_entries/disable'):
                return None
            raise AssertionError(cmd)
        self.env.client.side_effect = call
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(cutover, 'run_flow', side_effect=flow))
            stack.enter_context(patch.object(cutover.radio, 'build', return_value={
                'problems': 0, 'devices': 1, 'built': 1, 'backup': True}))
            stack.enter_context(patch.object(cutover.radio, 'cleanup_staging'))
            stack.enter_context(patch.object(util, 'fs'))
            stack.enter_context(patch.object(registry, 'plan', return_value=0))
            stack.enter_context(patch.object(registry.links, 'relink'))
            stack.enter_context(patch.object(registry.links, 'reload_linked', return_value=0))
            stack.enter_context(patch.object(registry, 'energy_gap'))
            with self.assertRaises(SystemExit) as raised:
                cutover.cutover(self.env)
        out = self.output.getvalue()
        self.assertIn('FAILED ' + OLD, out)
        self.assertIn('RESULT: APPLY INCOMPLETE', str(raised.exception))
        self.assertNotIn('RESULT: CUTOVER APPLIED', out)
        self.assertNotIn(call('config_entries/disable', entry_id='helper', disabled_by=None),
                         self.env.client.call_args_list)

    def test_rollback_detaches_before_mutation(self):
        self.env.client.side_effect = lambda cmd, **kw: [] if cmd in (
            'config_entries/get', 'config/entity_registry/list') else None
        with patch.object(util, 'detach') as detach:
            def stop(*args):
                detach.assert_called_once_with('rollback', self.env.state_dir)
                raise RuntimeError('stop before mutation')
            with patch.object(registry.links, 'set_disabled', side_effect=stop):
                with self.assertRaises(RuntimeError):
                    registry.rollback(self.env)

    def test_held_port_prevents_unsafe_recovery(self):
        self.prepare_cutover()
        self.env.wait_port_free.return_value = False
        self.env.port_holders = Mock(return_value=['probe'])
        with self.assertRaises(RuntimeError):
            cutover.cutover(self.env)
        self.env.z2m.start.assert_not_called()
        self.assertIn('RESULT: RECOVERY REQUIRED', self.output.getvalue())

    def test_lost_flow_response_does_not_start_z2m_with_existing_zha_entry(self):
        self.prepare_cutover()
        def lost_response(*args):
            self.env.client.side_effect = lambda cmd, **kw: ([{'entry_id': 'zha'}]
                if cmd == 'config_entries/get' else [])
            raise TimeoutError('response lost after creating entry')
        with patch.object(cutover.radio, 'build', return_value={
                'problems': 0, 'devices': 1, 'built': 1, 'backup': True}), patch.object(
                cutover.radio, 'cleanup_staging'), patch.object(util, 'fs'), patch.object(
                cutover, 'run_flow', side_effect=lost_response):
            with self.assertRaises(TimeoutError):
                cutover.cutover(self.env)
        self.env.z2m.start.assert_not_called()
        self.assertIn('RESULT: RECOVERY REQUIRED', self.output.getvalue())

    def test_cutover_verification_controls_final_result(self):
        for bad in (0, 1):
            with self.subTest(bad=bad):
                self.env = Env(self.tmp.name)
                self.prepare_cutover()
                self.env.client.side_effect = [[], [], [{'state': 'loaded'}], [
                    {'identifiers': [('zha', IEEE)]}, {'identifiers': [('zha', 'coordinator')]}]]
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch.object(cutover, 'run_flow', return_value='zha'))
                    stack.enter_context(patch.object(cutover.radio, 'build', return_value={
                        'problems': 0, 'devices': 1, 'built': 1, 'backup': True}))
                    stack.enter_context(patch.object(cutover.radio, 'cleanup_staging'))
                    stack.enter_context(patch.object(util, 'fs'))
                    stack.enter_context(patch.object(registry, 'plan', return_value=0))
                    stack.enter_context(patch.object(registry, 'apply'))
                    stack.enter_context(patch.object(registry, 'verify', return_value=bad))
                    if bad:
                        with self.assertRaisesRegex(SystemExit, 'RESULT: CUTOVER INCOMPLETE'):
                            cutover.cutover(self.env)
                    else:
                        cutover.cutover(self.env)
                self.env.z2m.start.assert_not_called()

    def test_verify_returns_failure_count_for_missing_migrated_entity(self):
        self.plan_client()
        with patch.object(registry, 'energy_gap'):
            self.assertEqual(registry.verify(self.env), 1)

if __name__ == '__main__':
    unittest.main(verbosity=2)

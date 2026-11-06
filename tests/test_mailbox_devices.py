"""Read-only Devices navigation and rendering, using existing bounded snapshots."""
import tests.ham_test_support as ham_test_support  # Protect live state before product imports.
import curses
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from ham.agents.labels import alias_identity
from ham.ui.key_input import ESCAPE, SHIFT_TAB, MouseEvent
from ham.ui.mailbox import MailboxApp, REPO_ENV, local_snapshot
from ham.ui.terminal_text import display_width
from tests import test_mailbox_tui as mailbox_tests


NOW = 1_800_000_000
SIMULATOR = '9B5C8E5D-08DF-47BB-B167-9EC20A3AF024'


def device_snapshots(now=NOW):
    """Pure fixture for unit tests and an isolated real-curses preview."""
    holder = {
        'agent_id': 'agent-holder', 'host': 'local', 'terminal_id': 'holder-terminal-full-id',
        'alias': 'purple-owl', 'pane_id': 'workspace:pane-holder', 'repo_id': '/work/other-repository',
        'workspace_id': 'workspace', 'generation': 1, 'active': True, 'live': True,
        'turn_state': 'idle', 'unread': 0, 'extension_present': True, 'client': 'codex',
        'devices': [], 'servers': [],
    }
    claims = [
        {'kind': 'phone', 'device_id': 'fold-serial-full-identity', 'label': '한글 폴드',
         'holder': holder, 'claimed_at': now - 120, 'stale': False},
        {'kind': 'phone', 'device_id': 'missing-phone-serial', 'label': 'Missing phone',
         'holder': holder, 'claimed_at': now - 300, 'stale': False},
        {'kind': 'phone', 'device_id': 'stale-phone-serial', 'label': 'Old phone',
         'holder': {**holder, 'alias': 'blue-otter', 'live': False},
         'claimed_at': now - 3600, 'stale': True},
        *[{'kind': 'emu', 'device_id': f'emulator-{port}', 'label': f'AVD {port}',
           'holder': holder, 'claimed_at': now - 60, 'stale': False} for port in (5554, 5556)],
    ]
    resources = {
        'device_claims': claims, 'emu_limit': 3,
        'devices_observed': {
            'adb': [{'serial': 'fold-serial-full-identity', 'state': 'device'},
                    {'serial': 'unclaimed-phone-serial', 'state': 'device'},
                    {'serial': 'emulator-5554', 'state': 'device'}],
            'emulator_processes': [{'pid': 1001, 'port': 5554}, {'pid': 1002, 'port': 5556}],
            'ios_simulators_booted': [], 'ios_devices': None, 'error': None,
        },
        'dead_serve': [], 'servers_unowned': [], 'stale_devices': [], 'error': None,
    }
    peers = [{'canonical_name': 'mac', 'uuid': 'mac-uuid'},
             {'canonical_name': 'old-peer', 'uuid': 'old-uuid'}]
    local = {
        'host': 'local', 'hosts': [{'host': 'local', 'uuid': 'local-uuid', 'stale': False,
                                   'resources': resources}],
        'resources': resources, 'agents': [holder], 'peers': peers, 'threads': [],
        'submissions': [], 'outbox': [], 'panes': [], 'workspaces': [], 'settings': None,
        'scope': {'repo_id': '/work/current', 'host': 'local', 'workspace_id': None,
                  'requested_workspace_id': None}, 'repo_modes': {},
    }
    remote = {
        'mac': {'hosts': [{'host': 'mac', 'uuid': 'mac-uuid', 'stale': False,
                          'resources': {'device_claims': [], 'emu_limit': 3,
                                        'devices_observed': {'adb': None, 'emulator_processes': [],
                                                            'ios_simulators_booted': [
                                                                {'udid': SIMULATOR, 'name': 'iPhone SE'}],
                                                            'ios_devices': None, 'error': None}}}],
                'agents': []},
        'old-peer': {'hosts': [{'host': 'old-peer', 'uuid': 'old-uuid', 'stale': False}], 'agents': []},
    }
    return local, remote


def apply_device_fixture(app, now=NOW):
    """Populate an existing isolated app without opening stores, probes, or jobs."""
    app.local, app.remote = device_snapshots(now)
    app.repo_id = '/work/current'
    app.settings_context = dict(app.local['scope'])
    app.remote_errors = {}
    app.remote_due = {host: float('inf') for host in app.remote}
    app.next_bell = float('inf')
    app.focus = app.preview = 'devices'
    return app


class MailboxDevicesTest(unittest.TestCase):
    def setUp(self):
        scope = device_snapshots()[0]['scope']
        self.store_patch = patch('ham.ui.mailbox.LedgerStore', return_value=Mock(host='local'))
        self.scope_patch = patch('ham.ui.mailbox.settings_scope', return_value=scope)
        self.cursor_patch = patch('ham.ui.mailbox.curses.curs_set')
        for patcher in (self.store_patch, self.scope_patch, self.cursor_patch):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.app = apply_device_fixture(MailboxApp(
            mailbox_tests.Screen(), {REPO_ENV: '/work/current', 'NO_COLOR': ''},
            job_factory=mailbox_tests.PendingJob, clock=lambda: 0, wall_clock=lambda: NOW))
        self.app.initialize_colors()

    def capture(self, height=40, width=160):
        self.app.screen = mailbox_tests.Screen(height, width)
        self.app.render()
        self.assertEqual(self.app.screen.out_of_bounds, [])
        self.assertTrue(all(call.column + display_width(call.requested_text) <= width
                            for call in self.app.screen.draw_calls))
        return self.app.screen.snapshot()

    def select(self, device_id):
        self.app.selections['devices'] = next(index for index, device in enumerate(self.app.devices())
                                              if device['device_id'] == device_id)
        self.app.refresh_detail()
        return self.app.detail

    def test_all_host_union_retains_claims_and_holders_from_other_repositories(self):
        devices = self.app.devices()
        self.assertEqual({device['host'] for device in devices}, {'local', 'mac'})
        self.assertEqual(len(devices), 7)
        held = next(device for device in devices if device['device_id'] == 'fold-serial-full-identity')
        self.assertEqual(held['holder']['repo_id'], '/work/other-repository')
        self.assertEqual(held['holder']['pane_id'], 'workspace:pane-holder')
        identities = [self.app.identity(device) for device in devices]
        self.app.repo_id = '/work/unrelated'
        self.assertEqual([self.app.identity(device) for device in self.app.devices()], identities)

    def test_wide_table_has_requested_columns_host_counts_and_findings(self):
        snapshot = self.capture(height=50)
        text = '\n'.join(snapshot.lines())
        self.assertIn('Agents │ Threads │ Devices', text)
        header = next(line for line in snapshot.lines() if 'PLATFORM' in line)
        labels = ('PLATFORM', 'DEVICE', 'HOLDER', 'AGE', 'STATE', '!')
        self.assertEqual([header.index(label) for label in labels],
                         sorted(header.index(label) for label in labels))
        self.assertIn('local · emu 2/3', text)
        for finding in ('unclaimed 1', 'missing 1', 'stale 1'):
            self.assertIn(finding, text)
        self.assertIn('held/attached', text)
        self.assertIn('held/running', text)
        self.assertIn('iPhone SE', text)
        self.assertIn('Device inventory unknown.', text)
        self.assertIn('old-peer · emu ?/?', text)
        self.assertEqual({value for _, target, value in self.app.hit_map if target == 'region'},
                         {'devices', 'detail'})

    def test_age_cells_show_relative_units_and_detail_keeps_full_timestamp(self):
        claim = next(claim for claim in self.app.local['resources']['device_claims']
                     if claim['device_id'] == 'fold-serial-full-identity')
        for elapsed, expected in ((5, '5s'), (120, '2m'), (10800, '3h'), (172800, '2d')):
            with self.subTest(elapsed=elapsed):
                claim['claimed_at'] = NOW - elapsed
                self.select(claim['device_id'])
                snapshot = self.capture()
                header = next(line for line in snapshot.lines() if 'PLATFORM' in line)
                age_column = display_width(header[:header.index('AGE')])
                row = next(rect[0] for rect, target, value in self.app.hit_map
                           if target == 'list-row' and value == self.app.selections['devices'])
                age = ''.join(cell.text for cell in snapshot.cells[row][age_column:age_column + 5]).strip()
                self.assertEqual(age, expected)
                detail = '\n'.join(self.app.detail_lines())
                self.assertIn(f'Age: {expected}\n', detail)
                self.assertIn(f'Claimed at: {NOW - elapsed} (', detail)
                self.assertIn('UTC)', detail)
        self.app.detail_age_deadlines = {}
        self.assertEqual(self.app.format_time(NOW - 120, NOW, relative=True), '2m')
        self.assertEqual(self.app.detail_age_deadlines['2m'], NOW + 60)

    def test_nonpositive_emulator_limit_is_displayed_as_unlimited(self):
        for limit in (0, -1):
            with self.subTest(limit=limit):
                self.app.local['resources']['emu_limit'] = limit
                text = '\n'.join(self.capture().lines())
                self.assertIn('local · emu 2/∞', text)
                self.assertNotIn(f'emu 2/{limit}', text)

    def test_device_identity_ignores_pid_for_ids_and_normalizes_only_ios_case(self):
        android = {'type': 'device', 'host': 'local', 'platform': 'android-emulator',
                   'device_id': 'emulator-5554', 'process_pid': 1001}
        self.assertEqual(self.app.identity(android), self.app.identity({**android, 'process_pid': None}))
        ios = {**android, 'platform': 'ios-simulator', 'device_id': SIMULATOR}
        self.assertEqual(self.app.identity(ios),
                         self.app.identity({**ios, 'device_id': SIMULATOR.lower(), 'process_pid': None}))
        phone = {**android, 'platform': 'android-device', 'device_id': 'SERIAL-ABC'}
        self.assertNotEqual(self.app.identity(phone),
                            self.app.identity({**phone, 'device_id': 'serial-abc'}))
        process = {**android, 'device_id': None, 'platform': 'unknown'}
        self.assertEqual(self.app.identity(process), ('device', 'local', 'process', 1001))

    def test_viewer_survives_process_only_emulator_becoming_adb_attached(self):
        resources = self.app.local['resources']
        resources['device_claims'] = [claim for claim in resources['device_claims']
                                      if claim['device_id'] != 'emulator-5554']
        resources['devices_observed']['adb'] = [
            observation for observation in resources['devices_observed']['adb']
            if observation['serial'] != 'emulator-5554']
        self.select('emulator-5554')
        self.app.key(' ')
        identity = self.app.identity(self.app.detail)
        updated = deepcopy(self.app.local)
        updated['resources']['devices_observed']['adb'].append(
            {'serial': 'emulator-5554', 'state': 'device'})
        self.app.start_job('snapshot', 'snapshot')
        self.app.jobs['snapshot'].result = updated
        self.app.tick()
        self.assertEqual(self.app.viewer, ('devices', identity))
        self.assertEqual(self.app.mode, 'viewer')
        self.assertEqual(self.app.detail['observed_state'], 'attached')

    def test_viewer_survives_ios_identifier_case_change(self):
        device = next(device for device in self.app.devices() if device['platform'] == 'ios-simulator')
        self.select(device['device_id'])
        self.app.key(' ')
        identity = self.app.identity(self.app.detail)
        updated = deepcopy(self.app.remote['mac'])
        updated['hosts'][0]['resources']['devices_observed']['ios_simulators_booted'][0]['udid'] = SIMULATOR.lower()
        self.app.start_job('remote:mac', 'remote', host='mac')
        self.app.jobs['remote:mac'].result = updated
        self.app.tick()
        self.assertEqual(self.app.viewer, ('devices', identity))
        self.assertEqual(self.app.mode, 'viewer')

    def test_devices_shortcut_hint_does_not_displace_agents_main_hint(self):
        self.app.focus = self.app.preview = 'agents'
        text = '\n'.join(self.capture(height=40, width=120).lines())
        self.assertIn('[m] Main', text)
        self.assertIn('[Enter] Send', text)
        self.assertIn('[F1] Help', text)

    def test_narrow_hangul_rows_and_tabs_stay_within_display_cells(self):
        for width in (32, 40, 60, 76, 77, 100):
            with self.subTest(width=width):
                self.select('fold-serial-full-identity')
                snapshot = self.capture(height=24, width=width)
                text = '\n'.join(snapshot.lines())
                self.assertIn('Agents │ Threads │ Devices', text)
                self.assertIn('PLAT', text)
                header = next(line for line in snapshot.lines() if 'PLAT' in line)
                self.assertEqual('AGE' in header, width >= 79)  # Box/prefix cells precede the table.
                self.assertTrue(all(display_width(line) <= width for line in snapshot.lines()))
        self.assertIn('한글', '\n'.join(self.capture(width=100).lines()))

    def test_no_color_keeps_holder_marker_selection_and_finding_distinct(self):
        with patch('ham.ui.mailbox.curses.color_pair', side_effect=AssertionError('NO_COLOR')) as color:
            snapshot = self.capture(height=50)
        color.assert_not_called()
        self.assertFalse(self.app.theme.colors_enabled)
        marker = alias_identity('purple-owl').marker
        self.assertIn(marker + ' purple-owl', '\n'.join(snapshot.lines()))
        selected = next(value for _, target, value in self.app.hit_map if target == 'list-row'
                        and value == self.app.selections['devices'])
        row = next(rect[0] for rect, target, value in self.app.hit_map
                   if target == 'list-row' and value == selected)
        self.assertIn('›', snapshot.line(row))
        self.assertTrue(any(cell.attribute & curses.A_BOLD for cell in snapshot.cells[row]))
        self.assertTrue(any(call.requested_text == '!' and call.attribute & curses.A_BOLD
                            for call in self.app.screen.draw_calls))

    def test_tab_cycle_and_direct_key_preserve_independent_selection(self):
        for forward, backward in (('\t', SHIFT_TAB), (curses.KEY_RIGHT, curses.KEY_LEFT), ('l', 'h')):
            with self.subTest(forward=forward):
                self.app.focus = self.app.preview = 'agents'
                self.app.selections['devices'] = 2
                for region in ('threads', 'devices', 'detail', 'agents'):
                    self.app.key(forward)
                    self.assertEqual(self.app.focus, region)
                for region in ('detail', 'devices', 'threads', 'agents'):
                    self.app.key(backward)
                    self.assertEqual(self.app.focus, region)
                self.app.key('d')
                self.assertEqual((self.app.focus, self.app.preview), ('devices', 'devices'))
                self.assertEqual(self.app.selections['devices'], 2)
                self.assertEqual(self.app.jobs, {})

    def test_wide_agents_panel_exposes_devices_tab_without_removing_threads(self):
        self.app.focus = self.app.preview = 'agents'
        self.capture(height=50, width=240)
        self.assertEqual(self.app.layout, 'stacked')
        self.assertEqual({value for _, target, value in self.app.hit_map if target == 'region'},
                         {'agents', 'threads', 'detail'})
        rect = next(rect for rect, target, value in self.app.hit_map
                    if target == 'list-tab' and value == 'devices')
        self.app.key(MouseEvent(rect[0], rect[1], 'press'))
        self.assertEqual((self.app.mode, self.app.focus), ('nav', 'devices'))
        self.assertIsNone(self.app.draft_key)
        self.capture(height=50, width=240)
        self.assertEqual({value for _, target, value in self.app.hit_map if target == 'region'},
                         {'devices', 'detail'})

    def test_narrow_detail_keeps_devices_tab_and_detail_label_readable(self):
        self.select('fold-serial-full-identity')
        self.app.key('\r')
        snapshot = self.capture(height=24, width=32)
        self.assertIn('Agents│Threads│Devices Detail', snapshot.line(1))
        self.assertEqual({value for _, target, value in self.app.hit_map if target == 'list-tab'},
                         {'agents', 'threads', 'devices'})

    def test_enter_detail_and_space_viewer_show_full_record_without_mutation(self):
        self.select('fold-serial-full-identity')
        self.app.key('\r')
        self.assertEqual((self.app.focus, self.app.preview), ('detail', 'devices'))
        text = '\n'.join(self.app.detail_lines())
        for value in ('fold-serial-full-identity', '/work/other-repository', 'workspace:pane-holder',
                      'holder-terminal-full-id', str(NOW - 120), 'UTC', 'Claim: held', 'Observed: attached'):
            self.assertIn(value, text)
        self.app.key('d')
        before = deepcopy(self.app.local)
        with patch.object(self.app, 'start_job') as start_job, \
             patch.object(self.app, 'choose_agent') as choose_agent, patch.object(self.app, 'reply') as reply:
            self.app.key(' ')
            self.assertEqual(self.app.mode, 'viewer')
            self.assertEqual('\n'.join(self.app.detail_lines()), text)
            for key in ('\r', 'm', 'f', 'w', 'a', 'n', 'd', 't'):
                self.app.key(key)
            self.assertEqual(self.app.mode, 'viewer')
            self.app.key(ESCAPE)
            start_job.assert_not_called()
            choose_agent.assert_not_called()
            reply.assert_not_called()
        self.assertEqual(self.app.local, before)
        self.assertEqual((self.app.mode, self.app.focus), ('nav', 'devices'))
        self.assertEqual(self.app.jobs, {})

    def test_device_click_never_falls_through_to_mail_reply(self):
        self.select('fold-serial-full-identity')
        self.capture()
        row = next(rect for rect, target, value in self.app.hit_map
                   if target == 'list-row' and value == self.app.selections['devices'])
        with patch.object(self.app, 'reply') as reply, patch.object(self.app, 'choose_agent') as choose:
            self.app.key(MouseEvent(row[0], row[1], 'press'))
            reply.assert_not_called()
            choose.assert_not_called()
        self.assertEqual((self.app.focus, self.app.preview), ('detail', 'devices'))

    def test_snapshot_refresh_restores_device_identity_and_closes_vanished_viewer(self):
        self.select('missing-phone-serial')
        self.app.key(' ')
        identity = self.app.identity(self.app.detail)
        self.app.start_job('snapshot', 'snapshot')
        updated = deepcopy(self.app.local)
        updated['resources']['device_claims'].insert(0, {
            'kind': 'phone', 'device_id': 'aaa-new-device', 'label': 'New',
            'holder': updated['agents'][0], 'claimed_at': NOW, 'stale': False})
        self.app.jobs['snapshot'].result = updated
        self.app.tick()
        self.assertEqual(self.app.identity(self.app.detail), identity)
        self.assertEqual(self.app.viewer, ('devices', identity))
        updated = deepcopy(updated)
        updated['resources']['device_claims'] = [claim for claim in updated['resources']['device_claims']
                                                  if claim['device_id'] != 'missing-phone-serial']
        self.app.start_job('snapshot', 'snapshot')
        self.app.jobs['snapshot'].result = updated
        self.app.tick()
        self.assertEqual(self.app.mode, 'nav')
        self.assertIsNone(self.app.viewer)
        self.assertIn('no longer available', self.app.status)

    def test_failed_or_mismatched_remote_snapshot_never_claims_fresh_observations(self):
        self.app.remote_errors['mac'] = 'peer offline'
        host = next(host for host in self.app.device_hosts() if host['host'] == 'mac')
        self.assertFalse(host['fresh'])
        self.assertEqual(host['error'], 'peer offline')
        self.assertIsNone(host['emu_limit']['in_use'])
        self.assertEqual(host['devices'], [])
        self.app.remote['mac']['hosts'][0]['uuid'] = 'unexpected-identity'
        self.assertNotIn(SIMULATOR, {device['device_id'] for device in self.app.devices()})

    def test_local_snapshot_failure_marks_retained_claims_unknown_until_success(self):
        self.app.start_job('snapshot', 'snapshot')
        self.app.jobs['snapshot'].result = {'error': {'code': 'offline', 'message': 'Local snapshot failed'}}
        self.app.tick()
        host = self.app.device_hosts()[0]
        self.assertFalse(host['fresh'])
        self.assertEqual(host['error'], 'Local snapshot failed')
        self.assertTrue(host['devices'])
        self.assertTrue(all(device['observed_state'] == 'unknown' for device in host['devices']))
        self.app.start_job('snapshot', 'snapshot')
        self.app.jobs['snapshot'].result = deepcopy(self.app.local)
        self.app.tick()
        self.assertTrue(self.app.device_hosts()[0]['fresh'])

    def test_process_without_port_has_stable_pid_identity_and_full_detail(self):
        self.app.local['resources']['devices_observed']['emulator_processes'].extend([
            {'pid': 8001, 'port': None}, {'pid': 8002, 'port': None}])
        processes = [device for device in self.app.devices() if device['device_id'] is None]
        self.assertEqual(len({self.app.identity(device) for device in processes}), 2)
        self.app.detail = processes[0]
        self.assertIn('Process PID: 8001', '\n'.join(self.app.detail_lines()))
        self.assertIn('PID 8001', self.app.device_name(processes[0]))
        self.assertNotIn('emulator-None', self.app.device_name(processes[0]))

    def test_navigation_and_render_only_project_existing_snapshots(self):
        with patch('ham.ui.mailbox.subprocess.Popen', side_effect=AssertionError('No key-loop subprocess')), \
             patch('ham.ui.mailbox.subprocess.run', side_effect=AssertionError('No key-loop probe')), \
             patch('ham.ops.resource_scan.scan_host_resources', side_effect=AssertionError('No key-loop scan')):
            for key in ('d', 'j', 'k', '\t', SHIFT_TAB, ' ', ESCAPE):
                self.app.key(key)
                self.capture()
        self.assertEqual(self.app.jobs, {})
        self.app.key('r')
        self.assertEqual(set(self.app.jobs), {'snapshot'})
        self.assertEqual(self.app.jobs['snapshot'].request['operation'], 'snapshot')
        self.assertTrue(self.app.jobs['snapshot'].request['force'])

    def test_bounded_local_snapshot_preserves_board_hosts_and_resource_identity(self):
        local, _ = device_snapshots()
        board = {'hosts': local['hosts'], 'agents': local['agents'], 'workspaces': []}
        settings = Mock()
        settings.show.return_value = {'mode': 'auto'}
        remote = Mock()
        remote.peers.return_value = []
        remote.outbox.return_value = []
        mail = Mock()
        mail.list_threads.return_value = {'threads': []}
        mail.list_direct.return_value = {'submissions': []}
        with patch('ham.ui.mailbox.status_board', return_value=board) as status, \
             patch('ham.ui.mailbox.RepoSettings', return_value=settings), \
             patch('ham.ui.mailbox.Mailbox', return_value=mail), \
             patch('ham.ui.mailbox.RemoteLedger', return_value=remote):
            snapshot = local_snapshot(Mock(host='local'), None)
        self.assertIs(snapshot['hosts'], board['hosts'])
        self.assertIs(snapshot['resources'], local['resources'])
        self.assertIsNone(status.call_args.kwargs['repo_id'])
        self.assertEqual(status.call_args.kwargs['host'], 'local')


if __name__ == '__main__':
    unittest.main()

import copy
import json
from pathlib import Path
import shutil
import socket
import struct
import tempfile
import time
import unittest
from unittest.mock import call, patch

from runtime import appliance as runtime
from pihole_builder.config import DEFAULTS


class SimulatedPowerLoss(BaseException):
    pass


class ContainerInspectionTests(unittest.TestCase):
    def test_ram_logs_mounts_leave_configuration_persistent(self):
        for enabled in (False, True):
            with self.subTest(ram_logs=enabled), tempfile.TemporaryDirectory() as d:
                c = copy.deepcopy(DEFAULTS)
                c['pihole']['ram_logs'] = enabled
                app = runtime.Appliance(c, Path(d))
                generation = {'image': 'test-image', 'data': 'gen-test'}
                with patch.object(runtime, 'command') as command:
                    app.start(generation)
                args = command.call_args.args
                mounts = [args[index + 1] for index, value in enumerate(args) if value == '--tmpfs']
                self.assertEqual([mount.split(':')[0] for mount in mounts],
                                 ['/var/log', '/run', '/tmp', '/var/tmp'] if enabled else [])
                self.assertIn(f'{app.data(generation)}:/etc/pihole:Z', args)
                self.assertEqual(args[-1], 'test-image')

    def test_image_named_pihole_is_not_a_running_container(self):
        def podman(*args, **kwargs):
            if args == ('podman', 'inspect', 'pihole'):
                return json.dumps([{'Id': 'image-only', 'RepoTags': ['docker.io/pihole/pihole:latest']}])
            if args == ('podman', 'container', 'inspect', 'pihole'):
                raise RuntimeError('no such container')
            raise AssertionError(f'Unexpected attempt to operate on a missing container: {args}')

        with tempfile.TemporaryDirectory() as d, patch.object(runtime, 'command', side_effect=podman):
            app = runtime.Appliance({}, Path(d))
            self.assertIsNone(app.inspect())
            app.stop()

    def test_health_checks_use_the_configured_domain_on_both_transports(self):
        c = copy.deepcopy(DEFAULTS)
        c['pihole']['health_domain'] = 'custom.test'
        with tempfile.TemporaryDirectory() as d:
            app = runtime.Appliance(c, Path(d))
            with patch.object(app, 'inspect', return_value={'State': {'Running': True}}), \
                    patch.object(runtime, 'dns_probe', return_value=True) as probe, \
                    patch.object(runtime.urllib.request, 'build_opener') as opener:
                opener.return_value.open.return_value.__enter__.return_value.status = 200
                self.assertTrue(app.healthy())
                self.assertEqual(probe.call_args_list, [call(domain='custom.test'), call(tcp=True, domain='custom.test')])


class FakeAppliance(runtime.Appliance):
    def __init__(self, config, root, ram_root=runtime.RAM_ROOT):
        super().__init__(config, root, ram_root)
        self.events = []
        self.results = iter([True])
        self.running = None
        self.crash = False
        self.migrate = False

    def stop(self):
        self.events.append('stop')
        self.running = None

    def start(self, generation):
        self.events.append(('start', generation['image']))
        self.prepare_data(generation)
        self.running = generation
        if generation['image'] == 'new':
            if self.migrate:
                (self.working_data(generation) / 'database').write_text('migrated-schema')
            if self.crash:
                raise SimulatedPowerLoss()

    def wait_healthy(self):
        return next(self.results)

    def healthy(self):
        return True

    def matches(self, generation):
        return generation == self.running

    def image(self, reference):
        return 'new'


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.c = copy.deepcopy(DEFAULTS)
        self.app = FakeAppliance(self.c, self.root)
        self.old = {'image': 'old', 'data': 'gen-old'}
        self.app.data(self.old).mkdir()
        (self.app.data(self.old) / 'database').write_text('original-schema')
        self.app.state['active'] = self.old
        self.app.running = self.old
        self.app.save()
        self.command = patch.object(runtime, 'command', side_effect=self.fake_command)
        self.command.start()
        self.sync = patch.object(runtime.os, 'sync', create=True)
        self.sync.start()

    def tearDown(self):
        self.command.stop()
        self.sync.stop()
        self.tmp.cleanup()

    def fake_command(self, *args, **kwargs):
        if args[0] == 'cp':
            shutil.copytree(args[-2], args[-1])
        return ''

    def test_success_promotes_new_data_and_keeps_previous(self):
        self.app.migrate = True
        self.app.deploy('new')
        self.assertEqual(self.app.state['active']['image'], 'new')
        self.assertEqual(self.app.state['previous'], self.old)
        self.assertEqual((self.app.data(self.old) / 'database').read_text(), 'original-schema')
        self.assertEqual((self.app.data(self.app.state['active']) / 'database').read_text(), 'migrated-schema')
        self.assertIsNone(json.loads(self.app.state_path.read_text())['pending'])

    def test_repeated_healthy_checks_do_not_write_state(self):
        # First cleanup may normalize bookkeeping; subsequent checks, including
        # a new controller process, must leave the durable state untouched.
        self.app.reconcile()
        with patch.object(runtime, 'atomic_json', wraps=runtime.atomic_json) as write:
            self.app.reconcile()
            rebooted = FakeAppliance(self.c, self.root)
            rebooted.running = self.old
            rebooted.reconcile()
            write.assert_not_called()

    def test_nested_state_change_is_saved_and_failed_write_is_retried(self):
        self.app.state['failed']['bad-image'] = 123
        with patch.object(runtime, 'atomic_json', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.app.save()
        self.app.save()
        self.assertEqual(json.loads(self.app.state_path.read_text())['failed']['bad-image'], 123)

    def test_fresh_deployment_seeds_lists_and_preserves_headers_as_toml(self):
        self.app.state['active'] = None
        self.c['pihole']['adlists'] = ['https://example.org/list1', 'https://example.org/list2']
        headers = ["Content-Security-Policy: default-src 'self'; script-src 'self';", 'X-Frame-Options: DENY']
        self.c['pihole']['web_headers'] = headers
        self.app.deploy('first')
        data = self.app.data(self.app.state['active'])
        self.assertEqual((data / 'adlists.list').read_text().splitlines(), self.c['pihole']['adlists'])
        # JSON strings are valid TOML basic strings; semicolons stay in headers.
        self.assertEqual(json.loads((data / 'pihole.toml').read_text().split(' = ', 1)[1]), headers)
        (data / 'adlists.list').unlink()  # Pi-hole consumes the initial file.
        (data / 'pihole.toml').write_text('modified-in-ui')
        self.app.results = iter([True])
        self.app.deploy('second')
        upgraded = self.app.data(self.app.state['active'])
        self.assertFalse((upgraded / 'adlists.list').exists())
        self.assertEqual((upgraded / 'pihole.toml').read_text(), 'modified-in-ui')

    def test_default_seed_and_explicit_empty_adlists(self):
        with tempfile.TemporaryDirectory() as d:
            directory = Path(d)
            self.app.seed(directory)
            self.assertEqual(list(directory.iterdir()), [])
            self.c['pihole']['adlists'] = []
            self.app.seed(directory)
            self.assertTrue((directory / 'adlists.list').exists())
            self.assertEqual((directory / 'adlists.list').read_text(), '')

    def test_failure_restores_image_and_unmigrated_data(self):
        self.app.results = iter([False, True])
        self.app.migrate = True
        with self.assertRaisesRegex(RuntimeError, 'probation'):
            self.app.deploy('new')
        self.assertEqual(self.app.running, self.old)
        self.assertEqual(self.app.state['active'], self.old)
        self.assertEqual((self.app.data(self.old) / 'database').read_text(), 'original-schema')
        self.assertIsNone(self.app.state['pending'])
        self.assertIn('new', self.app.state['failed'])
        self.assertEqual(list(self.root.glob('gen-*')), [self.root / 'gen-old'])

    def test_power_loss_during_migration_recovers_on_next_boot(self):
        self.app.crash = True
        self.app.migrate = True
        with self.assertRaises(SimulatedPowerLoss):
            self.app.deploy('new')
        rebooted = FakeAppliance(self.c, self.root)
        self.assertEqual(rebooted.state['pending']['image'], 'new')
        rebooted.reconcile()
        self.assertEqual(rebooted.running, self.old)
        self.assertIsNone(rebooted.state['pending'])
        self.assertEqual((rebooted.data(self.old) / 'database').read_text(), 'original-schema')

    def test_backup_failure_restarts_original(self):
        with patch.object(runtime, 'command', side_effect=RuntimeError('disk full')):
            with self.assertRaisesRegex(RuntimeError, 'disk full'):
                self.app.deploy('new')
        self.assertEqual(self.app.running, self.old)
        self.assertIsNone(self.app.state['pending'])

    def test_pull_failure_does_not_stop_working_container(self):
        with patch.object(runtime, 'command', side_effect=RuntimeError('registry offline')):
            with self.assertRaisesRegex(RuntimeError, 'registry offline'):
                self.app.update()
        self.assertEqual(self.app.events, [])
        self.assertEqual(self.app.running, self.old)

    def test_failed_digest_cooldown(self):
        self.app.state['failed']['new'] = time.time()
        self.app.update()
        self.assertEqual(self.app.events, [])
        self.app.state['failed']['new'] = time.time() - 25 * 3600
        self.app.update()
        self.assertEqual(self.app.state['active']['image'], 'new')

    def test_recovery_failure_retains_durable_journal(self):
        self.app.results = iter([False, False])
        with self.assertRaisesRegex(RuntimeError, 'Previous generation'):
            self.app.deploy('new')
        state = json.loads(self.app.state_path.read_text())
        self.assertEqual(state['active'], self.old)
        self.assertEqual(state['pending']['image'], 'new')

    def test_later_regression_rolls_back_once(self):
        self.app.deploy('new')
        self.app.healthy = lambda: False
        self.app.results = iter([False, True])
        self.app.reconcile()
        self.assertEqual(self.app.state['active'], self.old)
        self.assertIsNone(self.app.state['previous'])
        self.assertIn('new', self.app.state['failed'])

    def test_rotation_deletes_only_unreferenced_generations(self):
        self.app.deploy('new')
        self.app.results = iter([True])
        active = self.app.state['active']
        self.app.deploy('newer')
        self.assertEqual(self.app.state['previous'], active)
        self.assertFalse(self.app.data(self.old).exists())
        self.assertTrue(self.app.data(active).exists())


class RamDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'saved'
        self.ram = Path(self.tmp.name) / 'ram'
        self.c = copy.deepcopy(DEFAULTS)
        self.c['pihole']['ram_data'] = True
        self.app = FakeAppliance(self.c, self.root, self.ram)
        self.old = {'image': 'old', 'data': 'gen-old'}
        self.app.data(self.old).mkdir()
        (self.app.data(self.old) / 'database').write_text('original-schema')
        self.app.state['active'] = self.old
        self.app.save()
        def cp(*args, **kwargs):
            if args[0] == 'cp':
                shutil.copytree(args[-2], args[-1])
            return ''
        self.commands = patch.object(runtime, 'command', side_effect=cp)
        self.commands.start()
        self.addCleanup(self.commands.stop)
        self.sync = patch.object(runtime.os, 'sync', create=True)
        self.sync.start()
        self.addCleanup(self.sync.stop)
        self.app.start(self.old)
        (self.app.working_data(self.old) / 'ui-change').write_text('unsaved')

    def test_restarts_reuse_ram_but_host_reboot_restores_checkpoint(self):
        self.app.start(self.old)
        self.assertTrue((self.app.working_data(self.old) / 'ui-change').exists())
        shutil.rmtree(self.ram)
        rebooted = FakeAppliance(self.c, self.root, self.ram)
        rebooted.reconcile()
        self.assertFalse((rebooted.working_data(self.old) / 'ui-change').exists())
        self.assertEqual((rebooted.working_data(self.old) / 'database').read_text(), 'original-schema')

    def test_interrupted_ram_restore_is_replaced_before_start(self):
        shutil.rmtree(self.app.working_data(self.old))
        staging = self.ram / '.restore-gen-old'
        staging.mkdir()
        (staging / 'partial').write_text('incomplete')
        self.app.start(self.old)
        self.assertFalse(staging.exists())
        self.assertFalse((self.app.working_data(self.old) / 'partial').exists())
        self.assertEqual((self.app.working_data(self.old) / 'database').read_text(), 'original-schema')

    def test_upgrade_migrates_ram_without_saving_unsaved_changes(self):
        self.app.migrate = True
        self.app.deploy('new')
        active = self.app.state['active']
        self.assertTrue((self.app.working_data(active) / 'ui-change').exists())
        self.assertFalse((self.app.data(active) / 'ui-change').exists())
        self.assertEqual((self.app.working_data(active) / 'database').read_text(), 'migrated-schema')
        self.assertEqual((self.app.data(active) / 'database').read_text(), 'original-schema')

    def test_explicit_save_creates_durable_checkpoint_and_rotates_previous(self):
        self.app.persist()
        active = self.app.state['active']
        self.assertNotEqual(active['data'], self.old['data'])
        self.assertEqual(self.app.state['previous'], self.old)
        self.assertEqual((self.app.data(active) / 'ui-change').read_text(), 'unsaved')
        shutil.rmtree(self.ram)
        rebooted = FakeAppliance(self.c, self.root, self.ram)
        rebooted.reconcile()
        self.assertEqual((rebooted.working_data(active) / 'ui-change').read_text(), 'unsaved')

    def test_failed_save_preserves_checkpoint_and_restarts_live_data(self):
        with patch.object(runtime, 'command', side_effect=RuntimeError('disk full')):
            with self.assertRaisesRegex(RuntimeError, 'disk full'):
                self.app.persist()
        self.assertEqual(self.app.running, self.old)
        self.assertTrue((self.app.working_data(self.old) / 'ui-change').exists())
        self.assertFalse((self.app.data(self.old) / 'ui-change').exists())

    def test_failed_upgrade_restores_old_ram_data_and_checkpoint(self):
        self.app.migrate = True
        self.app.results = iter([False, True])
        with self.assertRaisesRegex(RuntimeError, 'probation'):
            self.app.deploy('new')
        self.assertEqual(self.app.running, self.old)
        self.assertTrue((self.app.working_data(self.old) / 'ui-change').exists())
        self.assertEqual((self.app.working_data(self.old) / 'database').read_text(), 'original-schema')
        self.assertEqual(list(self.ram.glob('gen-*')), [self.app.working_data(self.old)])

    def test_interrupted_save_does_not_replace_accepted_checkpoint(self):
        self.old['image'] = 'new'
        self.app.save()
        self.app.crash = True
        with self.assertRaises(SimulatedPowerLoss):
            self.app.persist()
        shutil.rmtree(self.ram)
        rebooted = FakeAppliance(self.c, self.root, self.ram)
        rebooted.reconcile()
        self.assertEqual(rebooted.state['active'], self.old)
        self.assertFalse((rebooted.working_data(self.old) / 'ui-change').exists())
        self.assertIsNone(rebooted.state['pending'])

    def test_unhealthy_instance_cannot_be_saved(self):
        self.app.healthy = lambda: False
        with self.assertRaisesRegex(RuntimeError, 'healthy'):
            self.app.persist()
        self.assertEqual(self.app.state['active'], self.old)

    def test_ram_mode_refuses_disk_storage_or_swappable_mount(self):
        for mount, graph in (('ext4 rw', '/run/pihole/containers'),
                             ('tmpfs rw', '/run/pihole/containers'),
                             ('tmpfs rw,noswap', '/var/lib/containers/storage')):
            with self.subTest(mount=mount, graph=graph), patch.object(runtime, 'command', side_effect=[mount, graph]):
                with self.assertRaisesRegex(RuntimeError, 'RAM mode'):
                    self.app.verify_ram()


class DnsTests(unittest.TestCase):
    def packet(self):
        question = b'\x04test\0' + struct.pack('!HH', 1, 1)
        answer = b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, 0, 4) + socket.inet_aton('192.0.2.1')
        return struct.pack('!6H', 42, 0x8180, 1, 1, 0, 0) + question + answer

    def test_parse_compressed_a_response(self):
        self.assertEqual(runtime.dns_answers(self.packet(), 42), ['192.0.2.1'])

    def test_reject_wrong_transaction_truncated_and_servfail(self):
        for packet, ident in ((self.packet(), 43), (self.packet()[:-1], 42),
                              (self.packet()[:3] + b'\x82' + self.packet()[4:], 42), (b'x', 42)):
            with self.subTest(packet=packet), self.assertRaises((ValueError, struct.error)):
                runtime.dns_answers(packet, ident)

    def test_tcp_fragmented_read(self):
        class Sock:
            def __init__(self):
                self.parts = iter([b'ab', b'c', b'd'])
            def recv(self, count):
                return next(self.parts)
        self.assertEqual(runtime.receive(Sock(), 4), b'abcd')

    def test_custom_health_domain_is_sent_over_udp_and_tcp(self):
        class Sock:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def settimeout(self, seconds): pass
            def connect(self, address): self.address = address
            def send(self, packet): self.packet = packet
            def sendall(self, packet): self.packet = packet[2:]
            def response(self):
                ident = struct.unpack('!H', self.packet[:2])[0]
                answer = b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, 0, 4) + socket.inet_aton(runtime.CANARY_IP)
                return struct.pack('!6H', ident, 0x8180, 1, 1, 0, 0) + self.packet[12:] + answer
            def recv(self, size):
                if not hasattr(self, 'buffer'):
                    response = self.response()
                    self.buffer = struct.pack('!H', len(response)) + response if tcp else response
                data, self.buffer = self.buffer[:size], self.buffer[size:]
                return data
        for tcp in (False, True):
            with self.subTest(tcp=tcp):
                sock = Sock()
                with patch.object(runtime.socket, 'socket', return_value=sock):
                    self.assertTrue(runtime.dns_probe(tcp=tcp, domain='custom.test'))
                self.assertEqual(sock.packet[12:-4], b'\x06custom\x04test\0')
                self.assertEqual(sock.address, ('127.0.0.1', 53))

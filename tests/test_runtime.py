import copy
import json
from pathlib import Path
import shutil
import socket
import struct
import tempfile
import time
import unittest
from unittest.mock import patch

from runtime import appliance as runtime
from pihole_builder.config import DEFAULTS


class SimulatedPowerLoss(BaseException):
    pass


class FakeAppliance(runtime.Appliance):
    def __init__(self, config, root):
        super().__init__(config, root)
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
        self.running = generation
        if generation['image'] == 'new':
            if self.migrate:
                (self.data(generation) / 'database').write_text('migrated-schema')
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

"""Opt-in Linux test of actual GPT/MBR, Btrfs resizing and seed assembly.

Run: sudo env PIHOLE_DISK_TESTS=1 python3 -m unittest discover -s tests -p test_disk_integration.py -v
Only loop devices backed by newly created temporary regular files are used.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from pihole_builder.build import assemble, loop_device, mounted, run


@unittest.skipUnless(sys.platform == 'linux' and os.geteuid() == 0 and os.getenv('PIHOLE_DISK_TESTS') == '1', 'requires opt-in Linux root filesystem tests')
class DiskIntegrationTests(unittest.TestCase):
    def exercise(self, label, target="pi4"):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            raw = work / 'fixture.raw'
            with raw.open('wb') as stream:
                stream.truncate(768 * 1024 * 1024)
            kind = '0FC63DAF-8483-4772-8E79-3D69D8477DE4' if label == 'gpt' else '83'
            run('sfdisk', raw, input=f'label: {label}\nstart=2048,size=65536,type={kind}\nstart=67584,size=1400000,type={kind}\n')
            with loop_device(raw) as dev:
                run('mkfs.ext4', '-q', '-L', 'BOOT-TEST', f'{dev}p1')
                run('mkfs.btrfs', '-q', '-f', '-L', 'ROOT', f'{dev}p2')
                with mounted(f'{dev}p2', work / 'fixture-root', 'rw') as root:
                    (root / 'usr/lib').mkdir(parents=True)
                    (root / 'usr/bin').mkdir()
                    (root / 'usr/lib/os-release').write_text('ID="opensuse-microos"\n')
                    # Architecture marker only; this fixture never executes it.
                    elf = (b'\x7fELF\x01\x01' + b'\0' * 12 + b'\x28\x00') if target == 'pi2' else (b'\x7fELF\x02\x01' + b'\0' * 12 + b'\xb7\x00')
                    (root / 'usr/bin/bash').write_bytes(elf)
                    (root / 'preserve-me').write_text('base data')
                old_uuid = run('blkid', '-s', 'UUID', '-o', 'value', f'{dev}p2')
            combustion = work / 'combustion'
            combustion.mkdir()
            (combustion / 'script').write_text('#!/bin/sh\nexit 0\n')
            archive = work / 'bootstrap.tar'
            archive.write_bytes(b'container-fixture')
            assemble(raw, combustion, 2048, work, archive, target)
            table = json.loads(run('sfdisk', '--json', raw))['partitiontable']
            self.assertEqual(len(table['partitions']), 3)
            with loop_device(raw) as dev:
                self.assertEqual(run('blkid', '-s', 'UUID', '-o', 'value', f'{dev}p2'), old_uuid)
                self.assertEqual(run('blkid', '-s', 'LABEL', '-o', 'value', f'{dev}p1'), 'BOOT-TEST')
                with mounted(f'{dev}p2', work / 'verify-root', 'ro') as root:
                    self.assertEqual((root / 'preserve-me').read_text(), 'base data')
                    self.assertGreater(os.statvfs(root).f_blocks * os.statvfs(root).f_frsize, 1024 ** 3)
                with mounted(f'{dev}p3', work / 'verify-seed', 'ro') as seed:
                    self.assertEqual((seed / 'combustion/script').read_text(), '#!/bin/sh\nexit 0\n')
                    self.assertEqual((seed / 'bootstrap.tar').read_bytes(), b'container-fixture')
                    self.assertFalse((seed / 'combustion/bootstrap.tar').exists())

    def test_gpt(self):
        self.exercise('gpt')

    def test_mbr(self):
        self.exercise('dos')

    def test_armv7_mbr(self):
        self.exercise('dos', 'pi2')

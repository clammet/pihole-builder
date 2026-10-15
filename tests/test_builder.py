import copy
import json
from pathlib import Path
import tempfile
import unittest

from pihole_builder.build import checksum_text, layout, partition_script
from pihole_builder.config import DEFAULTS, load, validate
from pihole_builder.provision import render


def config():
    c = copy.deepcopy(DEFAULTS)
    c['admin_password'] = 'unit-test-password'
    return c


class ConfigTests(unittest.TestCase):
    def test_nested_overrides_preserve_defaults(self):
        c = load(overrides=['admin_password="unit-test-password"', 'pihole.web_port=8080', 'updates.os_calendar=null'])
        self.assertEqual(c['pihole']['web_port'], 8080)
        self.assertIsNone(c['updates']['os_calendar'])
        self.assertEqual(c['pihole']['image'], DEFAULTS['pihole']['image'])

    def test_invalid_config_rejected(self):
        changes = [('target', 'pi1'), ('admin_password', 'secret'), ('image_size_mib', True),
                   ('hostname', 'x\nmalicious'), ('timezone', '../../etc/passwd')]
        for key, value in changes:
            with self.subTest(key=key), self.assertRaises(ValueError):
                c = config()
                c[key] = value
                validate(c)
        with self.assertRaises(ValueError):
            load(overrides=['updatse.os_calendar=null'])

    def test_static_address_and_dns_validation(self):
        c = config()
        c['network'].update(address='192.168.1.2/24', gateway='192.168.2.1')
        with self.assertRaises(ValueError):
            validate(c)
        c['network']['gateway'] = '192.168.1.1'
        validate(c)
        c['network']['host_dns'] = ['192.168.1.2']
        with self.assertRaises(ValueError):
            validate(c)

    def test_no_shell_interpolation_of_password(self):
        c = config()
        c['admin_password'] = 'literal-$HOME-$(false)-"quoted"'
        validate(c)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            render(c, root)
            self.assertEqual(json.loads((root / 'payload/config.json').read_text()), c)
            self.assertIn('FTLCONF_webserver_api_password=' + c['admin_password'] + '\n', (root / 'payload/container.env').read_text())
            self.assertNotIn(c['admin_password'], (root / 'script').read_text())
            self.assertEqual((root / 'payload/config.json').stat().st_mode & 0o777, 0o600)

    def test_migration_settings_and_local_records_reach_ftl(self):
        c = config()
        c['pihole'].update(dnssec=True, domain_needed=True, query_logging=False,
                           privacy_level=3, cache_optimizer=30, database_days=91,
                           web_session_timeout=2592000, health_domain='resolver-health.test',
                           local_records=['192.168.1.20 printer.test printer', '2001:db8::1 ipv6.test'])
        validate(c)
        with tempfile.TemporaryDirectory() as d:
            render(c, d)
            env = dict(line.split('=', 1) for line in (Path(d) / 'payload/container.env').read_text().splitlines())
        self.assertEqual(env['FTLCONF_dns_hosts'].split(';'),
                         ['192.0.2.1 resolver-health.test'] + c['pihole']['local_records'])
        self.assertEqual(env['FTLCONF_dns_dnssec'], 'true')
        self.assertEqual(env['FTLCONF_dns_domainNeeded'], 'true')
        self.assertEqual(env['FTLCONF_dns_queryLogging'], 'false')
        self.assertEqual(env['FTLCONF_misc_privacylevel'], '3')
        self.assertEqual(env['FTLCONF_dns_cache_optimizer'], '30')
        self.assertEqual(env['FTLCONF_database_maxDBdays'], '91')
        self.assertEqual(env['FTLCONF_webserver_session_timeout'], '2592000')
        self.assertEqual(env['FTLCONF_dns_listeningMode'], 'ALL')

    def test_reject_malformed_migration_settings_and_canary_conflicts(self):
        changes = [('dnssec', 'true'), ('privacy_level', True), ('cache_optimizer', -2),
                   ('database_days', -1), ('web_session_timeout', 0),
                   ('health_domain', 'public.example'), ('health_domain', 'x..test'),
                   ('local_records', ['192.168.1.2 APPLIANCE-HEALTH.test']),
                   ('local_records', ['192.168.1.2 example.test', '192.168.1.3 Example.test']),
                   ('local_records', ['invalid example.test']),
                   ('local_records', ['192.168.1.2 example.test;malicious.test']),
                   ('web_headers', ['Bad\nHeader: value']),
                   ('adlists', ['file:list.txt']),
                   ('adlists', ['https://user:password@example.org/list']),
                   ('adlists', ['https://example.org/"list']),
                   ('adlists', ['https://example.org:bad/list']),
                   ('adlists', ['https://example.org/list', 'https://example.org/list'])]
        for key, value in changes:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                c = config()
                c['pihole'][key] = value
                validate(c)


class PartitionTests(unittest.TestCase):
    def table(self, label='gpt'):
        return {'label': label, 'sectorsize': 512, 'partitions': [
            {'node': 'image.raw1', 'start': 2048, 'size': 100000, 'type': 'c'},
            {'node': 'image.raw2', 'start': 102400, 'size': 2000000, 'type': '83'}]}

    def test_root_extension_and_seed_do_not_overlap(self):
        number, size, start, seed_size = layout(self.table(), 8192, 512)
        self.assertEqual(number, 2)
        self.assertEqual(size + 102400, start)
        self.assertEqual(start % 2048, 0)
        self.assertLessEqual(start + seed_size, 8192 * 2048 - 2048)

    def test_refuses_shrinking_or_full_mbr(self):
        with self.assertRaises(ValueError):
            layout(self.table(), 512, 512)
        t = self.table('dos')
        t['partitions'][0]['type'] = '5'
        with self.assertRaises(ValueError):
            layout(t, 8192, 512)

    def test_partition_metadata_preserved(self):
        dump = 'label: gpt\nlast-lba: 999999\nimage.raw1 : start=2048, size=100000, type=c, uuid=KEEP\nimage.raw2 : start=102400, size=2000000, type=83, uuid=ROOT\n'
        result = partition_script(dump, self.table(), 8192, 512)
        self.assertIn('image.raw1 : start=2048, size=100000, type=c, uuid=KEEP', result)
        self.assertIn('uuid=ROOT', result)
        self.assertNotIn('last-lba:', result)
        self.assertIn('image.raw3 : start=', result)
        self.assertNotIn('size=2000000', result)

    def test_checksum_parses_gnu_and_bsd(self):
        digest = 'abcd' * 16
        for text in (f'{digest}  image.raw.xz\n', f'SHA256 (image.raw.xz) = {digest.upper()}'):
            self.assertEqual(checksum_text(text), digest)
        with self.assertRaises(ValueError):
            checksum_text('no checksum')
        with self.assertRaises(ValueError):
            checksum_text(digest + '\n' + digest)


class TargetTests(unittest.TestCase):
    def test_pi2_selects_armv7_image_and_container(self):
        from pihole_builder.targets import base_url_for, platform_for, skopeo_platform_args
        c = load(overrides=['admin_password="unit-test-password"', 'target="pi2"'])
        self.assertEqual(platform_for(c['target']), 'linux/arm/v7')
        self.assertIn('openSUSE-MicroOS.armv7l-RaspberryPi2.raw.xz', base_url_for('pi2'))
        self.assertEqual(skopeo_platform_args('pi2'), ['--override-os', 'linux', '--override-arch', 'arm', '--override-variant', 'v7'])
        self.assertEqual(platform_for('pi4'), 'linux/arm64')
        self.assertNotIn('--override-variant', skopeo_platform_args('pi4'))

    def test_elf_architecture_must_match_target(self):
        from pihole_builder.targets import validate_elf
        arm32 = b'\x7fELF\x01\x01' + b'\0' * 12 + b'\x28\x00'
        arm64 = b'\x7fELF\x02\x01' + b'\0' * 12 + b'\xb7\x00'
        validate_elf(arm32, 'pi2')
        validate_elf(arm64, 'pi4')
        for elf, target in ((arm32, 'pi4'), (arm64, 'pi2'), (b'invalid', 'pi2')):
            with self.subTest(target=target), self.assertRaises(ValueError):
                validate_elf(elf, target)

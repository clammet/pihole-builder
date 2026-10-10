# MicroOS Pi-hole SD image builder

Build a flashable Raspberry Pi SD image containing openSUSE MicroOS, a Pi-hole container matching the target architecture, and an unattended Podman update/recovery controller. Configuration comes from JSON and/or command-line overrides. The default follows Pi-hole's `latest` release and updates MicroOS daily.

**Status:** implementation and local unit tests are complete. Full image assembly, first boot, and recovery on physical Raspberry Pi hardware still need validation. Do not treat this repository as a hardware-tested appliance release.

## Hardware scope

| Board | Builder target | Status |
| --- | --- | --- |
| Pi 2B (all revisions, including original / 1.1) | `pi2` | ARMv7 MicroOS RaspberryPi2 base and `linux/arm/v7` container; download availability caveat below |
| Pi 2B revisions 1.2 / 1.3, optional 64-bit build | `pi2-64` | 64-bit CPU; requires an explicitly supplied, board-validated MicroOS base |
| Pi 3 family | `pi3` | Uses upstream Raspberry Pi image; hardware validation pending |
| Pi 4 / 400 | `pi4` / `pi400` | Uses upstream Raspberry Pi image; hardware validation pending |
| Pi 5 | `pi5` | Requires an explicitly supplied, board-validated MicroOS base; not assumed compatible with the generic image |

MicroOS has a 32-bit ARMv7 RaspberryPi2 image: `armv7l` is 32-bit, and the download repository uses the name `armv7hl`. Select `pi2` for this image, including on the original 2B. Other targets select ARM64. These are separate board/architecture builds, not one universal SD image. Compute Modules and other variants require separate boot validation.

**ARMv7 download availability (checked 2026-10-08):** the official MicroOS download page advertises `openSUSE-MicroOS.armv7l-RaspberryPi2.raw.xz`, but its linked image and checksum currently return HTTP 404. The builder selects that official URL for `pi2`; until it works again, supply a trusted ARMv7 MicroOS RaspberryPi2 image using `--base-image` and `--base-sha256`, or a verified alternative `--base-url`. This is a download availability issue, not a 64-bit CPU requirement. ARMv7 first boot and package installation remain unverified. The ARMv7 base is not labeled ContainerHost; provisioning explicitly installs Podman.

Use wired Ethernet, a reliable power supply and a high-endurance **16 GB or larger SD card**. The default image is 12 GiB. Root storage is expanded during the build; the final seed partition deliberately caps it at that size. A larger card's extra space is unused unless you increase `image_size_mib` before building. The builder never writes to an SD card or any user-supplied block device.

## Build

Use a Linux machine or Linux VM (ARM64 or x86-64), Python 3.9+, root access, internet access, and approximately 20–30 GB of free working space. No target-architecture emulation is required: the builder manipulates filesystems without executing ARM binaries.

On Debian/Ubuntu:

```sh
sudo apt-get update
sudo apt-get install -y python3 tzdata skopeo util-linux fdisk udev btrfs-progs e2fsprogs systemd
cp config.example.json config.json
chmod 600 config.json
```

Edit `config.json`: set a real web admin password, your timezone and target. Prefer a router DHCP reservation for the Pi. Optional SSH keys enable emergency maintenance; with no keys, SSH and password login are disabled.

```sh
sudo python3 build.py --config config.json
```

The result is `dist/pihole-microos.img`, with an image checksum and a nonsecret provenance manifest beside it. Existing output images are never overwritten. `config.json`, generated build files, and images are Git-ignored. Images and provisioning files contain credentials: keep them private.

Write the `.img` using Raspberry Pi Imager's **Use custom** option or another raw-image writer. Do not apply Raspberry Pi OS customization settings to this MicroOS image. Connect Ethernet and power it on. First boot needs working internet to install Python and health-checker using signed MicroOS repositories; the Pi-hole container itself is already included. Allow several minutes, then open `http://<pi-address>/admin/` (or the configured web port).

Set your router's client DNS address to the Pi. A public secondary DNS address may be used even while Pi-hole is healthy; it is not a guaranteed failover-only setting and can bypass blocking.

### CLI overrides

Values after `=` are JSON. Overrides apply after the config file and unknown settings are rejected.

```sh
sudo python3 build.py --config config.json \
  --set 'target="pi3"' \
  --set 'network.address="192.168.1.2/24"' \
  --set 'network.gateway="192.168.1.1"' \
  --set 'updates.container_calendar="*-*-* 02:00:00"' \
  --set 'updates.os_calendar="Sun *-*-* 04:00:00"' \
  --output dist/pihole-pi3.img
```

For macOS/Windows, use a Linux VM for the full build. You can validate config and inspect generated provisioning locally without root, downloads or disk operations:

```sh
python3 build.py --config config.json --render-only build/preview
```

A render-only directory is not a flashable image and does not contain the container archive.

### Base image verification and pinning

By default the builder downloads the target-specific official MicroOS Raspberry Pi image and verifies its published SHA-256 over HTTPS. This checks integrity but is **not independent GPG signature verification**. For a pinned build, supply a versioned `--base-url` with `--base-sha256`, or a locally obtained image with a trusted checksum:

```sh
sudo python3 build.py --config config.json \
  --base-image downloads/microos-raspberrypi.raw.xz \
  --base-sha256 '<trusted-64-character-sha256>'
```

For a 32-bit Pi 2 build, set `"target": "pi2"` or add `--set 'target="pi2"'`. The OS binary architecture is checked and the bundled container is selected as `linux/arm/v7`.

The checksum is for the supplied input, compressed if it is `.xz`. If the upstream moving alias changes between checksum and image downloads, the build fails safely; retry or pin a snapshot. Pin `pihole.image` to a digest for repeatable container selection; a pinned digest will intentionally stop following new releases.

## Configuration

`config.example.json` lists all settings. Defaults other than the password can be omitted.

| Setting | Meaning |
| --- | --- |
| `target` | Board selector listed above; defaults to `pi4` |
| `hostname`, `timezone` | Host name and IANA timezone; timezone defaults to UTC |
| `admin_password` | Required Pi-hole web password, at least 12 characters |
| `ssh_authorized_keys` | OpenSSH public keys for root; defaults to none |
| `network.interface` | Wired interface name; defaults to `eth0` |
| `network.address`, `network.gateway` | Static IPv4 CIDR and gateway, or both `null` for DHCP |
| `network.host_dns` | Independent IPv4 DNS servers for OS updates and registry pulls |
| `pihole.image` | Fully qualified registry reference; default `docker.io/pihole/pihole:latest` |
| `pihole.upstream_dns` | Upstream DNS server IP addresses for Pi-hole |
| `pihole.web_port` | Host HTTP port; default 80 |
| `updates.container_calendar` | systemd calendar for container updates; `null` disables them |
| `updates.os_calendar` | systemd calendar for OS updates; `null` disables them |
| `updates.random_delay_seconds` | Maximum schedule jitter, defaults to 600 seconds |
| `updates.health_timeout_seconds` | Per-attempt health probation budget; defaults to 180 seconds |
| `updates.health_successes` | Consecutive successful probes required, five seconds apart; default 3 |
| `updates.retry_failed_after_hours` | Cooldown for a failed image ID; defaults to 24 hours |
| `image_size_mib` | Final raw image size; defaults to 12288 MiB |

Schedules use the configured host timezone unless the calendar includes a timezone. Default container checks start at 03:00 and OS updates at 04:00, each with up to ten minutes of jitter. Timers are persistent: a missed run can execute after the next boot. These are start times, not hard maintenance-window end times. OS updates reboot immediately after successful installation, so allow for download/install time. Container and OS maintenance share a lock.

The current network scope is wired IPv4, DNS and HTTP administration. Wi-Fi, IPv6 client provisioning, DHCP-server mode, TLS termination and NTP serving are not configured. Pi-hole settings supplied through environment variables are authoritative; change the appliance configuration to change them. The reserved local record `appliance-health.test` is used for health probes. Query database retention is limited to seven days, and host journal storage is capped.

## Unattended update and recovery

1. The boot service loads the bundled container and starts Pi-hole under rootful Podman. Podman restarts crashed processes; a five-minute timer checks DNS and HTTP and attempts recovery.
2. The update timer pulls the configured tag while the current Pi-hole remains running. Pull failures leave the working instance alone. Actual image IDs, rather than mutable tags, select running and rollback containers.
3. Before an upgrade, the controller stops Pi-hole to make a consistent copy of its persistent directory, including SQLite databases. Btrfs reflinks make this cheap where supported.
4. A durable journal records a candidate generation before it starts. The candidate uses its own data copy; migrations cannot modify the accepted generation.
5. Probes query a known local A record over both UDP and TCP on the published DNS ports, and require a successful response from the admin web page. Several consecutive successes accept the candidate.
6. Failed candidates restore the previous image **and its pre-upgrade data**. An interrupted update is rolled back on the next boot. Failed image IDs are retried after their cooldown; a different new image is eligible immediately.
7. One previous accepted generation is kept. Later health failures first trigger a restart, then restore that previous generation if available. Obsolete generation directories and tracked container images are cleaned up; no global Podman prune is used.

Health checks deliberately use local DNS data so an upstream internet outage does not cause repeated rollbacks. They establish local DNS and HTTP operation; they do not prove upstream reachability, every filtering rule, or behavior of every LAN client. Rollback loses changes and query history made since the retained data copy.

MicroOS updates use `transactional-update` and the distribution's `health-checker`, extended to check Pi-hole after startup. OS snapshots and container/data generations are separate. Upstream health-checker can request OS rollback when a boot reaches its checks; this does **not** guarantee recovery from a broken bootloader, kernel that never reaches userspace, failing SD card, disk exhaustion or permanently broken upstream releases. Fully unattended is the operating goal, not a promise that recovery can never require intervention.

The large container archive is stored outside the Combustion directory to avoid copying it into initramfs RAM on small boards. First boot copies it to persistent storage, then removes seed configuration and the original archive after boot completion. That deletion is not secure erasure; the original `.img` also still contains its configuration.

## Operations and validation

When SSH is configured, useful commands on the Pi include:

```sh
systemctl list-timers 'pihole-*'
journalctl -u pihole-appliance -u pihole-update -u pihole-os-update
podman logs pihole
python3 /etc/pihole-appliance/appliance.py status
systemctl start pihole-update.service
```

Run local tests:

```sh
python3 -m unittest discover -s tests -v
```

Tests cover configuration parsing, ARMv7/ARM64 selection and architecture mismatch rejection, partition arithmetic, DNS response parsing, healthy upgrades, migrated-data rollback, pull/backup failures, cooldown, power loss and generation cleanup. Linux-only integration tests build temporary GPT and MBR disk fixtures, grow real Btrfs filesystems, and verify preserved data and seed placement:

```sh
sudo env PIHOLE_DISK_TESTS=1 python3 -m unittest discover -s tests -p test_disk_integration.py -v
```

CI runs those filesystem tests and ShellCheck. No test result here substitutes for this remaining release validation on each intended Pi model:

- Build the official image, flash a disposable card and verify unattended first boot, DNS from another LAN machine, HTTP login and the configured address.
- Confirm reboot persistence and a healthy container update.
- Use a deliberately unhealthy test image and a data-migration test image; verify both image ID and pre-update database restoration.
- Cut power during candidate probation, then verify recovery and absence of a promotion of the unverified candidate.
- Exercise a transactional OS update and reboot with serial-console access; verify the health-checker/bootloader rollback path.
- Check sustained storage use and operation through temporary registry, DNS and network outages.

## Upstream references

- [MicroOS download page](https://get.opensuse.org/microos/) and [official ARM64 appliance images](https://download.opensuse.org/ports/aarch64/tumbleweed/appliances/); the ARMv7 download advertised on that page uses the [ARMv7 appliance directory](https://download.opensuse.org/ports/armv7hl/tumbleweed/appliances/).
- [Combustion first-boot configuration](https://github.com/openSUSE/combustion).
- [Pi-hole container configuration](https://docs.pi-hole.net/docker/) and [FTL configuration](https://docs.pi-hole.net/ftldns/configfile/).
- [MicroOS health-checker behavior and limits](https://github.com/openSUSE/health-checker).
- [Transactional update manual](https://manpages.opensuse.org/Tumbleweed/transactional-update/transactional-update.8.en.html).
- [Raspberry Pi 2 processor revisions](https://www.raspberrypi.com/products/raspberry-pi-2-model-b/).

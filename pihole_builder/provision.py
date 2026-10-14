"""Render a Combustion config drive, with no secrets in shell arguments."""
import base64
import json
from pathlib import Path
import shlex
import shutil

RUNTIME = Path(__file__).resolve().parents[1] / "runtime" / "appliance.py"


def write(root, name, content, mode=0o600):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(mode)


def unit(description, action):
    return f"""[Unit]
Description={description}
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /etc/pihole-appliance/appliance.py {action}
TimeoutStartSec=2h
UMask=0077

[Install]
WantedBy=multi-user.target
"""


def timer(service, calendar, delay):
    return f"""[Unit]
Description=Scheduled {service}

[Timer]
OnCalendar={calendar}
RandomizedDelaySec={delay}
Persistent=true
Unit={service}.service

[Install]
WantedBy=timers.target
"""


def render(config, destination):
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    payload = root / "payload"
    n = config["network"]
    network = f"""[connection]
id=pihole-wired
type=ethernet
interface-name={n['interface']}
autoconnect=true
autoconnect-priority=100

[ipv4]
method={'manual' if n['address'] else 'auto'}
ignore-auto-dns=true
dns={';'.join(n['host_dns'])};
dns-priority=-100
"""
    if n["address"]:
        network += f"address1={n['address']},{n['gateway']}\n"
    network += "\n[ipv6]\nmethod=disabled\n"
    write(payload, "network.nmconnection", network)
    write(payload, "config.json", json.dumps(config, indent=2) + "\n")
    p = config["pihole"]
    # Podman's env-file parser preserves literal values; no shell expansion.
    environment = {
        "TZ": config["timezone"], "FTLCONF_webserver_api_password": config["admin_password"],
        "FTLCONF_dns_listeningMode": "ALL", "FTLCONF_dns_upstreams": ";".join(p["upstream_dns"]),
        "FTLCONF_dns_hosts": "192.0.2.1 appliance-health.test",
        "FTLCONF_webserver_port": "80", "FTLCONF_ntp_ipv4_active": "false",
        "FTLCONF_ntp_ipv6_active": "false", "FTLCONF_ntp_sync_active": "false",
        "FTLCONF_database_maxDBdays": "7",
    }
    write(payload, "container.env", "".join(f"{k}={v}\n" for k, v in environment.items()))
    shutil.copyfile(RUNTIME, payload / "appliance.py")
    write(payload, "authorized_keys", "\n".join(config["ssh_authorized_keys"]) + "\n")
    units = payload / "units"
    write(units, "pihole-appliance.service", unit("Pi-hole boot and periodic recovery", "reconcile"), 0o644)
    write(units, "pihole-update.service", unit("Update Pi-hole with data rollback", "update"), 0o644)
    write(units, "pihole-appliance.timer", """[Unit]
Description=Check Pi-hole every five minutes
[Timer]
OnBootSec=5min
OnUnitInactiveSec=5min
Unit=pihole-appliance.service
[Install]
WantedBy=timers.target
""", 0o644)
    enabled = ["pihole-appliance.service", "pihole-appliance.timer"]
    u = config["updates"]
    if u["container_calendar"]:
        write(units, "pihole-update.timer", timer("pihole-update", u["container_calendar"], u["random_delay_seconds"]), 0o644)
        enabled.append("pihole-update.timer")
    if u["os_calendar"]:
        write(units, "pihole-os-update.service", """[Unit]
Description=Transactional MicroOS update and reboot
Wants=network-online.target
After=network-online.target
[Service]
Type=oneshot
Environment=REBOOT_METHOD=systemd
ExecStart=/usr/bin/flock --wait 7200 /run/pihole-appliance.lock /usr/sbin/transactional-update --non-interactive --drop-if-no-change cleanup dup reboot
TimeoutStartSec=4h
""", 0o644)
        write(units, "pihole-os-update.timer", timer("pihole-os-update", u["os_calendar"], u["random_delay_seconds"]), 0o644)
        enabled.append("pihole-os-update.timer")
    encoded_nm = base64.b64encode(network.encode()).decode()
    script = f"""#!/bin/bash
# combustion: network prepare
set -euo pipefail
umask 077
configure_network() {{
    mkdir -p /etc/NetworkManager/system-connections
    printf '%s' '{encoded_nm}' | base64 -d > /etc/NetworkManager/system-connections/pihole.nmconnection
    chmod 600 /etc/NetworkManager/system-connections/pihole.nmconnection
}}
configure_network
if [ "${{1-}}" = --prepare ]; then exit 0; fi

# All target architecture commands execute here, on the Pi's first boot.
zypper --non-interactive install --no-recommends podman python3 health-checker curl
# transactional-update does not mount persistent subvolumes in its new root.
# Mount them before writing the archive and health-checker plugin, otherwise
# those files are hidden by the real subvolume mounts after switch-root.
mountpoint -q /var || mount /var
mountpoint -q /usr/local || mount /usr/local
install -d -m 700 /etc/pihole-appliance /var/lib/pihole-appliance
install -m 600 payload/config.json payload/container.env /etc/pihole-appliance/
install -m 644 payload/appliance.py /etc/pihole-appliance/
# Keep the large container archive outside the Combustion directory: dracut
# copies that directory into RAM, which is limited on 1 GiB boards.
mkdir -p /run/pihole-bootstrap
mount -o ro,nosuid,nodev,noexec /dev/disk/by-label/combustion /run/pihole-bootstrap
trap 'umount /run/pihole-bootstrap' EXIT
install -m 600 /run/pihole-bootstrap/bootstrap.tar /var/lib/pihole-appliance/bootstrap.tar
umount /run/pihole-bootstrap
trap - EXIT
install -m 644 payload/units/* /etc/systemd/system/
printf '%s\\n' {shlex.quote(config['hostname'])} > /etc/hostname
ln -snf {shlex.quote('/usr/share/zoneinfo/' + config['timezone'])} /etc/localtime

# Keep host resolution independent of Pi-hole and release its DNS ports.
mkdir -p /etc/systemd/resolved.conf.d
printf '[Resolve]\\nDNSStubListener=no\\n' > /etc/systemd/resolved.conf.d/pihole.conf
mkdir -p /etc/NetworkManager/conf.d
printf '[main]\\ndns=default\\n' > /etc/NetworkManager/conf.d/pihole.conf
ln -snf /run/NetworkManager/resolv.conf /etc/resolv.conf

# Avoid competing update managers and uncoordinated container tag updates.
systemctl mask transactional-update.timer podman-auto-update.timer
printf 'REBOOT_METHOD=systemd\\nUPDATE_METHOD=dup\\n' > /etc/transactional-update.conf
# The root filesystem was sized by the builder; seed is the final partition.
systemctl mask kiwi-repart.service
systemctl enable NetworkManager.service NetworkManager-wait-online.service
systemctl enable health-checker.service {' '.join(enabled)}

# Only allow SSH public keys; no default password or password SSH access.
passwd -l root
mkdir -p /etc/ssh/sshd_config.d
printf 'PasswordAuthentication no\\nKbdInteractiveAuthentication no\\nPermitRootLogin prohibit-password\\n' > /etc/ssh/sshd_config.d/pihole.conf
"""
    if config["ssh_authorized_keys"]:
        script += "install -d -m 700 /root/.ssh\ninstall -m 600 payload/authorized_keys /root/.ssh/authorized_keys\nsystemctl enable sshd.service\n"
    else:
        script += "systemctl disable sshd.service\n"
    script += f"""
# NetworkManager assigns this connection to the default firewall zone.
if command -v firewall-offline-cmd >/dev/null; then
    firewall-offline-cmd --add-service=dns
    firewall-offline-cmd --add-port={p['web_port']}/tcp
"""
    if config["ssh_authorized_keys"]:
        script += "    firewall-offline-cmd --add-service=ssh\n"
    script += "fi\n"
    # /etc is snapshotted; do not put runtime code in /usr/local (/var on MicroOS).
    script += """mkdir -p /etc/systemd/system/health-checker.service.d
cat > /etc/systemd/system/health-checker.service.d/pihole.conf <<'UNIT'
[Unit]
Wants=pihole-appliance.service
After=pihole-appliance.service
[Service]
TimeoutStartSec=45min
UNIT
install -d /usr/local/libexec/health-checker
cat > /usr/local/libexec/health-checker/pihole.sh <<'PLUGIN'
#!/bin/sh
case "$1" in
    check) exec /usr/bin/python3 /etc/pihole-appliance/appliance.py check ;;
    stop) /usr/bin/podman stop --time 30 pihole ;;
esac
PLUGIN
chmod 755 /usr/local/libexec/health-checker/pihole.sh
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\\nSystemMaxUse=100M\\nRuntimeMaxUse=32M\\n' > /etc/systemd/journald.conf.d/pihole.conf
# Remove bootstrap credentials from the seed after first boot has completed.
systemctl enable pihole-seed-cleanup.service
"""
    write(units, "pihole-seed-cleanup.service", """[Unit]
Description=Remove first-boot credentials and container archive from seed
After=first-boot-complete.target pihole-appliance.service
ConditionPathExists=/var/lib/pihole-appliance/state.json
[Service]
Type=oneshot
ExecStart=/bin/bash /etc/pihole-appliance/clean-seed.sh
[Install]
WantedBy=multi-user.target
""", 0o644)
    write(payload, "clean-seed.sh", """#!/bin/bash
set -euo pipefail
mkdir -p /run/pihole-seed
mount -o rw,nosuid,nodev,noexec /dev/disk/by-label/combustion /run/pihole-seed
trap 'umount /run/pihole-seed' EXIT
rm -rf /run/pihole-seed/combustion
rm -f /run/pihole-seed/bootstrap.tar
sync
""", 0o700)
    script += "install -m 700 payload/clean-seed.sh /etc/pihole-appliance/clean-seed.sh\n"
    write(root, "script", script, 0o700)

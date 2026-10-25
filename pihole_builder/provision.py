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
        "FTLCONF_dns_hosts": ";".join([f"192.0.2.1 {p['health_domain']}"] + p["local_records"]),
        "FTLCONF_webserver_port": "80", "FTLCONF_ntp_ipv4_active": "false",
        "FTLCONF_ntp_ipv6_active": "false", "FTLCONF_ntp_sync_active": "false",
        "FTLCONF_database_maxDBdays": str(p["database_days"]),
        "FTLCONF_dns_dnssec": str(p["dnssec"]).lower(),
        "FTLCONF_dns_domainNeeded": str(p["domain_needed"]).lower(),
        "FTLCONF_dns_queryLogging": str(p["query_logging"]).lower(),
        "FTLCONF_dns_cache_optimizer": str(p["cache_optimizer"]),
        "FTLCONF_misc_privacylevel": str(p["privacy_level"]),
        "FTLCONF_webserver_session_timeout": str(p["web_session_timeout"]),
    }
    if p["database_days"] == 0:
        # Also isolate ancillary FTL database writes (network/session data).
        environment.update({"FTLCONF_database_DBimport": "false",
                            "FTLCONF_database_forceDisk": "false",
                            "FTLCONF_files_database": "/dev/shm/pihole-FTL.db",
                            "FTLCONF_webserver_session_restore": "false"})
    if p["ram_logs"] or p["ram_data"]:
        environment["TAIL_FTL_LOG"] = "0"
    write(payload, "container.env", "".join(f"{k}={v}\n" for k, v in environment.items()))
    shutil.copyfile(RUNTIME, payload / "appliance.py")
    write(payload, "authorized_keys", "\n".join(config["ssh_authorized_keys"]) + "\n")
    logging = config["logging"]
    write(payload, "journald.conf", f"""[Journal]
Storage={logging['storage']}
MaxLevelStore={logging['max_level']}
SystemMaxUse=100M
RuntimeMaxUse=16M
ForwardToSyslog=no
ForwardToKMsg=no
ForwardToConsole=no
ForwardToWall=no
""", 0o644)
    units = payload / "units"
    if p["ram_data"]:
        write(payload, "storage.conf", """[storage]
driver = "overlay"
graphroot = "/run/pihole/containers"
runroot = "/run/pihole/runroot"
imagestore = "/var/lib/containers/storage"
""", 0o644)
        write(units, "run-pihole.mount", f"""[Unit]
Description=Pi-hole working data and container storage in RAM

[Mount]
What=tmpfs
Where=/run/pihole
Type=tmpfs
Options=mode=0700,size={p['ram_size_mib']}M,noswap

[Install]
WantedBy=local-fs.target
""", 0o644)
        write(units, "pihole-ram.service", """[Unit]
Description=Prepare SELinux labels for Pi-hole RAM container storage
Requires=run-pihole.mount
After=run-pihole.mount

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/mkdir -p /run/pihole/containers
ExecStart=/usr/sbin/restorecon -RF /run/pihole/containers
""", 0o644)
        for name in ("pihole-appliance", "pihole-update"):
            write(payload, f"ram-dependencies/{name}.service.d/ram.conf", """[Unit]
Requires=pihole-ram.service
After=pihole-ram.service
""", 0o644)
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
    if p["ram_data"]:
        enabled.append("run-pihole.mount")
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
zypper --non-interactive install --no-recommends podman python3 health-checker curl{' policycoreutils-python-utils' if p['ram_data'] else ''}
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
    if p["ram_data"]:
        script += """
# Images stay on the card; container metadata and writable layers live in RAM.
mkdir -p /etc/containers /var/lib/containers/storage
install -m 644 payload/storage.conf /etc/containers/storage.conf
cp -r payload/ram-dependencies/* /etc/systemd/system/
semanage fcontext -a -e /var/lib/containers/storage /run/pihole/containers
"""
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
install -m 644 payload/journald.conf /etc/systemd/journald.conf.d/90-pihole.conf
# Remove bootstrap credentials from the seed after first boot has completed.
systemctl enable pihole-seed-cleanup.service
"""
    if logging["storage"] in ("volatile", "none"):
        script += """
# Prevent independent log daemons from bypassing the journal's RAM-only policy.
systemctl mask rsyslog.service syslog-ng.service syslog.service auditd.service
mkdir -p /etc/systemd/coredump.conf.d
printf '[Coredump]\\nStorage=none\\nProcessSizeMax=0\\n' > /etc/systemd/coredump.conf.d/90-pihole.conf
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

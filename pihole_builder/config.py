"""Strict configuration loading; all rendered shell data is quoted or encoded."""
import copy
import ipaddress
import json
import re
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .targets import TARGETS

DEFAULTS = {
    "target": "pi4", "hostname": "pihole", "timezone": "UTC",
    "admin_password": "", "ssh_authorized_keys": [],
    "network": {"interface": "eth0", "address": None, "gateway": None,
                "host_dns": ["1.1.1.1", "9.9.9.9"]},
    "pihole": {"image": "docker.io/pihole/pihole:latest",
               "upstream_dns": ["1.1.1.1", "9.9.9.9"], "web_port": 80,
               "dnssec": False, "domain_needed": False, "query_logging": True,
               "privacy_level": 0, "cache_optimizer": 3600,
               "database_days": 7, "web_session_timeout": 1800,
               "web_headers": None, "local_records": [], "adlists": None,
               "health_domain": "appliance-health.test"},
    "updates": {"container_calendar": "*-*-* 03:00:00", "os_calendar": "*-*-* 04:00:00",
                "random_delay_seconds": 600, "health_timeout_seconds": 180,
                "health_successes": 3, "retry_failed_after_hours": 24},
    "image_size_mib": 12288,
}


def merge(base, values, prefix=""):
    if not isinstance(values, dict):
        raise ValueError(f"{prefix or 'config'} must be an object")
    for key, value in values.items():
        if key not in base:
            raise ValueError(f"Unknown setting: {prefix}{key}")
        if isinstance(base[key], dict):
            merge(base[key], value, f"{prefix}{key}.")
        else:
            base[key] = value


def line(value, name):
    if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{name} must be a nonempty single-line string")
    return value


def integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}")


def domain(value, name):
    value = line(value, name)
    if len(value) > 253 or not all(re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
                                  for label in value.split(".")):
        raise ValueError(f"{name} must be a DNS hostname without a trailing dot")
    return value.lower()


def validate(c):
    if c["target"] not in TARGETS:
        raise ValueError("Unknown target; choose " + ", ".join(TARGETS))
    if not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", line(c["hostname"], "hostname")):
        raise ValueError("Invalid hostname")
    try:
        ZoneInfo(line(c["timezone"], "timezone"))
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError("Unknown timezone") from error
    line(c["admin_password"], "admin_password")
    if len(c["admin_password"]) < 12 or c["admin_password"] == "replace-with-a-long-password":
        raise ValueError("Set a real admin_password of at least 12 characters")
    if c["admin_password"] != c["admin_password"].strip():
        raise ValueError("admin_password must not have leading/trailing whitespace")
    keys = c["ssh_authorized_keys"]
    if not isinstance(keys, list):
        raise ValueError("ssh_authorized_keys must be an array")
    for key in keys:
        if not re.match(r"(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-\S+) [A-Za-z0-9+/]+=*(?: |$)", line(key, "SSH key")):
            raise ValueError("Expected an OpenSSH public key (no authorized_keys options)")
    n = c["network"]
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,15}", line(n["interface"], "network.interface")):
        raise ValueError("Invalid network.interface")
    if n["address"] is not None:
        address = ipaddress.IPv4Interface(n["address"])
        if not n["gateway"] or ipaddress.IPv4Address(n["gateway"]) not in address.network:
            raise ValueError("Static IPv4 requires a gateway in the same subnet")
    elif n["gateway"] is not None:
        raise ValueError("network.gateway requires network.address")
    for name, addresses in (("host_dns", n["host_dns"]), ("upstream_dns", c["pihole"]["upstream_dns"])):
        if not isinstance(addresses, list) or not addresses:
            raise ValueError(f"{name} must be a nonempty IP address array")
        for value in addresses:
            ip = ipaddress.ip_address(value)
            if name == "host_dns" and ip.version != 4:
                raise ValueError("host_dns must contain IPv4 addresses for the wired IPv4 connection")
            if ip.is_loopback or ip.is_unspecified or (n["address"] and ip == ipaddress.ip_interface(n["address"]).ip):
                raise ValueError(f"{name} must not depend on this Pi-hole instance")
    image = line(c["pihole"]["image"], "pihole.image")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9./_:@-]+", image) or "/" not in image:
        raise ValueError("pihole.image must be a fully qualified container reference")
    if "." not in image.split("/")[0] and ":" not in image.split("/")[0] and image.split("/")[0] != "localhost":
        raise ValueError("Include a registry in pihole.image, e.g. docker.io/pihole/pihole:latest")
    integer(c["pihole"]["web_port"], "web_port", 1, 65535)
    if c["pihole"]["web_port"] in (22, 53):
        raise ValueError("web_port conflicts with SSH/DNS")
    p = c["pihole"]
    for key in ("dnssec", "domain_needed", "query_logging"):
        if type(p[key]) is not bool:
            raise ValueError(f"pihole.{key} must be a boolean")
    integer(p["privacy_level"], "pihole.privacy_level", 0, 3)
    integer(p["cache_optimizer"], "pihole.cache_optimizer", -1, 86400)
    integer(p["database_days"], "pihole.database_days", 0, 36500)
    integer(p["web_session_timeout"], "pihole.web_session_timeout", 1, 31536000)
    health_domain = domain(p["health_domain"], "pihole.health_domain")
    if not health_domain.endswith(".test"):
        raise ValueError("pihole.health_domain must be under the reserved .test domain")
    if not isinstance(p["local_records"], list):
        raise ValueError("pihole.local_records must be an array of IP HOSTNAME [HOSTNAME ...] strings")
    names = {health_domain}
    for record in p["local_records"]:
        parts = line(record, "local record").split()
        if len(parts) < 2:
            raise ValueError("Local records require an IP address and at least one hostname")
        ipaddress.ip_address(parts[0])
        for hostname in parts[1:]:
            hostname = domain(hostname, "local record hostname")
            if hostname in names:
                raise ValueError("Duplicate local hostname or conflict with the health domain")
            names.add(hostname)
    if p["web_headers"] is not None:
        if not isinstance(p["web_headers"], list) or not p["web_headers"]:
            raise ValueError("pihole.web_headers must be null or a nonempty header array")
        for header in p["web_headers"]:
            if not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+: .+", line(header, "web header")):
                raise ValueError("Headers require 'Name: value'")
    if p["adlists"] is not None:
        if not isinstance(p["adlists"], list):
            raise ValueError("pihole.adlists must be null or an array of HTTP(S) URLs")
        seen = set()
        for address in p["adlists"]:
            parsed = urlsplit(line(address, "adlist URL"))
            if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.fragment or any(ch.isspace() for ch in address)
                    or any(ch in address for ch in ('"', "\\", ";"))):
                raise ValueError("Adlists require HTTP(S) URLs without credentials, fragments, quotes, escapes, or semicolons")
            try:
                parsed.port
            except ValueError as error:
                raise ValueError("Invalid adlist URL port") from error
            if address in seen:
                raise ValueError("Duplicate adlist URL")
            seen.add(address)
    u = c["updates"]
    for key in ("container_calendar", "os_calendar"):
        if u[key] is not None:
            calendar = line(u[key], key)
            if "%" in calendar or "\\" in calendar:
                raise ValueError("Calendar specifiers and escapes are not supported")
    integer(u["random_delay_seconds"], "random_delay_seconds", 0, 86400)
    integer(u["health_timeout_seconds"], "health_timeout_seconds", 30, 1800)
    integer(u["health_successes"], "health_successes", 1, 20)
    if u["health_timeout_seconds"] < u["health_successes"] * 5:
        raise ValueError("health_timeout_seconds must allow all health_successes")
    integer(u["retry_failed_after_hours"], "retry_failed_after_hours", 1, 720)
    integer(c["image_size_mib"], "image_size_mib", 8192, 1048576)
    return c


def load(path=None, overrides=()):
    c = copy.deepcopy(DEFAULTS)
    if path:
        merge(c, json.loads(Path(path).read_text()))
    for override in overrides:
        key, sep, value = override.partition("=")
        if not sep:
            raise ValueError("Overrides use --set dotted.key=JSON_VALUE")
        nested = json.loads(value)
        for component in reversed(key.split(".")):
            nested = {component: nested}
        merge(c, nested)
    return validate(c)

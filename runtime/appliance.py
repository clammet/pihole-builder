#!/usr/bin/env python3
"""Crash-recoverable Pi-hole container updates. Uses only the Python standard library."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import time
import urllib.request
import uuid

ROOT = Path("/var/lib/pihole-appliance")
CONFIG = Path("/etc/pihole-appliance/config.json")
NAME = "pihole"
CANARY = "appliance-health.test"
CANARY_IP = "192.0.2.1"


def command(*args, check=True, timeout=900):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if check and result.returncode:
        # Arguments can include paths to secrets, but never secret values.
        raise RuntimeError(f"{args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip() if check else result.returncode


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as stream:
        os.chmod(tmp, 0o600)
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def skip_name(data, offset):
    for _ in range(128):
        if offset >= len(data):
            raise ValueError("Truncated DNS name")
        size = data[offset]
        if size & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                raise ValueError("Truncated DNS pointer")
            return offset + 2
        if size & 0xC0:
            raise ValueError("Invalid DNS label")
        offset += 1
        if not size:
            return offset
        offset += size
    raise ValueError("Invalid DNS name")


def dns_answers(data, ident):
    if len(data) < 12:
        raise ValueError("Truncated DNS header")
    rid, flags, questions, answers, _, _ = struct.unpack("!6H", data[:12])
    if rid != ident or not flags & 0x8000 or flags & 0x020F or questions != 1:
        raise ValueError("Invalid, truncated, or failed DNS response")
    offset = skip_name(data, 12) + 4
    addresses = []
    for _ in range(answers):
        offset = skip_name(data, offset)
        kind, cls, _, size = struct.unpack("!HHIH", data[offset:offset + 10])
        offset += 10
        payload = data[offset:offset + size]
        if len(payload) != size:
            raise ValueError("Truncated DNS answer")
        if kind == 1 and cls == 1 and size == 4:
            addresses.append(socket.inet_ntoa(payload))
        offset += size
    return addresses


def receive(sock, size):
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ValueError("Truncated TCP DNS response")
        data += chunk
    return data


def dns_probe(tcp=False):
    ident = int.from_bytes(os.urandom(2), "big")
    name = b"".join(bytes([len(p)]) + p.encode() for p in CANARY.split(".")) + b"\0"
    packet = struct.pack("!6H", ident, 0x100, 1, 0, 0, 0) + name + struct.pack("!HH", 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM) as sock:
        sock.settimeout(3)
        sock.connect(("127.0.0.1", 53))
        if tcp:
            sock.sendall(struct.pack("!H", len(packet)) + packet)
            data = receive(sock, struct.unpack("!H", receive(sock, 2))[0])
        else:
            sock.send(packet)
            data = sock.recv(4096)
    return CANARY_IP in dns_answers(data, ident)


class Appliance:
    def __init__(self, config, root=ROOT):
        self.config = config
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.state_path = root / "state.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {"active": None, "previous": None, "pending": None, "failed": {}}

    def save(self):
        atomic_json(self.state_path, self.state)

    def data(self, generation):
        # State is root-only, but never allow corrupt state to escape this tree.
        name = generation["data"]
        if not name.startswith("gen-") or "/" in name or ".." in name:
            raise ValueError("Invalid generation path")
        return self.root / name

    def inspect(self):
        try:
            return json.loads(command("podman", "inspect", NAME, timeout=30))[0]
        except (RuntimeError, ValueError, IndexError):
            return None

    def matches(self, generation):
        info = self.inspect()
        return bool(info and info["State"]["Running"] and info["Image"] == generation["image"]
                    and any(m.get("Source") == str(self.data(generation)) and m.get("Destination") == "/etc/pihole" for m in info.get("Mounts", [])))

    def stop(self):
        if self.inspect():
            command("podman", "stop", "--time", "30", NAME, timeout=60)

    def start(self, generation):
        command("podman", "rm", "--force", "--ignore", NAME, timeout=90)
        self.data(generation).mkdir(parents=True, exist_ok=True)
        port = str(self.config["pihole"]["web_port"])
        command("podman", "run", "--detach", "--name", NAME, "--pull=never",
                "--restart=always", "--log-driver=journald", "--health-cmd=none",
                "--env-file", "/etc/pihole-appliance/container.env",
                "--publish", "53:53/udp", "--publish", "53:53/tcp",
                "--publish", f"{port}:80/tcp", "--volume", f"{self.data(generation)}:/etc/pihole:Z",
                generation["image"], timeout=120)

    def healthy(self):
        try:
            info = self.inspect()
            if not info or not info["State"]["Running"]:
                return False
            if not dns_probe() or not dns_probe(tcp=True):
                return False
            url = f"http://127.0.0.1:{self.config['pihole']['web_port']}/admin/"
            # Do not inherit a host's proxy configuration for a loopback check.
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=5) as response:
                return response.status == 200
        except (OSError, ValueError, RuntimeError, struct.error, subprocess.TimeoutExpired):
            return False

    def wait_healthy(self):
        deadline = time.monotonic() + self.config["updates"]["health_timeout_seconds"]
        successes = 0
        while time.monotonic() < deadline:
            successes = successes + 1 if self.healthy() else 0
            if successes >= self.config["updates"]["health_successes"]:
                return True
            time.sleep(5)
        return False

    def image(self, reference):
        return json.loads(command("podman", "image", "inspect", reference, timeout=30))[0]["Id"]

    def bootstrap(self):
        archive = self.root / "bootstrap.tar"
        reference = "localhost/pihole-bootstrap:build"
        if archive.exists():
            command("podman", "load", "--input", str(archive))
        else:
            reference = self.config["pihole"]["image"]
            command("podman", "pull", reference)
        self.deploy(self.image(reference))
        if self.state["active"]:
            archive.unlink(missing_ok=True)
            command("podman", "untag", "localhost/pihole-bootstrap:build", check=False)

    def rollback_pending(self):
        pending = self.state["pending"]
        if not pending:
            return
        self.state["failed"][pending["image"]] = time.time()
        if self.state["active"]:
            self.start(self.state["active"])
            if not self.wait_healthy():
                # Retain the journal until recovery has actually succeeded.
                raise RuntimeError("Previous generation failed health checks; will retry recovery")
        else:
            command("podman", "rm", "--force", "--ignore", NAME)
        self.state["pending"] = None
        self.save()
        self.cleanup()

    def deploy(self, image):
        old = self.state["active"]
        candidate = {"image": image, "data": "gen-" + uuid.uuid4().hex}
        try:
            # Freeze SQLite before copying. The known-good generation is never
            # mounted in the candidate, so database migrations are reversible.
            self.stop()
            if old:
                command("cp", "--archive", "--reflink=auto", str(self.data(old)), str(self.data(candidate)))
            else:
                self.data(candidate).mkdir(mode=0o700)
            os.sync()
            self.state["pending"] = candidate
            self.save()  # durable BEFORE replacing the container
            self.start(candidate)
            if not self.wait_healthy():
                raise RuntimeError("Candidate failed DNS/HTTP probation")
            self.state.update(active=candidate, previous=old, pending=None)
            self.save()
        except Exception:
            if self.state["pending"]:
                self.rollback_pending()
            elif old:
                self.start(old)
            raise
        self.cleanup()
        print("Committed healthy Pi-hole generation", image, flush=True)

    def reconcile(self):
        if self.state["pending"]:
            print("Recovering interrupted or failed update", flush=True)
            self.rollback_pending()
        if not self.state["active"]:
            self.bootstrap()
            return
        active = self.state["active"]
        if self.matches(active) and self.healthy():
            self.cleanup()
            return
        self.start(active)
        if self.wait_healthy():
            return
        previous = self.state["previous"]
        if previous:
            # Journal this rollback too: an interrupted recovery must retry the
            # previous generation rather than promote a broken newer one.
            self.state.update(active=previous, previous=None, pending=active)
            self.save()
            self.rollback_pending()
            return
        raise RuntimeError("Pi-hole unhealthy; no previous generation. Recovery timer will retry.")

    def update(self):
        self.reconcile()
        reference = self.config["pihole"]["image"]
        # A failed pull leaves the running container and data untouched.
        command("podman", "pull", reference)
        image = self.image(reference)
        if image == self.state["active"]["image"]:
            return
        # Record downloaded IDs even if disk-full prevents taking a data copy.
        self.state["images"] = sorted(set(self.state.get("images", [])) | {image})
        self.save()
        failed_at = self.state["failed"].get(image, 0)
        cooldown = self.config["updates"]["retry_failed_after_hours"] * 3600
        if time.time() - failed_at < cooldown:
            print("Candidate is in failure cooldown", flush=True)
            return
        self.deploy(image)

    def cleanup(self):
        retained = [g for g in (self.state["active"], self.state["previous"], self.state["pending"]) if g]
        paths = {self.data(g) for g in retained}
        images = {g["image"] for g in retained}
        for path in self.root.glob("gen-*"):
            if path not in paths and path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
        # Only remove images this appliance has recorded; never global prune.
        tracked = set(self.state.get("images", [])) | set(self.state["failed"])
        for image in tracked - images:
            if command("podman", "image", "exists", image, check=False, timeout=30) == 0:
                if command("podman", "image", "rm", image, check=False, timeout=60) != 0:
                    images.add(image)  # retry failed garbage collection later
        self.state["images"] = sorted(images)
        self.state["failed"] = dict(sorted(self.state["failed"].items(), key=lambda x: x[1])[-32:])
        self.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("reconcile", "update", "check", "status"))
    args = parser.parse_args()
    os.umask(0o077)
    with open("/run/pihole-appliance.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        app = Appliance(json.loads(CONFIG.read_text()))
        if args.action == "status":
            print(json.dumps(app.state, indent=2))
        elif args.action == "check":
            # A first boot without a baseline must not cause OS rollback loops.
            if app.state["active"] and not app.wait_healthy():
                raise SystemExit(1)
        else:
            getattr(app, args.action)()


if __name__ == "__main__":
    main()

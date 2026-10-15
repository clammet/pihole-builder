"""macOS entry point for image assembly using Lima's native VZ backend."""
import argparse
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid

from .build import checksum
from .config import load

REPO = Path(__file__).resolve().parents[1]
PACKAGES = ("python3", "tzdata", "skopeo", "util-linux", "fdisk", "udev",
            "btrfs-progs", "e2fsprogs", "systemd")


def run(*args, capture=False):
    result = subprocess.run([str(arg) for arg in args], check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else None


def guest(vm, *args, capture=False):
    return run("limactl", "shell", vm, *args, capture=capture)


def source_archive(path, config):
    """Copy only builder inputs, never the checkout's images or private files."""
    with tarfile.open(path, "w") as archive:
        for name in ("build.py", "pihole_builder", "runtime", "tests"):
            archive.add(REPO / name, arcname=name,
                        filter=lambda member: None if "__pycache__" in Path(member.name).parts
                        or member.name.endswith((".pyc", ".pyo")) else member)
        if config is not None:
            data = (json.dumps(config) + "\n").encode()
            member = tarfile.TarInfo("config.json")
            member.size = len(data)
            member.mode = 0o600
            archive.addfile(member, io.BytesIO(data))


def publish(staging, output):
    """Verify the transfer and publish without replacing existing files."""
    image = staging / "image.img"
    manifest = json.loads((staging / "image.img.json").read_text())
    digest = checksum(image)
    recorded = (staging / "image.img.sha256").read_text().split()
    if digest != manifest.get("image_sha256") or not recorded or digest != recorded[0]:
        raise ValueError("Transferred image failed SHA-256 verification")
    (staging / "image.img.sha256").write_text(f"{digest}  {output.name}\n")
    published = []
    try:
        # Publish the image last, after its accompanying metadata is ready.
        for suffix in (".img.sha256", ".img.json", ".img"):
            destination = output.with_suffix(suffix)
            os.link(staging / ("image" + suffix), destination)
            published.append(destination)
    except OSError:
        for destination in published:
            destination.unlink()
        raise


@contextmanager
def temporary_vm(args, arch):
    """Only delete a VM that this invocation successfully created."""
    instances = [json.loads(line) for line in run("limactl", "list", "--json", capture=True).splitlines()]
    if any(item["name"] == args.vm_name for item in instances):
        raise ValueError("VM name already exists; choose another --vm-name (existing VMs are never reused or deleted)")
    print(f"Creating temporary Linux VM {args.vm_name}", flush=True)
    run("limactl", "create", "--yes", f"--name={args.vm_name}", "--vm-type=vz",
        f"--arch={arch}", f"--cpus={args.cpus}", f"--memory={args.memory}",
        f"--disk={args.disk}", "--mount-none", "--containerd=none", "template:ubuntu-26.04")
    try:
        run("limactl", "start", "--yes", args.vm_name)
        yield
    finally:
        print(f"Removing temporary VM {args.vm_name} and its disk", flush=True)
        # Forced deletion also works after a failed boot or an interrupted build.
        # The guest disk is disposable and no build output is needed from it now.
        run("limactl", "delete", "--force", "--yes", args.vm_name)


def build(args, config):
    arch = {"arm64": "aarch64", "x86_64": "x86_64"}.get(platform.machine())
    if sys.platform != "darwin" or not arch:
        raise ValueError("This wrapper requires an Apple Silicon or Intel Mac; use build.py on Linux")
    if os.geteuid() == 0:
        raise ValueError("Run this wrapper as your normal macOS user, without sudo")
    if not shutil.which("limactl"):
        raise ValueError("Lima is missing. Install it with: brew install lima")
    args.vm_name = args.vm_name or "pihole-build-" + uuid.uuid4().hex[:12]
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]*", args.vm_name):
        raise ValueError("VM name must start with a letter and contain only letters, numbers, '_' or '-'")
    with temporary_vm(args, arch):
        print("Preparing Linux build dependencies", flush=True)
        guest(args.vm_name, "sudo", "apt-get", "-o", "DPkg::Lock::Timeout=120", "update")
        guest(args.vm_name, "sudo", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get",
              "-o", "DPkg::Lock::Timeout=120", "install", "-y", *PACKAGES)
        guest(args.vm_name, "sudo", "modprobe", "loop")
        workspace = guest(args.vm_name, "python3", "-c",
                          "from pathlib import Path; import tempfile; "
                          "print(tempfile.mkdtemp(prefix='pihole-build-', dir=Path.home()))", capture=True)
        # Use the guest's own disk to avoid shared-mount filesystem limitations.
        with tempfile.TemporaryDirectory(prefix="pihole-input-") as tmp:
            archive = Path(tmp) / "source.tar"
            source_archive(archive, config)
            run("limactl", "copy", archive, f"{args.vm_name}:{workspace}/source.tar")
        guest(args.vm_name, "tar", "-xf", f"{workspace}/source.tar", "-C", workspace)
        if args.check_vm:
            print("Checking real GPT/MBR partitioning and Btrfs resizing in Linux", flush=True)
            run("limactl", "shell", "--workdir", workspace, args.vm_name, "sudo", "env",
                "PIHOLE_DISK_TESTS=1", "python3", "-m", "unittest", "discover", "-s", "tests",
                "-p", "test_disk_integration.py", "-v")
            return
        command = ["sudo", "python3", "build.py", "--config", "config.json",
                   "--output", "dist/image.img"]
        if args.base_image:
            base_name = "base.raw.xz" if args.base_image.suffix == ".xz" else "base.raw"
            run("limactl", "copy", args.base_image.resolve(), f"{args.vm_name}:{workspace}/{base_name}")
            command.extend(("--base-image", base_name))
        if args.base_url:
            command.extend(("--base-url", args.base_url))
        if args.base_sha256:
            command.extend(("--base-sha256", args.base_sha256))
        run("limactl", "shell", "--workdir", workspace, args.vm_name, *command)
        uid = guest(args.vm_name, "id", "-u", capture=True)
        gid = guest(args.vm_name, "id", "-g", capture=True)
        guest(args.vm_name, "sudo", "chown", "-R", f"{uid}:{gid}", f"{workspace}/dist")
        with tempfile.TemporaryDirectory(prefix=".pihole-transfer-", dir=args.output.parent) as tmp:
            staging = Path(tmp)
            for suffix in (".img", ".img.sha256", ".img.json"):
                run("limactl", "copy", f"{args.vm_name}:{workspace}/dist/image{suffix}", staging)
            publish(staging, args.output)
        print(f"Built {args.output}", flush=True)


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=REPO / "config.json")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=JSON")
    parser.add_argument("--output", type=Path, default=REPO / "dist/pihole-microos.img")
    parser.add_argument("--base-image", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--base-sha256")
    parser.add_argument("--vm-name", help="Name for the temporary VM (default: a unique pihole-build name); existing names are refused")
    parser.add_argument("--cpus", type=positive, default=4, help="CPUs for a new VM (default: 4)")
    parser.add_argument("--memory", type=positive, default=4, help="RAM in GiB for a new VM (default: 4)")
    parser.add_argument("--disk", type=positive, default=60, help="Disk in GiB for a new VM (default: 60)")
    parser.add_argument("--check-vm", action="store_true", help="Run Linux disk integration tests instead of building an image; no config needed")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        config = None if args.check_vm else load(args.config, args.set)
        args.output = args.output.absolute()
        if not args.check_vm:
            if args.output.suffix != ".img":
                raise ValueError("Output must end in .img")
            for suffix in (".img", ".img.sha256", ".img.json"):
                path = args.output.with_suffix(suffix)
                if path.exists() or path.is_symlink():
                    raise ValueError(f"Output exists; choose a new filename: {path}")
            if args.base_image and (not args.base_image.is_file() or not args.base_sha256):
                raise ValueError("Local base-image requires a regular file and --base-sha256")
            args.output.parent.mkdir(parents=True, exist_ok=True)
        build(args, config)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Build failed: {error}\n")
    except KeyboardInterrupt:
        parser.exit(130, "Build interrupted\n")

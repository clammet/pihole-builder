"""Board-specific OS images and matching OCI platforms."""
ARM64_BASE_URL = "https://download.opensuse.org/ports/aarch64/tumbleweed/appliances/openSUSE-MicroOS.aarch64-ContainerHost-RaspberryPi.raw.xz"
ARMV7_BASE_URL = "https://download.opensuse.org/ports/armv7hl/tumbleweed/appliances/openSUSE-MicroOS.armv7l-RaspberryPi2.raw.xz"
TARGETS = ("pi2", "pi2-64", "pi3", "pi4", "pi400", "pi5")


def platform_for(target):
    if target not in TARGETS:
        raise ValueError(f"Unknown target: {target}")
    return "linux/arm/v7" if target == "pi2" else "linux/arm64"


def base_url_for(target):
    return ARMV7_BASE_URL if platform_for(target) == "linux/arm/v7" else ARM64_BASE_URL


def skopeo_platform_args(target):
    platform = platform_for(target).split("/")
    args = ["--override-os", platform[0], "--override-arch", platform[1]]
    if len(platform) == 3:
        args += ["--override-variant", platform[2]]
    return args


def validate_elf(elf, target):
    # ELF class (32/64), endianness and e_machine must agree with the container.
    arm32 = platform_for(target) == "linux/arm/v7"
    prefix = b"\x7fELF" + bytes([1 if arm32 else 2, 1])
    machine = b"\x28\x00" if arm32 else b"\xb7\x00"
    if elf[:6] != prefix or elf[18:20] != machine:
        raise ValueError(f"Base binary architecture does not match {target} ({platform_for(target)})")

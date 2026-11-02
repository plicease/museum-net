"""Read-only check that this host can run the development lab."""

from __future__ import annotations

import argparse
import sys
import tomllib
from dataclasses import dataclass

from .common import (
    Host,
    Manifest,
    Paths,
    Tool,
    find_subnet_problems,
    load_config,
    load_manifest,
    load_tools,
    package_satisfied,
)

OK, WARN, FAIL = "OK", "WARN", "FAIL"
SUPPORTED_RELEASE = (26, 4)
MIN_FREE_BYTES = 20 * 1024**3
BOOTSTRAP = "run ./dev/scripts/bootstrap"


@dataclass(frozen=True)
class Check:
    status: str
    name: str
    detail: str
    fix: str = ""


def missing(tier: str) -> tuple[str, str]:
    """Return the status and fix for something absent from the given tier."""
    if tier == "required":
        return FAIL, BOOTSTRAP
    return WARN, f"{BOOTSTRAP} --{tier}"


def check_platform(host: Host) -> list[Check]:
    release = host.os_release()
    name = release.get("PRETTY_NAME", "unknown system")
    version = tuple(int(part) for part in release.get("VERSION_ID", "0").split("."))
    if release.get("ID") != "ubuntu" or version < SUPPORTED_RELEASE:
        os_check = Check(FAIL, "operating system", name, "use Ubuntu 26.04 or later")
    elif version > SUPPORTED_RELEASE:
        os_check = Check(WARN, "operating system", f"{name} is newer than 26.04, untested")
    else:
        os_check = Check(OK, "operating system", name)

    machine = host.machine()
    arch_check = Check(OK if machine == "x86_64" else FAIL, "architecture", machine)
    return [os_check, arch_check]


def check_virtualization(host: Host) -> list[Check]:
    checks = []
    if host.cpu_has_virtualization():
        checks.append(Check(OK, "cpu virtualization", "vmx or svm present"))
    else:
        checks.append(
            Check(
                FAIL,
                "cpu virtualization",
                "no vmx or svm flag",
                "enable virtualization in firmware, or nested virtualization",
            )
        )

    state = host.kvm_state()
    if state == "ok":
        checks.append(Check(OK, "/dev/kvm", "readable and writable"))
    elif state == "denied":
        checks.append(Check(FAIL, "/dev/kvm", "permission denied", BOOTSTRAP))
    else:
        checks.append(
            Check(FAIL, "/dev/kvm", "missing", "enable virtualization and load the kvm module")
        )

    error = host.libvirt_error()
    if error is None:
        checks.append(Check(OK, "libvirt", "qemu:///system reachable"))
    else:
        checks.append(Check(FAIL, "libvirt", error, BOOTSTRAP))
    return checks


def check_groups(host: Host, manifest: Manifest) -> list[Check]:
    checks = []
    for group in manifest.groups:
        name = f"group {group}"
        if host.session_has_group(group):
            checks.append(Check(OK, name, "active in this session"))
        elif host.user_in_group(group):
            checks.append(
                Check(WARN, name, "member, not active yet", "log out and back in")
            )
        else:
            checks.append(Check(FAIL, name, f"{host.username()} is not a member", BOOTSTRAP))
    return checks


def check_packages(host: Host, manifest: Manifest) -> list[Check]:
    checks = []
    for package in manifest.packages:
        name = f"{package.tier} package {package.name}"
        if package_satisfied(host, package):
            checks.append(Check(OK, name, "present"))
            continue
        status, fix = missing(package.tier)
        if host.package_available(package.name):
            checks.append(Check(status, name, package.why, fix))
        else:
            checks.append(
                Check(status, name, "no APT candidate", "run apt update or fix dev/packages.yaml")
            )
    return checks


def check_nix(host: Host) -> list[Check]:
    version = host.nix_version()
    if version is None:
        return [Check(FAIL, "nix", "not installed", BOOTSTRAP)]
    error = host.nix_store_error()
    if error is not None:
        return [Check(FAIL, "nix", f"{version}: store unusable: {error}", BOOTSTRAP)]
    return [Check(OK, "nix", version)]


def check_tools(host: Host, tools: list[Tool], paths: Paths) -> list[Check]:
    checks = []
    for tool in tools:
        name = f"tool {tool.name} {tool.version}"
        digest = host.file_sha256(paths.tool_path(tool))
        if digest is None:
            status, fix = missing(tool.tier)
            checks.append(Check(status, name, tool.why, fix))
        elif digest != tool.sha256:
            checks.append(
                Check(
                    FAIL,
                    name,
                    f"checksum mismatch at {paths.tool_path(tool)}",
                    f"delete that file, then {BOOTSTRAP}",
                )
            )
        else:
            checks.append(Check(OK, name, "checksum verified"))
    return checks


def check_roms(host: Host, paths: Paths, config: dict) -> list[Check]:
    roms = paths.roms_dir(config)
    if host.directory_exists(roms / "machines"):
        return [Check(OK, "86Box ROMs", str(roms))]
    return [
        Check(
            WARN,
            "86Box ROMs",
            f"no ROM set at {roms}",
            "ROMs are user-supplied; install them there or set paths.roms",
        )
    ]


def check_storage(host: Host, paths: Paths) -> list[Check]:
    free = host.free_bytes(paths.data)
    detail = f"{free / 1024**3:.0f} GiB free for {paths.data}"
    if free < MIN_FREE_BYTES:
        return [Check(WARN, "storage", detail, "the lab needs about 20 GiB for VM disks")]
    return [Check(OK, "storage", detail)]


def check_subnets(host: Host, config: dict) -> list[Check]:
    subnets = config.get("subnets", {})
    if not subnets:
        return [Check(OK, "subnets", "none configured, nothing to check")]
    problems = find_subnet_problems(subnets, host.routes())
    if not problems:
        return [Check(OK, "subnets", f"{len(subnets)} configured, no collisions")]
    return [
        Check(FAIL, "subnets", problem, "edit [subnets] in config.toml")
        for problem in problems
    ]


def collect(
    host: Host, manifest: Manifest, tools: list[Tool], paths: Paths
) -> list[Check]:
    checks = check_platform(host)
    checks += check_virtualization(host)
    checks += check_groups(host, manifest)
    checks += check_packages(host, manifest)
    checks += check_nix(host)
    checks += check_tools(host, tools, paths)
    checks += check_storage(host, paths)
    try:
        config = load_config(paths)
    except tomllib.TOMLDecodeError as error:
        checks.append(Check(FAIL, "config", str(error), "fix config.toml"))
        return checks + check_roms(host, paths, {})
    return checks + check_roms(host, paths, config) + check_subnets(host, config)


def report(checks: list[Check]) -> int:
    for check in checks:
        print(f"[{check.status:^4}] {check.name}: {check.detail}")
        if check.fix and check.status != OK:
            print(f"       fix: {check.fix}")
    failures = sum(check.status == FAIL for check in checks)
    warnings = sum(check.status == WARN for check in checks)
    print(f"\n{failures} failed, {warnings} warnings, {len(checks)} checks")
    return 1 if failures else 0


def main() -> int:
    argparse.ArgumentParser(
        prog="doctor",
        description="Check that this host can run the Museum Net lab. Changes nothing.",
        epilog="Exit status: 0 no failures, 1 at least one failure, 2 usage error.",
    ).parse_args()
    return report(collect(Host(), load_manifest(), load_tools(), Paths.from_env()))


if __name__ == "__main__":
    sys.exit(main())

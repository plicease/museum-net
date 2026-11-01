"""Manifests, local paths, and read-only host inspection."""

from __future__ import annotations

import getpass
import grp
import hashlib
import ipaddress
import json
import os
import platform
import pwd
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
TIERS = ("required", "optional", "test", "vintage")


@dataclass(frozen=True)
class Package:
    name: str
    why: str
    tier: str
    command: str | None = None


@dataclass(frozen=True)
class Manifest:
    packages: tuple[Package, ...]
    groups: tuple[str, ...]

    def tier(self, tier: str) -> list[Package]:
        return [p for p in self.packages if p.tier == tier]


@dataclass(frozen=True)
class Tool:
    name: str
    version: str
    url: str
    sha256: str
    filename: str
    tier: str
    why: str


def load_manifest(path: Path = REPO_ROOT / "dev/packages.yaml") -> Manifest:
    data = yaml.safe_load(path.read_text())
    packages = tuple(
        Package(tier=tier, **entry)
        for tier in TIERS
        for entry in data["packages"].get(tier, [])
    )
    return Manifest(packages=packages, groups=tuple(data["groups"]))


def load_tools(path: Path = REPO_ROOT / "dev/tools.lock.yaml") -> list[Tool]:
    data = yaml.safe_load(path.read_text())
    return [Tool(name=name, **entry) for name, entry in data["tools"].items()]


def _xdg(env: dict[str, str], variable: str, fallback: str) -> Path:
    # The XDG spec says an empty value counts as unset.
    value = env.get(variable)
    return Path(value) if value else Path(env["HOME"]) / fallback


@dataclass(frozen=True)
class Paths:
    config: Path
    data: Path
    cache: Path

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Paths:
        env = dict(os.environ) if env is None else env
        return cls(
            config=_xdg(env, "XDG_CONFIG_HOME", ".config") / "museum-net",
            data=_xdg(env, "XDG_DATA_HOME", ".local/share") / "museum-net",
            cache=_xdg(env, "XDG_CACHE_HOME", ".cache") / "museum-net",
        )

    def tool_path(self, tool: Tool) -> Path:
        return self.data / "tools" / tool.name / tool.version / tool.filename

    def download_path(self, tool: Tool) -> Path:
        return self.cache / "downloads" / f"{tool.sha256}-{tool.filename}"

    def roms_dir(self, config: dict) -> Path:
        override = config.get("paths", {}).get("roms")
        return Path(override).expanduser() if override else self.data / "roms"


def load_config(paths: Paths) -> dict:
    """Return the local config, or {} when there is none.

    Raises tomllib.TOMLDecodeError if the file exists but is malformed.
    """
    path = paths.config / "config.toml"
    if not path.exists():
        return {}
    with path.open("rb") as file:
        return tomllib.load(file)


def sha256_file(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def find_subnet_problems(
    subnets: dict[str, str], routes: list[tuple[str, str]]
) -> list[str]:
    """Describe invalid or colliding subnets.

    `subnets` maps lab network names to CIDRs; `routes` holds (CIDR, device)
    pairs from the host.
    """
    problems = []
    parsed = {}
    for name, value in subnets.items():
        try:
            parsed[name] = ipaddress.ip_network(value)
        except (TypeError, ValueError) as error:
            problems.append(f"{name}: invalid subnet {value!r} ({error})")

    names = list(parsed)
    for index, name in enumerate(names):
        network = parsed[name]
        for other in names[index + 1 :]:
            if _overlaps(network, parsed[other]):
                problems.append(f"{name} ({network}) overlaps {other} ({parsed[other]})")
        for route, device in routes:
            host_network = ipaddress.ip_network(route)
            if _overlaps(network, host_network):
                problems.append(
                    f"{name} ({network}) overlaps host route {route} on {device}"
                )
    return problems


def _overlaps(first, second) -> bool:
    return first.version == second.version and first.overlaps(second)


class Host:
    """Read-only view of this machine. Nothing here may change host state."""

    def os_release(self) -> dict[str, str]:
        result = {}
        for line in Path("/etc/os-release").read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator:
                result[key] = value.strip().strip('"')
        return result

    def machine(self) -> str:
        return platform.machine()

    def cpu_has_virtualization(self) -> bool:
        words = Path("/proc/cpuinfo").read_text().split()
        return "vmx" in words or "svm" in words

    def kvm_state(self) -> str:
        """Return "ok", "denied", or "missing"."""
        if not os.path.exists("/dev/kvm"):
            return "missing"
        return "ok" if os.access("/dev/kvm", os.R_OK | os.W_OK) else "denied"

    def package_installed(self, name: str) -> bool:
        result = _run(["dpkg-query", "-W", "-f=${db:Status-Abbrev}", name])
        return result.returncode == 0 and result.stdout.startswith("ii")

    def package_available(self, name: str) -> bool:
        result = _run(["apt-cache", "policy", name])
        for line in result.stdout.splitlines():
            key, _, value = line.strip().partition(": ")
            if key == "Candidate":
                return value != "(none)"
        return False

    def has_command(self, name: str) -> bool:
        return shutil.which(name) is not None

    def username(self) -> str:
        return getpass.getuser()

    def is_root(self) -> bool:
        return os.geteuid() == 0

    def user_in_group(self, group: str) -> bool:
        """Whether /etc/group lists the user, which a new login would pick up."""
        try:
            entry = grp.getgrnam(group)
        except KeyError:
            return False
        primary = pwd.getpwnam(self.username()).pw_gid == entry.gr_gid
        return primary or self.username() in entry.gr_mem

    def session_has_group(self, group: str) -> bool:
        try:
            return grp.getgrnam(group).gr_gid in os.getgroups()
        except KeyError:
            return False

    def libvirt_error(self) -> str | None:
        """Return why the system libvirt is unreachable, or None if it works."""
        if not self.has_command("virsh"):
            return "virsh is not installed"
        result = _run(["virsh", "-c", "qemu:///system", "version"])
        return None if result.returncode == 0 else result.stderr.strip()

    def nix_version(self) -> str | None:
        if not self.has_command("nix"):
            return None
        return _run(["nix", "--version"]).stdout.strip() or None

    def nix_store_error(self) -> str | None:
        """Return why the Nix store is unusable, or None if it works."""
        result = _run(
            ["nix", "--extra-experimental-features", "nix-command", "store", "info"]
        )
        return None if result.returncode == 0 else result.stderr.strip()

    def routes(self) -> list[tuple[str, str]]:
        routes = []
        for family in ("-4", "-6"):
            result = _run(["ip", "-j", family, "route", "show"])
            if result.returncode != 0 or not result.stdout.strip():
                continue
            for route in json.loads(result.stdout):
                if route["dst"] != "default":
                    routes.append((route["dst"], route.get("dev", "?")))
        return routes

    def free_bytes(self, path: Path) -> int:
        while not path.exists():
            path = path.parent
        return shutil.disk_usage(path).free

    def file_sha256(self, path: Path) -> str | None:
        return sha256_file(path) if path.is_file() else None

    def directory_exists(self, path: Path) -> bool:
        return path.is_dir()


def package_satisfied(host: Host, package: Package) -> bool:
    if package.command and host.has_command(package.command):
        return True
    return host.package_installed(package.name)


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "LC_ALL": "C"},
        check=False,
    )

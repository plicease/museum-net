"""Opt-in emulated clients, attached to a lab museum network.

Each directory in dev/machines with a client.yaml is a client. Nothing here
names one, and only the Emulator classes know about a specific emulator.

An emulator reaches the lab through a TAP device that root creates once, owned
by the user, and joins to the lab's bridge. The emulator itself needs no
privileges, which matters because it parses untrusted disk images: 86Box's own
TAP backend would need CAP_NET_ADMIN, and creates a bridge if the name it is
given doesn't exist.
"""

from __future__ import annotations

import argparse
import configparser
import getpass
import ipaddress
import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

from .common import REPO_ROOT, Paths, load_config, load_tools
from .lab import MAX_INTERFACE_NAME, Lab, load_lab
from .libvirt import Virsh

TAP_PREFIX = "mnc-"


class ClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class Asset:
    file: str
    description: str


@dataclass(frozen=True)
class Client:
    name: str
    description: str
    guest: str
    emulator: str
    directory: Path
    config: str
    lab_network: str
    host: int
    card: str
    assets: tuple[Asset, ...]
    capabilities: tuple[str, ...]

    @property
    def tap(self) -> str:
        return TAP_PREFIX + self.name


def load_clients(machines: Path = REPO_ROOT / "dev/machines") -> list[Client]:
    clients = []
    for path in sorted(machines.glob("*/client.yaml")):
        data = yaml.safe_load(path.read_text())
        if data.get("version") != 1:
            raise ClientError(f"{path}: unsupported version {data.get('version')}")
        network = data["network"]
        client = Client(
            name=path.parent.name,
            description=data["description"],
            guest=data["guest"],
            emulator=data["emulator"],
            directory=path.parent,
            config=data["config"],
            lab_network=network["lab_network"],
            host=network["host"],
            card=network["card"],
            assets=tuple(Asset(**asset) for asset in data["assets"]),
            capabilities=tuple(data["capabilities"]),
        )
        if len(client.tap) > MAX_INTERFACE_NAME:
            raise ClientError(f"{path}: {client.tap} is too long for an interface name")
        clients.append(client)
    return clients


class Emulator(Protocol):
    def problems(self, client: Client, paths: Paths, config: dict) -> list[str]:
        """What is missing to run this client."""

    def write_config(self, client: Client, state: Path, socket: Path, paths: Paths) -> None:
        """Create or update the emulator's configuration in `state`."""

    def command(self, client: Client, state: Path, paths: Paths, config: dict) -> list[str]:
        """The command that runs this client."""


class Box86:
    def _tool(self, paths: Paths) -> Path:
        tool = next(tool for tool in load_tools() if tool.name == "86box")
        return paths.tool_path(tool)

    def problems(self, client: Client, paths: Paths, config: dict) -> list[str]:
        problems = []
        if not self._tool(paths).exists():
            problems.append("86Box isn't installed: run ./dev/scripts/bootstrap --vintage")
        roms = paths.roms_dir(config)
        if not (roms / "machines").is_dir():
            problems.append(f"no 86Box ROM set at {roms}: ROMs are user-supplied")
        return problems

    def write_config(self, client: Client, state: Path, socket: Path, paths: Paths) -> None:
        """Start from the tracked file, then keep what the user set in 86Box.
        The network section is always rewritten, so a saved setting can't move
        the machine off the lab."""
        target = state / "86box.cfg"
        parser = configparser.ConfigParser(interpolation=None, delimiters=("=",))
        parser.optionxform = str
        parser.read(target if target.exists() else client.directory / client.config)
        if parser.has_section("Network"):
            parser.remove_section("Network")
        parser["Network"] = {
            "net_01_card": client.card,
            "net_01_net_type": "vde",
            "net_01_host_device": str(socket),
            "net_01_link": "0",
        }
        section = "Hard disks"
        if not parser.has_section(section) and client.assets:
            parser[section] = {"hdd_01_fn": str(state / client.assets[0].file)}
        with target.open("w") as file:
            parser.write(file, space_around_delimiters=True)

    def command(self, client: Client, state: Path, paths: Paths, config: dict) -> list[str]:
        return [str(self._tool(paths)), "-P", str(state), "-R", str(paths.roms_dir(config))]


EMULATORS: dict[str, Emulator] = {"86box": Box86()}


def emulator_for(client: Client) -> Emulator:
    if client.emulator not in EMULATORS:
        raise ClientError(f"{client.name}: no support for emulator {client.emulator}")
    return EMULATORS[client.emulator]


def state_dir(client: Client, paths: Paths) -> Path:
    return paths.data / "clients" / client.name


def missing_assets(client: Client, paths: Paths) -> list[str]:
    state = state_dir(client, paths)
    return [
        f"missing {state / asset.file}: {asset.description}"
        for asset in client.assets
        if not (state / asset.file).exists()
    ]


def lab_bridge(lab: Lab, client: Client, virsh: Virsh) -> str:
    """The client's bridge, only if the lab's own network is running."""
    network = next((n for n in lab.networks if n.key == client.lab_network), None)
    if network is None:
        raise ClientError(f"{client.name}: the lab has no network {client.lab_network}")
    if not virsh.network_active(network.libvirt_name):
        raise ClientError(f"the lab isn't running: {network.libvirt_name} is inactive")
    return network.bridge


def tap_master(tap: str, sys_class_net: Path = Path("/sys/class/net")) -> str | None:
    """The bridge a TAP belongs to, "" if it has none, or None if it's absent."""
    device = sys_class_net / tap
    if not device.exists():
        return None
    master = device / "master"
    return master.resolve().name if master.exists() else ""


def attach_commands(tap: str, bridge: str, user: str) -> list[list[str]]:
    return [
        ["sudo", "ip", "tuntap", "add", "dev", tap, "mode", "tap", "user", user],
        ["sudo", "ip", "link", "set", tap, "master", bridge, "up"],
    ]


def ensure_tap(client: Client, bridge: str, run=subprocess.run) -> None:
    master = tap_master(client.tap)
    if master == bridge:
        return
    if master is not None:
        attached = f"attached to {master}" if master else "not attached to a bridge"
        raise ClientError(f"{client.tap} exists but is {attached}: expected {bridge}")
    for command in attach_commands(client.tap, bridge, getpass.getuser()):
        print(" ".join(command))
        run(command, check=True)


def switch_pid(state: Path) -> int | None:
    try:
        pid = int((state / "vde.pid").read_text())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return None
    return pid


def ensure_switch(client: Client, state: Path, run=subprocess.run) -> Path:
    socket = state / "vde"
    if switch_pid(state) is None:
        run(
            [
                "vde_switch", "--daemon", "--tap", client.tap,
                "--sock", str(socket), "--pidfile", str(state / "vde.pid"),
            ],
            check=True,
        )
    return socket


def assigned_address(client: Client, paths: Paths) -> str | None:
    """The address to configure in the guest, once the lab has its subnets."""
    saved = paths.data / "lab" / "subnets.json"
    if not saved.exists():
        return None
    subnet = json.loads(saved.read_text()).get(client.lab_network)
    return str(ipaddress.ip_network(subnet)[client.host]) if subnet else None


def find_client(clients: list[Client], name: str) -> Client:
    for client in clients:
        if client.name == name:
            return client
    known = ", ".join(client.name for client in clients)
    raise ClientError(f"no client {name!r}: available clients are {known}")


def command_list(clients: list[Client], paths: Paths, config: dict) -> int:
    for client in clients:
        problems = emulator_for(client).problems(client, paths, config)
        problems += missing_assets(client, paths)
        print(f"{client.name}: {client.description}")
        print(f"  emulator {client.emulator}, network {client.lab_network}")
        print(f"  address {assigned_address(client, paths) or 'allocated by the first ./dev/scripts/up'}")
        print(f"  capabilities {', '.join(client.capabilities)}")
        for problem in problems:
            print(f"  {problem}")
    return 0


def command_start(client: Client, lab: Lab, paths: Paths, config: dict) -> int:
    emulator = emulator_for(client)
    state = state_dir(client, paths)
    problems = emulator.problems(client, paths, config) + missing_assets(client, paths)
    if problems:
        raise ClientError("\n".join(problems))
    bridge = lab_bridge(lab, client, Virsh())
    state.mkdir(parents=True, exist_ok=True)
    ensure_tap(client, bridge)
    socket = ensure_switch(client, state)
    emulator.write_config(client, state, socket, paths)
    return subprocess.run(emulator.command(client, state, paths, config), check=False).returncode


def command_stop(client: Client, paths: Paths) -> int:
    state = state_dir(client, paths)
    pid = switch_pid(state)
    if pid is not None:
        os.kill(pid, signal.SIGTERM)
    if tap_master(client.tap) is not None:
        command = ["sudo", "ip", "link", "del", client.tap]
        print(" ".join(command))
        subprocess.run(command, check=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="client",
        description="Run opt-in emulated clients on the lab's museum networks.",
        epilog="Exit status: 0 success, 1 failed, 2 usage error.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="show clients and what each is missing")
    for name, text in (
        ("start", "attach a client to its museum network and run it"),
        ("stop", "detach a client from its museum network"),
    ):
        commands.add_parser(name, help=text).add_argument("name")
    args = parser.parse_args()

    paths = Paths.from_env()
    config = load_config(paths)
    try:
        clients = load_clients()
        if args.command == "list":
            return command_list(clients, paths, config)
        client = find_client(clients, args.name)
        if args.command == "start":
            return command_start(client, load_lab(config), paths, config)
        return command_stop(client, paths)
    except (ClientError, subprocess.CalledProcessError, OSError) as error:
        print(f"client: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

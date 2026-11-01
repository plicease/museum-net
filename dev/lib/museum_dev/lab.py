"""Lifecycle of the virtual lab: up, down, update, and destroy.

Every libvirt resource is named from dev/lab.yaml and carries an ownership
marker, so nothing here can touch VMs, networks, or bridges it did not create.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from .common import REPO_ROOT, Host, Paths, find_subnet_problems, load_config
from .libvirt import LibvirtError, Virsh

PREFIX = "museum-net-dev-"
POOL = f"{PREFIX}disks"
POOL_PATH = "/var/lib/libvirt/museum-net-dev"
BRIDGE_PREFIX = "mnd-"
MAX_INTERFACE_NAME = 15
OWNER_NS = "https://github.com/plicease/museum-net/lab"
# RFC 2544 benchmarking space: unlikely to collide with real networks.
ALLOCATION_RANGE = "198.18.0.0/15"
SHUTDOWN_TIMEOUT = 60
# Bump when domain_xml changes shape, so existing machines are redefined.
DOMAIN_LAYOUT = "2"
NIX = ["nix", "--extra-experimental-features", "nix-command flakes"]


class LabError(RuntimeError):
    pass


@dataclass(frozen=True)
class Network:
    key: str
    bridge: str

    @property
    def libvirt_name(self) -> str:
        return PREFIX + self.key.replace("_", "-")


@dataclass(frozen=True)
class Interface:
    name: str
    network: str
    host: int
    mac: str


@dataclass(frozen=True)
class Machine:
    name: str
    vcpus: int
    memory_mib: int
    disk_gib: int
    interfaces: tuple[Interface, ...]

    @property
    def domain(self) -> str:
        return PREFIX + self.name.removeprefix("dev-")

    @property
    def disk(self) -> str:
        return f"{self.name}.qcow2"

    @property
    def kernel(self) -> str:
        return f"{self.name}.kernel"

    @property
    def initrd(self) -> str:
        return f"{self.name}.initrd"


@dataclass(frozen=True)
class Lab:
    networks: tuple[Network, ...]
    machines: tuple[Machine, ...]


def load_lab(config: dict, path: Path = REPO_ROOT / "dev/lab.yaml") -> Lab:
    data = yaml.safe_load(path.read_text())
    networks = tuple(
        Network(key, entry["bridge"]) for key, entry in data["networks"].items()
    )
    known = {network.key for network in networks}
    for network in networks:
        if not network.bridge.startswith(BRIDGE_PREFIX):
            raise LabError(f"{network.key}: bridge must start with {BRIDGE_PREFIX}")
        if len(network.bridge) > MAX_INTERFACE_NAME:
            raise LabError(f"{network.key}: bridge name is too long for Linux")

    overrides = config.get("machines", {})
    machines = []
    for machine_index, (name, entry) in enumerate(data["machines"].items(), start=1):
        interfaces = []
        for interface_index, interface in enumerate(entry["interfaces"], start=1):
            if interface["network"] not in known:
                raise LabError(f"{name}: unknown network {interface['network']}")
            # 4d is "M": locally stable, and inside QEMU's 52:54:00 prefix.
            mac = f"52:54:00:4d:{machine_index:02x}:{interface_index:02x}"
            interfaces.append(Interface(mac=mac, **interface))
        local = overrides.get(name, {})
        machines.append(
            Machine(
                name=name,
                vcpus=local.get("vcpus", entry["vcpus"]),
                memory_mib=local.get("memory_mib", entry["memory_mib"]),
                disk_gib=local.get("disk_gib", entry["disk_gib"]),
                interfaces=tuple(interfaces),
            )
        )
    return Lab(networks, tuple(machines))


def allocate_subnets(
    lab: Lab,
    configured: dict[str, str],
    saved: dict[str, str],
    routes: list[tuple[str, str]],
) -> dict[str, str]:
    """Choose a subnet per network: config first, then saved, then a free /24."""
    chosen = {}
    for network in lab.networks:
        if network.key in configured:
            chosen[network.key] = configured[network.key]
        elif network.key in saved:
            chosen[network.key] = saved[network.key]

    candidates = ipaddress.ip_network(ALLOCATION_RANGE).subnets(new_prefix=24)
    for network in lab.networks:
        if network.key in chosen:
            continue
        for candidate in candidates:
            attempt = {**chosen, network.key: str(candidate)}
            if not find_subnet_problems(attempt, routes):
                chosen = attempt
                break
        else:
            raise LabError(f"no free subnet in {ALLOCATION_RANGE}")
    return chosen


def host_address(subnet: str, host: int) -> str:
    network = ipaddress.ip_network(subnet)
    return f"{network[host]}/{network.prefixlen}"


def nix_spec(lab: Lab, subnets: dict[str, str]) -> dict:
    """Return the per-host values passed to NixOS as the flake's `lab` input."""
    return {
        "machines": {
            machine.name: {
                "interfaces": [
                    {
                        "name": interface.name,
                        "mac": interface.mac,
                        "address": host_address(subnets[interface.network], interface.host),
                    }
                    for interface in machine.interfaces
                ]
            }
            for machine in lab.machines
        }
    }


def _marker(root: ET.Element, spec: str) -> None:
    ET.register_namespace("museum", OWNER_NS)
    metadata = ET.SubElement(root, "metadata")
    ET.SubElement(metadata, f"{{{OWNER_NS}}}lab", {"spec": spec})


def owned_spec(xml: str) -> str | None:
    """Return a resource's spec hash, or None if the lab did not create it."""
    found = ET.fromstring(xml).find(f"metadata/{{{OWNER_NS}}}lab")
    return None if found is None else found.get("spec")


def _fingerprint(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def network_xml(network: Network) -> str:
    # No <forward> and no <ip>: the bridge is plain layer 2, with no host
    # address, no NAT, no DHCP, and no physical NIC.
    spec = _fingerprint(network.libvirt_name, network.bridge)
    root = ET.Element("network")
    ET.SubElement(root, "name").text = network.libvirt_name
    _marker(root, spec)
    ET.SubElement(root, "bridge", {"name": network.bridge, "stp": "on", "delay": "0"})
    ET.SubElement(root, "dns", {"enable": "no"})
    return ET.tostring(root, encoding="unicode")


def domain_xml(lab: Lab, machine: Machine, toplevel: str, params: str) -> str:
    networks = {network.key: network for network in lab.networks}
    # The system path determines the kernel, initrd, and parameters, so this
    # can be computed by evaluation alone, without building anything.
    spec = _fingerprint(
        DOMAIN_LAYOUT,
        machine.name,
        toplevel,
        str((machine.vcpus, machine.memory_mib)),
        *(f"{i.mac} {networks[i.network].libvirt_name}" for i in machine.interfaces),
    )

    root = ET.Element("domain", {"type": "kvm"})
    ET.SubElement(root, "name").text = machine.domain
    _marker(root, spec)
    ET.SubElement(root, "memory", {"unit": "MiB"}).text = str(machine.memory_mib)
    ET.SubElement(root, "vcpu").text = str(machine.vcpus)

    os_element = ET.SubElement(root, "os")
    ET.SubElement(os_element, "type", {"arch": "x86_64", "machine": "q35"}).text = "hvm"
    # Copies in the pool: libvirt changes the owner of boot files, which must
    # never happen to anything inside /nix/store.
    ET.SubElement(os_element, "kernel").text = f"{POOL_PATH}/{machine.kernel}"
    ET.SubElement(os_element, "initrd").text = f"{POOL_PATH}/{machine.initrd}"
    ET.SubElement(os_element, "cmdline").text = f"init={toplevel}/init {params}".strip()

    features = ET.SubElement(root, "features")
    ET.SubElement(features, "acpi")
    ET.SubElement(root, "cpu", {"mode": "host-passthrough"})

    devices = ET.SubElement(root, "devices")
    # A file path, not a pool volume: on Ubuntu 26.04, QEMU is denied access to
    # type='volume' disks.
    disk = ET.SubElement(devices, "disk", {"type": "file", "device": "disk"})
    ET.SubElement(disk, "driver", {"name": "qemu", "type": "qcow2"})
    ET.SubElement(disk, "source", {"file": f"{POOL_PATH}/{machine.disk}"})
    ET.SubElement(disk, "target", {"dev": "vda", "bus": "virtio"})

    store = ET.SubElement(devices, "filesystem", {"type": "mount", "accessmode": "squash"})
    ET.SubElement(store, "driver", {"type": "path"})
    ET.SubElement(store, "source", {"dir": "/nix/store"})
    ET.SubElement(store, "target", {"dir": "nix-store"})
    ET.SubElement(store, "readonly")

    for interface in machine.interfaces:
        nic = ET.SubElement(devices, "interface", {"type": "network"})
        ET.SubElement(nic, "mac", {"address": interface.mac})
        ET.SubElement(nic, "source", {"network": networks[interface.network].libvirt_name})
        ET.SubElement(nic, "model", {"type": "virtio"})

    ET.SubElement(devices, "serial", {"type": "pty"})
    ET.SubElement(devices, "console", {"type": "pty"})
    channel = ET.SubElement(devices, "channel", {"type": "unix"})
    ET.SubElement(channel, "target", {"type": "virtio", "name": "org.qemu.guest_agent.0"})
    ET.SubElement(devices, "memballoon", {"model": "none"})
    rng = ET.SubElement(devices, "rng", {"model": "virtio"})
    ET.SubElement(rng, "backend", {"model": "random"}).text = "/dev/urandom"
    return ET.tostring(root, encoding="unicode")


class Nix:
    """Evaluates and builds lab machines against generated host values."""

    def __init__(self, spec: dict, gcroots: Path):
        self.spec = spec
        self.gcroots = gcroots

    def _flake_args(self, directory: str, machine: Machine, suffix: str = "") -> list[str]:
        attribute = f"nixosConfigurations.{machine.name}.config.system.build.toplevel"
        return [
            f"{REPO_ROOT}#{attribute}{suffix}",
            "--no-write-lock-file",
            "--override-input",
            "lab",
            f"path:{directory}",
        ]

    def _with_spec(self, action: Callable[[str], str]) -> str:
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "lab.json").write_text(json.dumps(self.spec, indent=2))
            return action(directory)

    def _run(self, command: list[str]) -> str:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise LabError(f"nix failed:\n{result.stderr.strip()}")
        return result.stdout.strip()

    def evaluate(self, machine: Machine) -> str:
        """Return the machine's system path without building or changing anything."""
        return self._with_spec(
            lambda directory: self._run(
                [*NIX, "eval", "--raw", *self._flake_args(directory, machine, ".outPath")]
            )
        )

    def build(self, machine: Machine) -> str:
        self.gcroots.mkdir(parents=True, exist_ok=True)
        link = self.gcroots / machine.name
        return self._with_spec(
            lambda directory: self._run(
                [
                    *NIX,
                    "build",
                    "--print-out-paths",
                    "--out-link",
                    str(link),
                    *self._flake_args(directory, machine),
                ]
            )
        )


@dataclass(frozen=True)
class Step:
    description: str
    action: Callable[[], None]


class Reconciler:
    """Works out what differs from dev/lab.yaml, and how to fix it."""

    def __init__(self, lab: Lab, virsh: Virsh, nix: Nix, host: Host):
        self.lab = lab
        self.virsh = virsh
        self.nix = nix
        self.host = host

    def check_ownership(self) -> None:
        domains = self.virsh.domains()
        for machine in self.lab.machines:
            if machine.domain in domains and owned_spec(
                self.virsh.domain_xml(machine.domain)
            ) is None:
                raise LabError(f"domain {machine.domain} exists but the lab does not own it")
        networks = self.virsh.networks()
        for network in self.lab.networks:
            if network.libvirt_name in networks and owned_spec(
                self.virsh.network_xml(network.libvirt_name)
            ) is None:
                raise LabError(f"network {network.libvirt_name} exists but the lab does not own it")
        if POOL in self.virsh.pools() and self.virsh.pool_path(POOL) != POOL_PATH:
            raise LabError(f"storage pool {POOL} exists but the lab does not own it")

    def check_bridges(self) -> None:
        """Fail rather than ever sharing a bridge the lab did not create."""
        existing = set(self.virsh.networks())
        for network in self.lab.networks:
            if network.libvirt_name in existing and self.virsh.network_active(
                network.libvirt_name
            ):
                continue
            if self.host.interface_exists(network.bridge):
                raise LabError(f"bridge {network.bridge} already exists and is not the lab's")

    def plan(self) -> tuple[list[Step], list[Machine]]:
        """Return the steps, and the machines those steps invalidate."""
        self.check_ownership()
        self.check_bridges()

        steps: list[Step] = []
        changed: dict[str, Machine] = {}
        pools = self.virsh.pools()
        if POOL not in pools:
            steps.append(
                Step(f"create storage pool {POOL} at {POOL_PATH}",
                     lambda: self.virsh.create_pool(POOL, POOL_PATH))
            )
        elif not self.virsh.pool_active(POOL):
            steps.append(Step(f"start storage pool {POOL}", lambda: self.virsh.start_pool(POOL)))

        volumes = self.virsh.volumes(POOL) if POOL in pools and self.virsh.pool_active(POOL) else set()
        for machine in self.lab.machines:
            if machine.disk not in volumes:
                steps.append(
                    Step(f"create {machine.disk_gib} GiB disk {machine.disk}",
                         lambda m=machine: self.virsh.create_disk(POOL, m.disk, m.disk_gib))
                )

        networks_changed = False
        existing_networks = self.virsh.networks()
        for network in self.lab.networks:
            xml = network_xml(network)
            name = network.libvirt_name
            if name not in existing_networks:
                steps.append(Step(f"define network {name} on {network.bridge}",
                                  lambda x=xml: self._define_network(x)))
            elif owned_spec(self.virsh.network_xml(name)) != owned_spec(xml):
                networks_changed = True
                steps.append(Step(f"replace network {name}",
                                  lambda n=name, x=xml: self._replace_network(n, x)))

        domains = self.virsh.domains()
        for machine in self.lab.machines:
            toplevel = self.nix.evaluate(machine)
            current = (
                owned_spec(self.virsh.domain_xml(machine.domain))
                if machine.domain in domains
                else None
            )
            if current != owned_spec(domain_xml(self.lab, machine, toplevel, "")):
                changed[machine.name] = machine
                steps.append(Step(f"build and define {machine.domain}",
                                  lambda m=machine: self._install_machine(m)))

        if networks_changed:
            changed = {machine.name: machine for machine in self.lab.machines}
        return steps, list(changed.values())

    def _define_network(self, xml: str) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".xml") as file:
            file.write(xml)
            file.flush()
            self.virsh.define_network(Path(file.name))

    def _replace_network(self, name: str, xml: str) -> None:
        stop_machines(self.lab, self.virsh)
        if self.virsh.network_active(name):
            self.virsh.stop_network(name)
        self.virsh.undefine_network(name)
        self._define_network(xml)

    def _install_machine(self, machine: Machine) -> None:
        toplevel = Path(self.nix.build(machine))
        if machine.domain in self.virsh.domains():
            if self.virsh.domain_running(machine.domain):
                stop_domain(self.virsh, machine.domain)
            # The definition carries a generated UUID; all state is on the disk.
            self.virsh.undefine_domain(machine.domain)
        self.virsh.upload_file(POOL, machine.kernel, (toplevel / "kernel").resolve())
        self.virsh.upload_file(POOL, machine.initrd, (toplevel / "initrd").resolve())
        params = (toplevel / "kernel-params").read_text().strip()
        xml = domain_xml(self.lab, machine, str(toplevel), params)
        with tempfile.NamedTemporaryFile("w", suffix=".xml") as file:
            file.write(xml)
            file.flush()
            self.virsh.define_domain(Path(file.name))


def stop_domain(virsh: Virsh, name: str) -> None:
    virsh.shutdown_domain(name)
    deadline = time.monotonic() + SHUTDOWN_TIMEOUT
    while time.monotonic() < deadline:
        if not virsh.domain_running(name):
            return
        time.sleep(1)
    virsh.force_stop_domain(name)


def stop_machines(lab: Lab, virsh: Virsh) -> None:
    domains = virsh.domains()
    for machine in lab.machines:
        if machine.domain in domains and virsh.domain_running(machine.domain):
            stop_domain(virsh, machine.domain)


def start_lab(lab: Lab, virsh: Virsh) -> None:
    for network in lab.networks:
        if not virsh.network_active(network.libvirt_name):
            virsh.start_network(network.libvirt_name)
    for machine in lab.machines:
        if not virsh.domain_running(machine.domain):
            virsh.start_domain(machine.domain)


def verify_attachments(lab: Lab, virsh: Virsh) -> None:
    """Confirm each machine's NICs use only the lab's own networks."""
    allowed = {network.libvirt_name for network in lab.networks}
    for machine in lab.machines:
        root = ET.fromstring(virsh.domain_xml(machine.domain))
        for source in root.findall("devices/interface/source"):
            if source.get("network") not in allowed:
                raise LabError(f"{machine.domain} is attached outside the lab: {source.attrib}")


@dataclass
class Context:
    lab: Lab
    virsh: Virsh
    reconciler: Reconciler
    state_file: Path
    subnets: dict[str, str]


def load_context() -> Context:
    host = Host()
    paths = Paths.from_env()
    config = load_config(paths)
    lab = load_lab(config)
    state_file = paths.data / "lab" / "subnets.json"
    saved = json.loads(state_file.read_text()) if state_file.exists() else {}
    routes = host.routes()
    subnets = allocate_subnets(lab, config.get("subnets", {}), saved, routes)
    problems = find_subnet_problems(subnets, routes)
    if problems:
        raise LabError("subnet problems:\n  " + "\n  ".join(problems))
    virsh = Virsh()
    nix = Nix(nix_spec(lab, subnets), paths.data / "lab" / "gcroots")
    return Context(lab, virsh, Reconciler(lab, virsh, nix, host), state_file, subnets)


def save_subnets(context: Context) -> None:
    context.state_file.parent.mkdir(parents=True, exist_ok=True)
    context.state_file.write_text(json.dumps(context.subnets, indent=2) + "\n")


def run_steps(steps: list[Step]) -> None:
    for step in steps:
        print(f"  {step.description}")
        step.action()


def command_up(args: argparse.Namespace) -> int:
    context = load_context()
    steps, _ = context.reconciler.plan()
    save_subnets(context)
    run_steps(steps)
    print("Starting the lab.")
    start_lab(context.lab, context.virsh)
    verify_attachments(context.lab, context.virsh)
    print("Lab is up. Use `virsh -c qemu:///system console <domain>` to reach a machine:")
    for machine in context.lab.machines:
        print(f"  {machine.domain}")
    return 0


def command_down(args: argparse.Namespace) -> int:
    virsh = Virsh()
    lab = load_lab(load_config(Paths.from_env()))
    Reconciler(lab, virsh, None, Host()).check_ownership()
    stop_machines(lab, virsh)
    existing = virsh.networks()
    for network in lab.networks:
        if network.libvirt_name in existing and virsh.network_active(network.libvirt_name):
            virsh.stop_network(network.libvirt_name)
    print("Lab is down. Disks are kept.")
    return 0


def command_update(args: argparse.Namespace) -> int:
    context = load_context()
    steps, changed = context.reconciler.plan()
    if not steps:
        print("Nothing to do: the lab matches this checkout.")
        return 0
    print("update will:" if args.dry_run else "Updating:")
    if args.dry_run:
        for step in steps:
            print(f"  {step.description}")
        print("Dry run: nothing changed.")
        return 0

    domains = context.virsh.domains()
    running = {
        machine.name
        for machine in changed
        if machine.domain in domains and context.virsh.domain_running(machine.domain)
    }
    save_subnets(context)
    run_steps(steps)
    for machine in changed:
        if machine.name in running:
            print(f"  restart {machine.domain}")
            context.virsh.start_domain(machine.domain)
    return 0


def confirmed(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("destroy: no terminal to confirm on; rerun with --yes", file=sys.stderr)
        return False
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


def command_destroy(args: argparse.Namespace) -> int:
    virsh = Virsh()
    lab = load_lab(load_config(Paths.from_env()))
    Reconciler(lab, virsh, None, Host()).check_ownership()

    domains = virsh.domains() & {machine.domain for machine in lab.machines}
    networks = virsh.networks() & {network.libvirt_name for network in lab.networks}
    print("destroy will remove:")
    for name in sorted(domains | networks):
        print(f"  {name}")
    if not confirmed("Remove these lab resources? Disks are kept.", args.yes):
        print("Nothing changed.")
        return 1
    for machine in lab.machines:
        if machine.domain in domains:
            if virsh.domain_running(machine.domain):
                virsh.force_stop_domain(machine.domain)
            virsh.undefine_domain(machine.domain)
    for network in lab.networks:
        if network.libvirt_name in networks:
            if virsh.network_active(network.libvirt_name):
                virsh.stop_network(network.libvirt_name)
            virsh.undefine_network(network.libvirt_name)

    if not args.disks or POOL not in virsh.pools():
        print("Lab removed. Disks are kept.")
        return 0
    if not confirmed(f"Permanently delete all disks in {POOL}?", args.yes):
        print("Lab removed. Disks are kept.")
        return 1
    if not virsh.pool_active(POOL):
        virsh.start_pool(POOL)
    for volume in virsh.volumes(POOL):
        virsh.delete_volume(POOL, volume)
    virsh.delete_pool(POOL)
    print("Lab and disks removed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="lab",
        description="Manage the isolated Museum Net lab.",
        epilog="Exit status: 0 success, 1 failed or declined, 2 usage error.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("up", help="build, define, and start the lab").set_defaults(run=command_up)
    commands.add_parser("down", help="stop the lab and keep its disks").set_defaults(run=command_down)
    update = commands.add_parser("update", help="reconcile the lab with this checkout")
    update.add_argument("--dry-run", action="store_true", help="show the plan and change nothing")
    update.set_defaults(run=command_update)
    destroy = commands.add_parser("destroy", help="remove the lab's own resources")
    destroy.add_argument("--disks", action="store_true", help="also delete VM disks")
    destroy.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    destroy.set_defaults(run=command_destroy)
    args = parser.parse_args()
    try:
        return args.run(args)
    except (LabError, LibvirtError) as error:
        print(f"{args.command}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

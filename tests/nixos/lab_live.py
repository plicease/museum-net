#!/usr/bin/env python3
"""Live test of the real lab: boot, isolation, lifecycle, and clean removal.

Needs KVM, libvirt, and Nix, so it is not part of the unit tests. It destroys
the lab, including disks, when it finishes:

    tests/nixos/lab_live.py
"""

import base64
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
URI = "qemu:///system"
GATEWAY = "museum-net-dev-gateway"
CLIENT = "museum-net-dev-client"
BRIDGES = ("mnd-upstream", "mnd-museum-a", "mnd-museum-b")
GUEST_PATH = "/run/wrappers/bin:/run/current-system/sw/bin"

failures = []


def check(condition, name, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {name}")
    if not condition:
        failures.append(name)
        if detail:
            print(f"       {detail}")


def host(*command):
    return subprocess.run(command, capture_output=True, text=True, check=False)


def virsh(*args):
    return host("virsh", "-c", URI, *args)


def script(name, *args):
    result = host(str(ROOT / "dev/scripts" / name), *args)
    if result.returncode != 0:
        sys.exit(f"{name} failed:\n{result.stdout}\n{result.stderr}")
    return result.stdout


def agent(domain, command):
    result = virsh("qemu-agent-command", domain, json.dumps(command))
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)["return"]


def wait_for_boot(domain):
    for _ in range(90):
        if virsh("qemu-agent-command", domain, '{"execute":"guest-ping"}').returncode == 0:
            if guest(domain, "systemctl is-system-running --wait")[1].strip() in (
                "running",
                "degraded",
            ):
                return
        time.sleep(2)
    sys.exit(f"{domain} did not boot")


def guest(domain, command):
    """Run a shell command in a guest. Returns (exit status, output)."""
    pid = agent(
        domain,
        {
            "execute": "guest-exec",
            "arguments": {
                "path": "/run/current-system/sw/bin/sh",
                "arg": ["-c", f"PATH={GUEST_PATH}; {command}"],
                "capture-output": True,
            },
        },
    )["pid"]
    while True:
        status = agent(domain, {"execute": "guest-exec-status", "arguments": {"pid": pid}})
        if status["exited"]:
            break
        time.sleep(0.2)
    output = base64.b64decode(status.get("out-data", "")).decode()
    return status.get("exitcode", 1), output


def reachable(domain, address):
    return guest(domain, f"ping -c1 -W2 {address}")[0] == 0


def foreign_state():
    """Names and running state of everything the lab does not own."""
    state = {}
    for command in (
        ("list", "--all", "--name"),
        ("list", "--name"),
        ("net-list", "--all", "--name"),
        ("net-list", "--name"),
        ("pool-list", "--all", "--name"),
        ("pool-list", "--name"),
    ):
        names = virsh(*command).stdout.split()
        state[command] = sorted(name for name in names if "museum-net-dev" not in name)
    return state


def host_lan_gateway():
    routes = json.loads(host("ip", "-j", "route", "show", "default").stdout)
    return routes[0]["gateway"] if routes else "192.0.2.1"


def host_lan_address():
    routes = json.loads(host("ip", "-j", "route", "get", "1.1.1.1").stdout)
    return routes[0]["prefsrc"]


def subnets():
    state = Path.home() / ".local/share/museum-net/lab/subnets.json"
    return json.loads(state.read_text())


def test_host_side():
    addresses = json.loads(host("ip", "-j", "addr", "show").stdout)
    bridges = {link["ifname"]: link for link in addresses if link["ifname"] in BRIDGES}
    check(set(bridges) == set(BRIDGES), "all lab bridges exist")
    check(
        all(not link["addr_info"] for link in bridges.values()),
        "lab bridges have no host address",
    )
    links = json.loads(host("ip", "-j", "link", "show").stdout)
    members = [link for link in links if link.get("master") in BRIDGES]
    check(
        members and all(link["ifname"].startswith("vnet") for link in members),
        "only VM ports are attached to lab bridges",
        str([link["ifname"] for link in members]),
    )
    foreign = [
        link["ifname"]
        for link in links
        if link["ifname"].startswith("vnet") and link.get("master") not in BRIDGES
    ]
    check(not foreign, "no lab port is on another bridge", str(foreign))
    routes = host("ip", "route").stdout
    check("mnd-" not in routes, "the host has no route into the lab")


def test_guests():
    nets = subnets()
    prefix = {key: value.rsplit(".", 1)[0] for key, value in nets.items()}
    gateway_museum = f"{prefix['museum_a']}.1"
    gateway_other_museum = f"{prefix['museum_b']}.1"
    gateway_mgmt = f"{prefix['upstream']}.2"

    check(guest(GATEWAY, "hostname")[1].strip() == "dev-gateway", "gateway hostname")
    check(guest(CLIENT, "hostname")[1].strip() == "dev-client", "client hostname")
    names = guest(GATEWAY, "ls /sys/class/net")[1].split()
    check(
        {"mgmt0", "museum0", "museum1"} <= set(names),
        "gateway interfaces are bound by MAC",
        str(names),
    )
    check(guest(CLIENT, "touch /nix/store/x")[0] != 0, "guest cannot write the Nix store")

    check(reachable(CLIENT, gateway_museum), "client reaches gateway on the museum network")
    check(not reachable(CLIENT, gateway_mgmt), "client cannot reach gateway management address")
    check(
        not reachable(CLIENT, gateway_other_museum),
        "client cannot reach the other museum network",
    )
    for domain, label in ((CLIENT, "client"), (GATEWAY, "gateway")):
        check(not reachable(domain, host_lan_gateway()), f"{label} cannot reach the host LAN")
        check(not reachable(domain, host_lan_address()), f"{label} cannot reach the host")
        check(not reachable(domain, "1.1.1.1"), f"{label} cannot reach the Internet")
        check(
            guest(domain, "ip route show default")[1].strip() == "",
            f"{label} has no default route",
        )
    forwarding = guest(GATEWAY, "cat /proc/sys/net/ipv4/ip_forward")[1].strip()
    check(forwarding == "0", "gateway does not forward IPv4")
    forwarding = guest(GATEWAY, "cat /proc/sys/net/ipv6/conf/all/forwarding")[1].strip()
    check(forwarding == "0", "gateway does not forward IPv6")


def test_lifecycle(before):
    output = script("update", "--dry-run")
    check("Nothing to do" in output, "update --dry-run finds nothing to do", output)

    guest(CLIENT, "echo kept > /root/marker; sync")
    script("down")
    state = virsh("domstate", CLIENT).stdout.strip()
    check(state == "shut off", "down stops machines", state)
    check(not Path("/sys/class/net/mnd-upstream").exists(), "down stops lab bridges")

    script("up")
    wait_for_boot(CLIENT)
    check(
        guest(CLIENT, "cat /root/marker")[1].strip() == "kept",
        "disk state survives down and up",
    )

    script("destroy", "--yes")
    check(CLIENT not in virsh("list", "--all").stdout, "destroy removes machines")
    check(
        "museum-net-dev-disks" in virsh("pool-list", "--all").stdout,
        "destroy keeps disks by default",
    )
    script("up")
    wait_for_boot(CLIENT)
    check(
        guest(CLIENT, "cat /root/marker")[1].strip() == "kept",
        "disk state survives destroy without --disks",
    )

    check(foreign_state() == before, "resources outside the lab are unchanged while up")
    script("destroy", "--yes", "--disks")
    remaining = [
        line
        for command in (("list", "--all"), ("net-list", "--all"), ("pool-list", "--all"))
        for line in virsh(*command).stdout.splitlines()
        if "museum-net-dev" in line
    ]
    check(not remaining, "destroy --disks leaves nothing behind", str(remaining))
    check(foreign_state() == before, "resources outside the lab are unchanged after destroy")


def main():
    before = foreign_state()
    print(script("up"))
    wait_for_boot(GATEWAY)
    wait_for_boot(CLIENT)
    test_host_side()
    test_guests()
    test_lifecycle(before)
    print(f"\n{len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

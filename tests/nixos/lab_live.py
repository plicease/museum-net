#!/usr/bin/env python3
"""Live test of the real lab: boot, isolation, VPN, SSH, lifecycle, and removal.

Needs KVM, libvirt, and Nix, so it is not part of the unit tests. It destroys
the lab, including disks, when it finishes:

    tests/nixos/lab_live.py
"""

import base64
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
URI = "qemu:///system"
GATEWAY = "museum-net-dev-gateway"
CLIENT = "museum-net-dev-client"
NULLRAY = "museum-net-dev-nullray"
ADMIN = "museum-net-dev-admin"
CREDENTIALS = Path.home() / ".local/share/museum-net/lab/credentials"
TUNNEL_UP = "Initialization Sequence Completed"
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


def wait_until(condition, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(1)
    return False


def tunnel_up(domain, address):
    output = guest(domain, "ip -4 -br addr show tun-mgmt 2>/dev/null")[1]
    return f"{address}/" in output


def connections(domain):
    """Count how many times a machine's VPN client has connected."""
    return guest(
        domain, f"journalctl -u openvpn-management --no-pager | grep -c '{TUNNEL_UP}'"
    )[1].strip()


def ssh(domain, address):
    return guest(
        domain,
        "ssh -i /run/lab-secrets/id_ed25519 -o BatchMode=yes -o StrictHostKeyChecking=yes "
        f"-o ConnectTimeout=5 museum-admin@{address} 'echo connection: $SSH_CONNECTION' 2>&1",
    )


def tcp_open(domain, address, port):
    command = f"timeout 4 sh -c 'exec 3<>/dev/tcp/{address}/{port}'"
    return guest(domain, command)[0] == 0


def vpn_addresses():
    base = subnets()["vpn"].rsplit(".", 1)[0]
    return f"{base}.1", f"{base}.2", f"{base}.3"


def test_tunnel():
    server, gateway, admin = vpn_addresses()
    check(wait_until(lambda: tunnel_up(GATEWAY, gateway)), "gateway gets its fixed VPN address")
    check(wait_until(lambda: tunnel_up(ADMIN, admin)), "admin gets its fixed VPN address")
    check(reachable(GATEWAY, server), "gateway reaches the VPN server")
    check(reachable(ADMIN, gateway), "admin reaches the gateway through the VPN")
    routes = guest(GATEWAY, "ip route")[1]
    check(
        "default" not in routes and "0.0.0.0/1" not in routes,
        "the VPN adds no default route",
        routes,
    )


def test_ssh():
    server, gateway, admin = vpn_addresses()
    for name, domain, target, source in (
        ("gateway to dev-nullray", GATEWAY, server, gateway),
        ("dev-nullray to gateway", NULLRAY, gateway, server),
        ("admin to gateway", ADMIN, gateway, admin),
    ):
        status, output = ssh(domain, target)
        check(
            status == 0 and f"connection: {source} " in output and f" {target} 22" in output,
            f"SSH {name} runs over the VPN",
            output,
        )
    status, output = ssh(ADMIN, server)
    check(status != 0 and "Permission denied" in output, "unauthorized SSH is refused", output)


def test_museum_isolation():
    server, gateway, _ = vpn_addresses()
    vpn_subnet = subnets()["vpn"]
    prefix = subnets()["museum_a"].rsplit(".", 1)[0]
    gateway_museum = f"{prefix}.1"
    gateway_mgmt = subnets()["upstream"].rsplit(".", 1)[0] + ".2"

    check(not reachable(CLIENT, gateway), "client cannot reach the gateway's VPN address")
    check(not reachable(CLIENT, server), "client cannot reach the VPN server")
    check(not tcp_open(CLIENT, gateway_museum, 22), "client cannot reach SSH on the museum address")
    check(guest(CLIENT, "ls /run/lab-secrets")[0] != 0, "client has no credentials directory")
    check(guest(CLIENT, "test -e /dev/vdb")[0] != 0, "client has no secrets disk")

    # A hostile client can route through the gateway by hand. Linux would
    # otherwise answer for any of the gateway's own addresses.
    guest(CLIENT, f"ip route add {vpn_subnet} via {gateway_museum}")
    guest(CLIENT, f"ip route add {subnets()['upstream']} via {gateway_museum}")
    check(not reachable(CLIENT, gateway), "a forced route cannot reach the gateway's VPN address")
    check(not reachable(CLIENT, server), "a forced route cannot reach the VPN server")
    check(not reachable(CLIENT, gateway_mgmt), "a forced route cannot reach the management address")
    check(not tcp_open(CLIENT, gateway, 22), "a forced route cannot reach SSH on the VPN address")
    check(not tcp_open(CLIENT, gateway_mgmt, 22), "a forced route cannot reach management SSH")
    guest(CLIENT, f"ip route del {vpn_subnet}; ip route del {subnets()['upstream']}")
    check(reachable(CLIENT, gateway_museum), "client still reaches the gateway normally")


def put_file(domain, path, data):
    encoded = base64.b64encode(data).decode()
    guest(domain, f"echo {encoded} | base64 -d > {path}; chmod 600 {path}")


def attempt_vpn(cert, key):
    """Try a one-shot VPN connection from the admin machine."""
    put_file(ADMIN, "/tmp/try.crt", cert)
    put_file(ADMIN, "/tmp/try.key", key)
    put_file(ADMIN, "/tmp/try-ca.crt", (CREDENTIALS / "ca.crt").read_bytes())
    server = subnets()["upstream"].rsplit(".", 1)[0] + ".1"
    return guest(
        ADMIN,
        f"timeout 12 openvpn --client --dev tun-try --dev-type tun --proto udp --nobind "
        f"--remote {server} 1194 --ca /tmp/try-ca.crt --cert /tmp/try.crt --key /tmp/try.key "
        "--remote-cert-tls server --verify-x509-name dev-nullray name --route-nopull 2>&1",
    )[1]


def issue(directory, name, ca_cert, ca_key):
    key, request, cert = directory / f"{name}.key", directory / f"{name}.csr", directory / f"{name}.crt"
    extensions = directory / f"{name}.ext"
    extensions.write_text("keyUsage=digitalSignature\nextendedKeyUsage=clientAuth\n")
    curve = ("-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes")
    host("openssl", "req", "-new", *curve, "-keyout", str(key), "-out", str(request), "-subj", f"/CN={name}")
    host(
        "openssl", "x509", "-req", "-in", str(request), "-CA", str(ca_cert), "-CAkey", str(ca_key),
        "-CAcreateserial", "-days", "1", "-extfile", str(extensions), "-out", str(cert),
    )
    return cert.read_bytes(), key.read_bytes()


def test_credentials():
    _, _, admin = vpn_addresses()
    guest(ADMIN, "systemctl stop openvpn-management")
    own = CREDENTIALS / "dev-admin"
    output = attempt_vpn((own / "vpn.crt").read_bytes(), (own / "vpn.key").read_bytes())
    check(TUNNEL_UP in output, "control: valid credentials connect the same way", output[-400:])

    with tempfile.TemporaryDirectory() as name:
        directory = Path(name)
        rogue_cert, rogue_key = directory / "rogue-ca.crt", directory / "rogue-ca.key"
        host(
            "openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
            "-nodes", "-keyout", str(rogue_key), "-out", str(rogue_cert), "-subj", "/CN=rogue",
            "-days", "1",
        )
        output = attempt_vpn(*issue(directory, "dev-admin", rogue_cert, rogue_key))
        check(TUNNEL_UP not in output, "a certificate from another CA is refused", output[-400:])
        output = attempt_vpn(
            *issue(directory, "dev-unknown", CREDENTIALS / "ca.crt", CREDENTIALS / "ca.key")
        )
        check(TUNNEL_UP not in output, "an unlisted client name is refused", output[-400:])
    check(guest(ADMIN, "ip link show tun-try")[0] != 0, "a refused attempt leaves no tunnel")
    guest(ADMIN, "rm -f /tmp/try*; systemctl start openvpn-management")
    check(wait_until(lambda: tunnel_up(ADMIN, admin)), "admin reconnects with its own credentials")


def test_reconnection():
    server, gateway, _ = vpn_addresses()
    gateway_museum = subnets()["museum_a"].rsplit(".", 1)[0] + ".1"

    count = connections(GATEWAY)
    guest(NULLRAY, "systemctl restart openvpn-management")
    check(
        wait_until(lambda: connections(GATEWAY) != count and reachable(GATEWAY, server), 90),
        "gateway reconnects after a server restart",
    )

    upstream = next(
        line.split()[0]
        for line in virsh("domiflist", GATEWAY).stdout.splitlines()
        if "museum-net-dev-upstream" in line
    )
    count = connections(GATEWAY)
    virsh("domif-setlink", GATEWAY, upstream, "down")
    time.sleep(15)
    check(reachable(CLIENT, gateway_museum), "museum network works while the link is down")
    virsh("domif-setlink", GATEWAY, upstream, "up")
    check(
        wait_until(lambda: connections(GATEWAY) != count and reachable(GATEWAY, server), 90),
        "gateway reconnects after a link interruption",
    )

    guest(NULLRAY, "systemctl stop openvpn-management")
    check(wait_until(lambda: not reachable(GATEWAY, server), 30), "control: the VPN is really down")
    check(reachable(CLIENT, gateway_museum), "museum network works while the VPN is down")
    check(status_ok(GATEWAY), "gateway stays up while the VPN is down")
    guest(NULLRAY, "systemctl start openvpn-management")
    check(
        wait_until(lambda: reachable(GATEWAY, server), 90), "gateway reconnects when the VPN returns"
    )
    check(ssh(ADMIN, gateway)[0] == 0 or wait_until(lambda: ssh(ADMIN, gateway)[0] == 0, 60),
          "SSH over the VPN works again")


def status_ok(domain):
    return guest(domain, "systemctl is-system-running")[1].strip() in ("running", "degraded")


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
    wait_for_boot(NULLRAY)
    wait_for_boot(ADMIN)
    test_host_side()
    test_guests()
    test_tunnel()
    test_ssh()
    test_museum_isolation()
    test_credentials()
    test_reconnection()
    test_lifecycle(before)
    print(f"\n{len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

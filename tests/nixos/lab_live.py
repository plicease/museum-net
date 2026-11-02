#!/usr/bin/env python3
"""Live test of the real lab: boot, isolation, VPN, SSH, management, archive, lifecycle, and removal.

Needs KVM, libvirt, and Nix, so it is not part of the unit tests. It destroys
the lab, including disks, when it finishes:

    tests/nixos/lab_live.py
"""

import base64
import json
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
URI = "qemu:///system"
GATEWAY = "museum-net-dev-gateway"
CLIENT = "museum-net-dev-client"
CLIENT_B = "museum-net-dev-client-b"
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


def museumctl(*args, user="root"):
    """Run museumctl in the gateway. Returns (exit status, output)."""
    command = shlex.join(("museumctl", *args))
    if user != "root":
        command = f"runuser -u {user} -- {command}"
    return guest(GATEWAY, f"{command} 2>&1")


def museumctl_json(*args):
    status, output = museumctl("--json", *args)
    if status != 0:
        raise RuntimeError(output)
    return json.loads(output)


def current_revision():
    return museumctl_json("status")["current"]["revision"]


def port_view(name):
    ports = museumctl_json("serial", "list")["ports"]
    return next(port for port in ports if port["name"] == name)


def serial_port(name):
    return port_view(name)["desired"]


def test_management_service():
    guest(GATEWAY, "systemctl is-active --wait museumd")
    socket = guest(GATEWAY, "stat -c '%a %U %G %F' /run/museumd/museumd.sock")[1].strip()
    check(socket == "660 root museum socket", "museumd socket is restricted to its group", socket)
    state = guest(GATEWAY, "stat -c '%a %U' /var/lib/museumd")[1].strip()
    check(state == "700 root", "museumd state is private", state)
    sockets = guest(GATEWAY, "ss -H -tulnp; echo end")[1]
    check(
        "end" in sockets and "museumd" not in sockets,
        "museumd has no network sockets",
        sockets,
    )
    interfaces = {i["name"]: i for i in museumctl_json("network", "list")["interfaces"]}
    check(
        interfaces["museum0"]["zone"] == "museum"
        and interfaces["museum0"]["state"] == "up"
        and interfaces["mgmt0"]["zone"] == "management",
        "museumctl lists interfaces with their zones and kernel state",
        str(interfaces),
    )

    adapters = {a["name"] for a in museumctl_json("serial", "adapters")["adapters"]}
    check(
        adapters == {"slip", "ppp", "sprocket", "interloper"},
        "museumctl discovers adapters from the daemon",
        str(adapters),
    )
    check(
        serial_port("com1")["mode"] == "disabled" and current_revision() == 1,
        "a new gateway starts with its ports disabled",
    )

    status, output = museumctl(
        "serial", "set", "com2", "--mode", "sprocket", "--baud", "57600", user="museum-admin"
    )
    check(status == 0, "an admin can assign a port", output)
    before = serial_port("com2")
    status, output = museumctl(
        "serial", "set", "com1", "--mode", "slip", "--baud", "38400",
        "--label", "PS/2 Model 70", user="museum-admin",
    )
    check(status == 0, "an admin can assign the other port", output)
    check(
        "stored as revision" in output and "not active" in output,
        "museumctl reports a stored change, not an active one",
        output,
    )
    port = port_view("com1")
    check(
        port["active"] is None and port["state"] == "stopped",
        "a stored mode is not reported as active",
        str(port),
    )
    listing = museumctl("serial", "list")[1]
    check("not active" in listing, "serial list says the port is not active", listing)
    check(serial_port("com2") == before, "changing com1 leaves com2 alone")
    assigned = serial_port("com1")
    check(
        (assigned["mode"], assigned["baud"], assigned["label"])
        == ("slip", 38400, "PS/2 Model 70"),
        "the assignment is stored",
        str(assigned),
    )


def test_management_authorization():
    status, output = museumctl("status", user="museum-viewer")
    check(status == 0, "a museum group member can read", output)
    revision = current_revision()
    for args in (
        ("serial", "set", "com1", "--mode", "ppp"),
        ("serial", "disable", "com1"),
        ("config", "rollback", "1"),
    ):
        status, output = museumctl(*args, user="museum-viewer")
        check(status != 0 and "may not change" in output, f"a viewer can't: {args[1]}", output)
    check(current_revision() == revision, "denied requests change nothing")

    status, output = museumctl("status", user="nobody")
    check(
        status != 0 and "museum group" in output,
        "a user outside the museum group can't connect",
        output,
    )


def test_management_validation():
    revision = current_revision()
    before = serial_port("com1")
    for args, expected in (
        (("serial", "set", "com1", "--mode", "gopher"), "unknown mode"),
        (("serial", "set", "com1", "--baud", "31337"), "unsupported baud"),
        (("serial", "set", "com1", "--framing", "9Z9"), "invalid framing"),
        (("serial", "set", "com9", "--mode", "slip"), "no serial port"),
        (("config", "rollback", "999"), "no revision"),
    ):
        status, output = museumctl(*args)
        check(status != 0 and expected in output, f"rejected: {expected}", output)
    check(
        current_revision() == revision and serial_port("com1") == before,
        "rejected changes leave the configuration as it was",
    )
    check(museumctl("config", "validate")[0] == 0, "the stored configuration validates")


def test_management_history():
    changes = museumctl_json("config", "history")["changes"]
    check(
        changes[0]["actor"] == "museum-admin" and "com1" in changes[0]["action"],
        "the change log names who changed what",
        str(changes[0]),
    )
    revision = current_revision()
    museumctl("serial", "disable", "com1")
    check(serial_port("com1")["mode"] == "disabled", "a port can be disabled")
    status, output = museumctl("config", "rollback", str(revision))
    check(status == 0 and serial_port("com1")["mode"] == "slip", "rollback restores it", output)
    newest = museumctl_json("config", "history")["changes"][0]
    check(
        newest["revision"] == revision + 2 and "roll back" in newest["action"],
        "rollback is recorded as a new revision",
        str(newest),
    )


def test_management_durability():
    revision = current_revision()
    main_pid = "systemctl show -p MainPID --value museumd"
    killed = guest(GATEWAY, main_pid)[1].strip()
    guest(GATEWAY, f"kill -KILL {killed}")
    check(
        wait_until(
            lambda: guest(GATEWAY, main_pid)[1].strip() not in ("0", killed)
            and museumctl("status")[0] == 0
        ),
        "museumd comes back after being killed",
    )
    check(current_revision() == revision, "a killed museumd keeps its configuration")

    # guest-exec can't outlive the guest, so reboot from outside.
    virsh("reboot", GATEWAY)
    time.sleep(10)
    wait_for_boot(GATEWAY)
    guest(GATEWAY, "systemctl is-active --wait museumd")
    check(
        current_revision() == revision and serial_port("com1")["baud"] == 38400,
        "the configuration survives a reboot",
    )


def test_management_without_network():
    interfaces = ("mgmt0", "museum0", "museum1")
    links = "; ".join(f"ip link set {name} down" for name in interfaces)
    guest(GATEWAY, f"systemctl stop systemd-networkd.socket systemd-networkd openvpn-management; {links}")
    state = guest(GATEWAY, "cat /sys/class/net/mgmt0/operstate")[1].strip()
    check(state == "down", "the gateway's network is down", state)
    status, output = museumctl("serial", "set", "com1", "--baud", "19200")
    check(status == 0, "museumctl changes configuration with no network", output)
    check(serial_port("com1")["baud"] == 19200, "the change applied with no network")

    museumctl("serial", "set", "com1", "--baud", "38400")
    up = "; ".join(f"ip link set {name} up" for name in interfaces)
    guest(GATEWAY, f"{up}; systemctl start systemd-networkd openvpn-management")
    check(
        wait_until(lambda: tunnel_up(GATEWAY, vpn_addresses()[1]), 90),
        "the VPN returns when the network does",
    )


def museum_addresses():
    """The gateway's address on each museum network, and each client's."""
    a = subnets()["museum_a"].rsplit(".", 1)[0]
    b = subnets()["museum_b"].rsplit(".", 1)[0]
    return f"{a}.1", f"{b}.1", f"{a}.10", f"{b}.10"


def sha256(domain, command):
    return guest(domain, f"{command} | sha256sum")[1].split()[0]


def ftp_session(domain, address, *commands):
    """Speak raw FTP. Each command is sent after reading the previous reply."""
    lines = "".join(f"printf '%s\\r\\n' {shlex.quote(c)} >&3; sleep 0.3; " for c in commands)
    script = f"exec 3<>/dev/tcp/{address}/21; sleep 0.3; {lines}timeout 1 cat <&3"
    return guest(domain, f"bash -c {shlex.quote(script)}")[1]


def ftp_banner(domain, address):
    script = f"exec 3<>/dev/tcp/{address}/21; head -n 1 <&3"
    return guest(domain, f"timeout 4 bash -c {shlex.quote(script)}")[1]


def test_archive_transfers():
    gateway_a, gateway_b, _, _ = museum_addresses()
    binary = "PUB/DOS/BINARY.BIN"
    expected = sha256(GATEWAY, f"cat /srv/archive/{binary}")
    for label, domain, address in (("A", CLIENT, gateway_a), ("B", CLIENT_B, gateway_b)):
        for name, command in (
            ("HTTP/1.1", f"curl -fsS http://{address}/{binary}"),
            ("HTTP/1.0", f"curl -fsS --http1.0 http://{address}/{binary}"),
            ("passive FTP", f"curl -fsS ftp://{address}/{binary}"),
            ("active FTP", f"curl -fsS --ftp-port - ftp://{address}/{binary}"),
        ):
            check(sha256(domain, command) == expected, f"{name} gives identical bytes on LAN {label}")

    for path, label in (
        ("PUB/DOS/HELLO.TXT", "an 8.3 name"),
        ("pub/long%20names/a%20file%20with%20spaces.txt", "a long name with spaces"),
    ):
        http = sha256(CLIENT, f"curl -fsS http://{gateway_a}/{path}")
        ftp = sha256(CLIENT, f"curl -fsS ftp://{gateway_a}/{path}")
        check(http == ftp, f"FTP and HTTP agree on {label}")

    listing = guest(CLIENT, f"curl -fsS http://{gateway_a}/")[1]
    check("README.TXT" in listing, "HTTP lists directories", listing)
    check("<script" not in listing.lower(), "the HTTP listing needs no JavaScript")
    listing = guest(CLIENT, f"curl -fsS --list-only ftp://{gateway_a}/")[1]
    check("README.TXT" in listing, "FTP lists directories", listing)

    bare = "GET /PUB HTTP/1.0\\r\\n\\r\\n"
    reply = guest(
        CLIENT,
        f"bash -c 'exec 3<>/dev/tcp/{gateway_a}/80; printf \"{bare}\" >&3; timeout 2 cat <&3'",
    )[1]
    check(
        f"Location: http://{gateway_a}/PUB/" in reply,
        "a directory redirect works without a Host header",
        reply,
    )
    reply = guest(CLIENT, f"curl -sS -o /dev/null -w '%{{http_code}}' -X PUT http://{gateway_a}/x")[1]
    check(reply.strip() in ("403", "405"), "HTTP refuses writes", reply)
    headers = guest(CLIENT, f"curl -sSI http://{gateway_a}/README.TXT")[1].lower()
    check("https" not in headers, "HTTP has no TLS redirect or upgrade", headers)


def test_archive_read_only():
    gateway_a, _, _, _ = museum_addresses()
    login = ("USER anonymous", "PASS guest@")
    for command in ("STOR x", "MKD x", "DELE README.TXT", "RNFR README.TXT", "SITE CHMOD 777 README.TXT"):
        reply = ftp_session(CLIENT, gateway_a, *login, "TYPE I", command, "QUIT")
        check(
            "230" in reply and not any(f"\n{c} " in reply for c in ("150", "226", "250", "257", "350")),
            f"anonymous FTP refuses {command.split()[0]}",
            reply,
        )
    reply = ftp_session(CLIENT, gateway_a, "USER root", "PASS root", "QUIT")
    check("230" not in reply, "FTP refuses a local user", reply)
    status = guest(GATEWAY, "touch /srv/archive/x")[0]
    check(status != 0, "the archive mount is read-only")

    # Without this, a client could make the gateway connect anywhere for it.
    upstream = subnets()["upstream"].rsplit(".", 1)[0].replace(".", ",")
    reply = ftp_session(CLIENT, gateway_a, *login, f"PORT {upstream},1,0,22", "LIST", "QUIT")
    check("500" in reply and "150" not in reply, "FTP refuses a bounce to another host", reply)


def test_archive_boundaries():
    _, gateway, _ = vpn_addresses()
    gateway_mgmt = subnets()["upstream"].rsplit(".", 1)[0] + ".2"
    for port, name in ((21, "FTP"), (80, "HTTP"), (2120, "passive FTP")):
        check(not tcp_open(ADMIN, gateway_mgmt, port), f"{name} is closed on the management address")
        check(not tcp_open(ADMIN, gateway, port), f"{name} is closed over the VPN")

    # Prove the services refuse those interfaces on their own, without the firewall.
    guest(GATEWAY, "nft add rule inet nixos-fw input-allow tcp dport { 21, 80 } accept")
    http = guest(ADMIN, f"timeout 4 curl -fsS -o /dev/null http://{gateway_mgmt}/README.TXT")[0]
    banner = ftp_banner(ADMIN, gateway_mgmt)
    guest(GATEWAY, "systemctl reload nftables || systemctl restart nftables")
    check(http != 0, "HTTP doesn't answer on management even with the firewall open")
    check("220" not in banner, "FTP doesn't answer on management even with the firewall open", banner)


def test_archive_missing():
    gateway_a, _, _, _ = museum_addresses()
    guest(GATEWAY, "umount /srv/archive")
    check(guest(GATEWAY, "mountpoint -q /srv/archive")[0] != 0, "the archive can be unmounted")

    code = guest(CLIENT, f"curl -s -o /dev/null -w '%{{http_code}}' http://{gateway_a}/README.TXT")[1]
    check(code.strip() == "503", "HTTP reports a missing archive", code)
    listing = guest(CLIENT, f"curl -fsS --list-only ftp://{gateway_a}/")[1]
    check("ARCHIVE-UNAVAILABLE.TXT" in listing, "FTP reports a missing archive", listing)
    check(museumctl("status")[0] == 0, "management works without the archive")

    guest(GATEWAY, "systemctl start srv-archive.mount")
    check(
        wait_until(lambda: guest(CLIENT, f"curl -fsS http://{gateway_a}/README.TXT")[0] == 0),
        "HTTP recovers when the archive returns",
    )
    listing = guest(CLIENT, f"curl -fsS --list-only ftp://{gateway_a}/")[1]
    check(
        "README.TXT" in listing and "UNAVAILABLE" not in listing,
        "the notice is hidden while the archive is mounted",
        listing,
    )


def test_archive_during_vpn_outage():
    gateway_a, _, _, _ = museum_addresses()
    _, gateway, _ = vpn_addresses()
    guest(NULLRAY, "systemctl stop openvpn-management")
    check(wait_until(lambda: not vpn_reachable(), 90), "the VPN is down for this test")
    check(guest(CLIENT, f"curl -fsS -o /dev/null http://{gateway_a}/README.TXT")[0] == 0,
          "HTTP works during a VPN outage")
    check(guest(CLIENT, f"curl -fsS -o /dev/null ftp://{gateway_a}/README.TXT")[0] == 0,
          "FTP works during a VPN outage")
    guest(NULLRAY, "systemctl start openvpn-management")
    check(wait_until(lambda: tunnel_up(GATEWAY, gateway), 90), "the VPN returns after the outage")


def vpn_reachable():
    server = vpn_addresses()[0]
    return reachable(GATEWAY, server)


def test_forwarding():
    gateway_a, gateway_b, client_a, client_b = museum_addresses()
    admin = subnets()["upstream"].rsplit(".", 1)[0] + ".3"
    guest(CLIENT, f"ip route add {client_b}/32 via {gateway_a}; ip route add {admin}/32 via {gateway_a}")
    guest(CLIENT_B, f"ip route add {client_a}/32 via {gateway_b}")
    guest(GATEWAY, "sysctl -w net.ipv4.ip_forward=1")
    check(not reachable(CLIENT, client_b), "the firewall stops museum A reaching museum B")
    check(not reachable(CLIENT_B, client_a), "the firewall stops museum B reaching museum A")
    check(not reachable(CLIENT, admin), "the firewall stops a museum client reaching upstream")
    dropped = guest(GATEWAY, "sysctl -w net.ipv4.ip_forward=0")[0]
    check(dropped == 0, "forwarding is switched back off")
    guest(CLIENT, f"ip route del {client_b}/32; ip route del {admin}/32")
    guest(CLIENT_B, f"ip route del {client_a}/32")


def test_ipv6():
    addresses = guest(GATEWAY, "ip -6 -br addr show museum0; ip -6 -br addr show museum1")[1]
    check("fe80" not in addresses, "museum interfaces have no IPv6 addresses", addresses)
    neighbours = guest(CLIENT, "ping -6 -c 2 -w 3 ff02::1%eth0 2>&1")[1]
    check("DUP" not in neighbours, "only the client answers link-local multicast", neighbours)

    # The firewall has to hold even if an address appears.
    guest(GATEWAY, "ip -6 addr add fd4d::1/64 dev museum0 nodad")
    guest(CLIENT, "ip -6 addr add fd4d::2/64 dev eth0 nodad")
    time.sleep(1)
    check(not reachable(CLIENT, "fd4d::1"), "the firewall drops IPv6 to the gateway")
    check(
        guest(CLIENT, "timeout 4 curl -fsS -o /dev/null http://[fd4d::1]/README.TXT")[0] != 0,
        "archive HTTP isn't reachable over IPv6",
    )
    check(
        guest(CLIENT, "timeout 4 bash -c 'exec 3<>/dev/tcp/fd4d::1/22'")[0] != 0,
        "SSH isn't reachable over IPv6",
    )
    guest(GATEWAY, "ip -6 addr del fd4d::1/64 dev museum0")
    guest(CLIENT, "ip -6 addr del fd4d::2/64 dev eth0")


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
    wait_for_boot(CLIENT_B)
    wait_for_boot(NULLRAY)
    wait_for_boot(ADMIN)
    test_host_side()
    test_guests()
    test_tunnel()
    test_ssh()
    test_museum_isolation()
    test_credentials()
    test_reconnection()
    test_management_service()
    test_management_authorization()
    test_management_validation()
    test_management_history()
    test_management_durability()
    test_management_without_network()
    test_archive_transfers()
    test_archive_read_only()
    test_archive_boundaries()
    test_archive_missing()
    test_archive_during_vpn_outage()
    test_forwarding()
    test_ipv6()
    test_lifecycle(before)
    print(f"\n{len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

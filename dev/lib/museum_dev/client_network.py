"""Finding and removing the network resources of emulated clients.

`destroy` has to clean up after a client that was killed or forgotten, so this
works from what the system reports, not from anything a session wrote down. A
TAP counts as ours only if it has our name prefix and marker and belongs to
this user. Removing one needs no privileges, because the kernel lets a TAP's
owner remove it and refuses anyone else.

This module only uses the standard library, so tests can run it inside a VM.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import signal
import struct
import time
from pathlib import Path

TAP_PREFIX = "mnc-"
TAP_MARKER = "museum-net-dev-client"
SWITCH = "vde_switch"
SWITCH_SOCKET = "vde"
STOP_TIMEOUT = 5

SYS_CLASS_NET = Path("/sys/class/net")
PROC = Path("/proc")

TUNSETIFF = 0x400454CA
TUNSETPERSIST = 0x400454CB
IFF_TAP = 0x0002
IFF_NO_PI = 0x1000


class ClientNetworkError(RuntimeError):
    pass


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def owned_taps(sys_class_net: Path = SYS_CLASS_NET, uid: int | None = None) -> list[str]:
    """TAP devices this user created for the lab."""
    uid = os.getuid() if uid is None else uid
    taps = []
    for device in sorted(sys_class_net.glob(f"{TAP_PREFIX}*")):
        if (
            _read(device / "ifalias") == TAP_MARKER
            and _read(device / "owner") == str(uid)
            # Only TUN and TAP devices have this file.
            and (device / "tun_flags").exists()
        ):
            taps.append(device.name)
    return taps


def remove_tap(name: str) -> None:
    """Remove a TAP this user owns. It must have no other process attached."""
    try:
        descriptor = os.open("/dev/net/tun", os.O_RDWR)
        try:
            request = struct.pack("16sH22x", name.encode(), IFF_TAP | IFF_NO_PI)
            fcntl.ioctl(descriptor, TUNSETIFF, request)
            fcntl.ioctl(descriptor, TUNSETPERSIST, 0)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise ClientNetworkError(f"can't remove {name}: {error}") from error


def client_switches(
    data: Path, proc: Path = PROC, uid: int | None = None
) -> dict[Path, int]:
    """Running switches of this user's clients, by their socket directory."""
    uid = os.getuid() if uid is None else uid
    root = data / "clients"
    switches = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            arguments = (entry / "cmdline").read_bytes().decode(errors="replace").split("\0")
        except OSError:
            continue  # It exited while we looked.
        if Path(arguments[0]).name != SWITCH:
            continue
        for argument in arguments[1:]:
            path = Path(argument)
            if path.name == SWITCH_SOCKET and path.parent.parent == root:
                switches[path] = int(entry.name)
    return switches


def _running(pid: int, proc: Path) -> bool:
    state = _read(proc / str(pid) / "stat")
    return state is not None and state.rsplit(")", 1)[-1].split()[0] != "Z"


def stop_switch(pid: int, proc: Path = PROC) -> None:
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline:
        if not _running(pid, proc):
            return
        time.sleep(0.05)
    os.kill(pid, signal.SIGKILL)


def leftovers(
    data: Path, sys_class_net: Path = SYS_CLASS_NET, proc: Path = PROC
) -> list[str]:
    """Names of what `cleanup` would remove."""
    switches = sorted(str(path) for path in client_switches(data, proc))
    return owned_taps(sys_class_net) + switches


def cleanup(data: Path, sys_class_net: Path = SYS_CLASS_NET, proc: Path = PROC) -> list[str]:
    """Remove every client network resource of this user. Returns their names.
    Switches go first, because a TAP can't be removed while one holds it."""
    removed = []
    for path, pid in client_switches(data, proc).items():
        stop_switch(pid, proc)
        removed.append(str(path))
    # A killed switch leaves its socket directory behind.
    for path in (data / "clients").glob(f"*/{SWITCH_SOCKET}"):
        shutil.rmtree(path, ignore_errors=True)
    for tap in owned_taps(sys_class_net):
        remove_tap(tap)
        removed.append(tap)
    return removed

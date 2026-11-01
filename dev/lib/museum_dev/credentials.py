"""Throwaway lab credentials: a test PKI and SSH keys.

These never leave the host except on each machine's own secrets disk. Lab VMs
share the host's Nix store, so nothing private may ever enter it; only the
public values returned by public() are passed to NixOS.
"""

from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path

PENDING = "pending"
CA_NAME = "Museum Net lab test CA, never trust in production"
EC_KEY = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes"]
DAYS = "3650"


def _run(*command: str) -> None:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"{command[0]} failed: {result.stderr.strip()}")


def _ssh_key(path: Path) -> None:
    if not path.exists():
        _run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "museum-net-lab", "-f", str(path))


class Credentials:
    def __init__(self, directory: Path):
        self.directory = directory

    def _machine(self, name: str) -> Path:
        return self.directory / name

    def ensure(self, machines: list) -> None:
        """Create whatever is missing. Existing credentials are kept."""
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        ca_key, ca_cert = self.directory / "ca.key", self.directory / "ca.crt"
        if not ca_cert.exists():
            _run(
                "openssl", "req", "-x509", *EC_KEY, "-keyout", str(ca_key), "-out", str(ca_cert),
                "-subj", f"/CN={CA_NAME}", "-days", DAYS,
            )
        for machine in machines:
            if not machine.has_secrets:
                continue
            directory = self._machine(machine.name)
            directory.mkdir(exist_ok=True)
            _ssh_key(directory / "id_ed25519")
            if machine.ssh_from:
                _ssh_key(directory / "ssh_host_ed25519_key")
            if machine.vpn_role and not (directory / "vpn.crt").exists():
                self._issue(machine.name, machine.vpn_role, directory)

    def _issue(self, name: str, role: str, directory: Path) -> None:
        usage = "serverAuth" if role == "server" else "clientAuth"
        extensions = directory / "vpn.ext"
        extensions.write_text(f"keyUsage=digitalSignature\nextendedKeyUsage={usage}\n")
        key, request, cert = directory / "vpn.key", directory / "vpn.csr", directory / "vpn.crt"
        _run("openssl", "req", "-new", *EC_KEY, "-keyout", str(key), "-out", str(request),
             "-subj", f"/CN={name}")
        _run(
            "openssl", "x509", "-req", "-in", str(request),
            "-CA", str(self.directory / "ca.crt"), "-CAkey", str(self.directory / "ca.key"),
            "-CAcreateserial", "-days", DAYS, "-extfile", str(extensions), "-out", str(cert),
        )
        request.unlink()
        extensions.unlink()

    def _read(self, path: Path) -> str:
        return path.read_text().strip() if path.exists() else PENDING

    def public(self, machines: list) -> dict:
        """Return what may safely enter the Nix store."""
        return {
            "ca": self._read(self.directory / "ca.crt"),
            "identities": {
                m.name: self._read(self._machine(m.name) / "id_ed25519.pub") for m in machines
            },
            "hostKeys": {
                m.name: self._read(self._machine(m.name) / "ssh_host_ed25519_key.pub")
                for m in machines
            },
        }

    def bundle(self, machine, target: Path) -> None:
        """Write the tar of one machine's private files, and no other machine's."""
        directory = self._machine(machine.name)
        with tarfile.open(target, "w") as archive:
            for name in ("id_ed25519", "ssh_host_ed25519_key", "vpn.crt", "vpn.key"):
                if (directory / name).exists():
                    archive.add(directory / name, arcname=name)

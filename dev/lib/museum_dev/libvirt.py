"""Thin wrapper around virsh for the system libvirt instance."""

from __future__ import annotations

import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

URI = "qemu:///system"


class LibvirtError(RuntimeError):
    pass


class Virsh:
    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["virsh", "-c", URI, *args],
            capture_output=True,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
            check=False,
        )
        if check and result.returncode != 0:
            raise LibvirtError(f"virsh {args[0]}: {result.stderr.strip()}")
        return result

    def _names(self, *args: str) -> set[str]:
        return {line.strip() for line in self.run(*args).stdout.splitlines() if line.strip()}

    def domains(self) -> set[str]:
        return self._names("list", "--all", "--name")

    def networks(self) -> set[str]:
        return self._names("net-list", "--all", "--name")

    def pools(self) -> set[str]:
        return self._names("pool-list", "--all", "--name")

    def volumes(self, pool: str) -> set[str]:
        lines = self.run("vol-list", pool).stdout.splitlines()[2:]
        return {line.split()[0] for line in lines if line.strip()}

    def domain_xml(self, name: str) -> str:
        return self.run("dumpxml", "--inactive", name).stdout

    def network_xml(self, name: str) -> str:
        return self.run("net-dumpxml", "--inactive", name).stdout

    def pool_path(self, name: str) -> str | None:
        return ET.fromstring(self.run("pool-dumpxml", name).stdout).findtext("target/path")

    def domain_running(self, name: str) -> bool:
        return self.run("domstate", name).stdout.strip() == "running"

    def network_active(self, name: str) -> bool:
        return name in self._names("net-list", "--name")

    def pool_active(self, name: str) -> bool:
        return name in self._names("pool-list", "--name")

    def define_network(self, xml_path: Path) -> None:
        self.run("net-define", str(xml_path))

    def start_network(self, name: str) -> None:
        self.run("net-start", name)

    def stop_network(self, name: str) -> None:
        self.run("net-destroy", name)

    def undefine_network(self, name: str) -> None:
        self.run("net-undefine", name)

    def create_pool(self, name: str, path: str) -> None:
        self.run("pool-define-as", name, "dir", "--target", path)
        self.run("pool-build", name)
        self.run("pool-start", name)

    def start_pool(self, name: str) -> None:
        self.run("pool-start", name)

    def delete_pool(self, name: str) -> None:
        self.run("pool-destroy", name)
        self.run("pool-delete", name)
        self.run("pool-undefine", name)

    def create_disk(self, pool: str, name: str, size_gib: int) -> None:
        self.run("vol-create-as", pool, name, f"{size_gib}G", "--format", "qcow2")

    def upload_file(self, pool: str, name: str, source: Path) -> None:
        """Copy a file into a pool, replacing any existing volume."""
        if name in self.volumes(pool):
            self.delete_volume(pool, name)
        size = source.stat().st_size
        self.run("vol-create-as", pool, name, str(size), "--format", "raw")
        self.run("vol-upload", "--pool", pool, name, str(source))

    def delete_volume(self, pool: str, name: str) -> None:
        self.run("vol-delete", "--pool", pool, name)

    def define_domain(self, xml_path: Path) -> None:
        self.run("define", str(xml_path))

    def start_domain(self, name: str) -> None:
        self.run("start", name)

    def shutdown_domain(self, name: str) -> None:
        self.run("shutdown", name)

    def force_stop_domain(self, name: str) -> None:
        self.run("destroy", name)

    def undefine_domain(self, name: str) -> None:
        self.run("undefine", name)

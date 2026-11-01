"""Install what the development lab needs, and nothing else.

Every privileged step is shown before it runs and goes through sudo. Rerunning
is safe: the plan only contains what is still missing.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .common import (
    Host,
    Manifest,
    Paths,
    Tool,
    load_manifest,
    load_tools,
    package_satisfied,
    sha256_file,
)

# Recommends are skipped so bootstrap only adds what the manifest names.
APT_INSTALL = [
    "sudo",
    "env",
    "DEBIAN_FRONTEND=noninteractive",
    "apt-get",
    "install",
    "-y",
    "--no-install-recommends",
]


@dataclass
class Plan:
    packages: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    tools: list[Tool] = field(default_factory=list)

    def empty(self) -> bool:
        return not (self.packages or self.groups or self.tools)


def build_plan(
    host: Host,
    manifest: Manifest,
    tools: list[Tool],
    paths: Paths,
    tiers: tuple[str, ...] = ("required",),
) -> Plan:
    return Plan(
        packages=[
            package.name
            for tier in tiers
            for package in manifest.tier(tier)
            if not package_satisfied(host, package)
        ],
        groups=[group for group in manifest.groups if not host.user_in_group(group)],
        tools=[
            tool
            for tool in tools
            if tool.tier in tiers
            and host.file_sha256(paths.tool_path(tool)) != tool.sha256
        ],
    )


def describe(plan: Plan, username: str, paths: Paths) -> list[str]:
    lines = []
    if plan.packages:
        lines.append(f"sudo: install APT packages: {' '.join(plan.packages)}")
    if plan.groups:
        lines.append(f"sudo: add {username} to groups: {' '.join(plan.groups)}")
    for tool in plan.tools:
        lines.append(f"user: install {tool.name} {tool.version} to {paths.tool_path(tool)}")
    return lines


def install_tool(tool: Tool, paths: Paths) -> None:
    """Download a pinned tool as the user. Only a verified file is installed."""
    download = paths.download_path(tool)
    download.parent.mkdir(parents=True, exist_ok=True)
    if not download.exists() or sha256_file(download) != tool.sha256:
        partial = download.with_name(download.name + ".partial")
        subprocess.run(
            ["curl", "-fsSL", "--proto", "=https", "-o", str(partial), tool.url],
            check=True,
        )
        digest = sha256_file(partial)
        if digest != tool.sha256:
            partial.unlink()
            raise RuntimeError(
                f"{tool.name}: checksum mismatch: expected {tool.sha256}, got {digest}"
            )
        partial.replace(download)

    target = paths.tool_path(tool)
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(target.name + ".new")
    staged.write_bytes(download.read_bytes())
    staged.chmod(0o755)
    staged.replace(target)


def apply(plan: Plan, username: str, paths: Paths) -> None:
    if plan.packages:
        subprocess.run(["sudo", "apt-get", "update"], check=True)
        subprocess.run([*APT_INSTALL, *plan.packages], check=True)
    for group in plan.groups:
        # This must follow the packages above, which create some of these groups.
        subprocess.run(["sudo", "usermod", "-aG", group, username], check=True)
    for tool in plan.tools:
        install_tool(tool, paths)


def confirmed(assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("bootstrap: no terminal to confirm on; rerun with --yes", file=sys.stderr)
        return False
    return input("Proceed? [y/N] ").strip().lower() in ("y", "yes")


def run(
    host: Host,
    manifest: Manifest,
    tools: list[Tool],
    paths: Paths,
    tiers: tuple[str, ...],
    dry_run: bool,
    assume_yes: bool,
) -> int:
    if host.is_root():
        print(
            "bootstrap: run this as your own user; it uses sudo where needed",
            file=sys.stderr,
        )
        return 1

    plan = build_plan(host, manifest, tools, paths, tiers)
    if plan.empty():
        print("Nothing to do: this host is already bootstrapped.")
        return 0

    print("bootstrap will:")
    for line in describe(plan, host.username(), paths):
        print(f"  {line}")
    if dry_run:
        print("Dry run: nothing changed.")
        return 0
    if not confirmed(assume_yes):
        print("Nothing changed.")
        return 1

    try:
        apply(plan, host.username(), paths)
    except (subprocess.CalledProcessError, RuntimeError, OSError) as error:
        print(f"bootstrap: failed: {error}", file=sys.stderr)
        print("Fix the problem and rerun; completed steps are skipped.", file=sys.stderr)
        return 1

    print("Done. Run ./dev/scripts/doctor to verify.")
    if plan.groups:
        print("Log out and back in so the new group memberships take effect.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="bootstrap",
        description="Install the Museum Net lab's host dependencies. Safe to rerun.",
        epilog="Exit status: 0 success or nothing to do, 1 failed or declined, 2 usage error.",
    )
    parser.add_argument("--optional", action="store_true", help="also install debugging aids")
    parser.add_argument("--test", action="store_true", help="also install test dependencies")
    parser.add_argument("--vintage", action="store_true", help="also install 86Box and its needs")
    parser.add_argument("--dry-run", action="store_true", help="show the plan and change nothing")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args()

    tiers = ["required"]
    if args.optional:
        tiers.append("optional")
    if args.test:
        tiers.append("test")
    if args.vintage:
        tiers.append("vintage")
    return run(
        Host(),
        load_manifest(),
        load_tools(),
        Paths.from_env(),
        tuple(tiers),
        args.dry_run,
        args.yes,
    )


if __name__ == "__main__":
    sys.exit(main())

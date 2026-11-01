"""Unit tests for doctor and bootstrap, against a fake host.

Run from the repository root:
    PYTHONPATH=dev/lib python3 -m unittest discover -s tests/provisioning
"""

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from museum_dev import bootstrap, common, doctor
from museum_dev.common import Manifest, Package, Paths, Tool, find_subnet_problems

REPO_ROOT = Path(__file__).resolve().parents[2]
PATHS = Paths(config=Path("/c"), data=Path("/d"), cache=Path("/k"))
TOOL = Tool("emu", "1.0", "https://example.invalid/emu", "a" * 64, "emu.bin", "testing")
MANIFEST = Manifest(
    packages=(
        Package("needed", "why", "required"),
        Package("extra", "why", "optional"),
        Package("rustish", "why", "test", command="rustish"),
    ),
    groups=("libvirt",),
)


class FakeHost:
    """A fully provisioned host. Tests override what they care about."""

    def __init__(self, **overrides):
        self.packages = {"needed", "extra", "rustish"}
        self.commands = set()
        self.groups = {"libvirt"}
        self.session_groups = {"libvirt"}
        self.digest = TOOL.sha256
        self.kvm = "ok"
        self.root = False
        self.subnet_routes = []
        self.free = 100 * 1024**3
        self.release = {"ID": "ubuntu", "VERSION_ID": "26.04", "PRETTY_NAME": "Ubuntu"}
        self.__dict__.update(overrides)

    def os_release(self):
        return self.release

    def machine(self):
        return "x86_64"

    def cpu_has_virtualization(self):
        return True

    def kvm_state(self):
        return self.kvm

    def package_installed(self, name):
        return name in self.packages

    def package_available(self, name):
        return True

    def has_command(self, name):
        return name in self.commands

    def username(self):
        return "tester"

    def is_root(self):
        return self.root

    def user_in_group(self, group):
        return group in self.groups

    def session_has_group(self, group):
        return group in self.session_groups

    def libvirt_error(self):
        return None

    def nix_version(self):
        return "nix 2.34"

    def nix_store_error(self):
        return None

    def routes(self):
        return self.subnet_routes

    def free_bytes(self, path):
        return self.free

    def file_sha256(self, path):
        return self.digest

    def directory_exists(self, path):
        return True


def statuses(checks):
    return {check.name: check.status for check in checks}


class ManifestTests(unittest.TestCase):
    def test_every_package_says_why_and_appears_once(self):
        manifest = common.load_manifest()
        names = [package.name for package in manifest.packages]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(package.why for package in manifest.packages))

    def test_shell_prerequisites_are_in_the_manifest(self):
        names = {p.name for p in common.load_manifest().tier("required")}
        self.assertLessEqual({"python3", "python3-yaml", "sudo", "curl"}, names)

    def test_tools_are_pinned_to_https_and_sha256(self):
        for tool in common.load_tools():
            self.assertTrue(tool.url.startswith("https://"))
            self.assertRegex(tool.sha256, r"^[0-9a-f]{64}$")


class PathTests(unittest.TestCase):
    def test_defaults_are_under_home(self):
        paths = Paths.from_env({"HOME": "/h"})
        self.assertEqual(paths.config, Path("/h/.config/museum-net"))
        self.assertEqual(paths.data, Path("/h/.local/share/museum-net"))
        self.assertEqual(paths.cache, Path("/h/.cache/museum-net"))

    def test_xdg_overrides_are_respected(self):
        paths = Paths.from_env({"HOME": "/h", "XDG_DATA_HOME": "/x"})
        self.assertEqual(paths.data, Path("/x/museum-net"))

    def test_empty_xdg_value_counts_as_unset(self):
        paths = Paths.from_env({"HOME": "/h", "XDG_CACHE_HOME": ""})
        self.assertEqual(paths.cache, Path("/h/.cache/museum-net"))


class SubnetTests(unittest.TestCase):
    def test_free_subnets_pass(self):
        routes = [("192.168.122.0/24", "virbr0")]
        self.assertEqual(find_subnet_problems({"a": "198.18.1.0/24"}, routes), [])

    def test_host_route_collision_names_the_device(self):
        problems = find_subnet_problems(
            {"a": "192.168.122.128/25"}, [("192.168.122.0/24", "virbr0")]
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("virbr0", problems[0])

    def test_bare_host_route_collides(self):
        problems = find_subnet_problems({"a": "10.9.8.0/24"}, [("10.9.8.126", "tun0")])
        self.assertEqual(len(problems), 1)

    def test_lab_networks_cannot_overlap_each_other(self):
        problems = find_subnet_problems(
            {"a": "198.18.0.0/16", "b": "198.18.5.0/24"}, []
        )
        self.assertEqual(len(problems), 1)

    def test_mixed_ip_versions_do_not_collide(self):
        self.assertEqual(find_subnet_problems({"a": "198.18.0.0/24"}, [("fe80::/64", "x")]), [])

    def test_invalid_subnet_is_reported(self):
        problems = find_subnet_problems({"a": "not-a-subnet"}, [])
        self.assertIn("invalid", problems[0])


class DoctorTests(unittest.TestCase):
    def test_healthy_host_has_no_failures(self):
        checks = doctor.collect(FakeHost(), MANIFEST, [TOOL], PATHS)
        self.assertEqual([c for c in checks if c.status != doctor.OK], [])

    def test_missing_required_package_fails_but_optional_warns(self):
        host = FakeHost(packages=set())
        result = statuses(doctor.check_packages(host, MANIFEST))
        self.assertEqual(result["required package needed"], doctor.FAIL)
        self.assertEqual(result["optional package extra"], doctor.WARN)

    def test_command_satisfies_a_package_without_dpkg(self):
        host = FakeHost(packages=set(), commands={"rustish"})
        result = statuses(doctor.check_packages(host, MANIFEST))
        self.assertEqual(result["test package rustish"], doctor.OK)

    def test_denied_kvm_fails(self):
        result = statuses(doctor.check_virtualization(FakeHost(kvm="denied")))
        self.assertEqual(result["/dev/kvm"], doctor.FAIL)

    def test_group_not_yet_active_warns_to_log_in_again(self):
        checks = doctor.check_groups(FakeHost(session_groups=set()), MANIFEST)
        self.assertEqual(checks[0].status, doctor.WARN)

    def test_not_in_group_fails(self):
        host = FakeHost(session_groups=set(), groups=set())
        self.assertEqual(doctor.check_groups(host, MANIFEST)[0].status, doctor.FAIL)

    def test_tampered_tool_fails(self):
        checks = doctor.check_tools(FakeHost(digest="b" * 64), [TOOL], PATHS)
        self.assertEqual(checks[0].status, doctor.FAIL)
        self.assertIn("mismatch", checks[0].detail)

    def test_missing_roms_only_warn(self):
        host = FakeHost()
        host.directory_exists = lambda path: False
        self.assertEqual(doctor.check_roms(host, PATHS, {})[0].status, doctor.WARN)

    def test_old_ubuntu_fails_and_newer_warns(self):
        old = {"ID": "ubuntu", "VERSION_ID": "24.04"}
        new = {"ID": "ubuntu", "VERSION_ID": "26.10"}
        self.assertEqual(doctor.check_platform(FakeHost(release=old))[0].status, doctor.FAIL)
        self.assertEqual(doctor.check_platform(FakeHost(release=new))[0].status, doctor.WARN)

    def test_other_distribution_fails(self):
        debian = {"ID": "debian", "VERSION_ID": "27"}
        self.assertEqual(doctor.check_platform(FakeHost(release=debian))[0].status, doctor.FAIL)

    def test_subnet_collision_fails(self):
        host = FakeHost(subnet_routes=[("198.18.0.0/24", "br0")])
        checks = doctor.check_subnets(host, {"subnets": {"a": "198.18.0.0/24"}})
        self.assertEqual(checks[0].status, doctor.FAIL)

    def test_report_exit_status(self):
        with mock.patch("builtins.print"):
            self.assertEqual(doctor.report([doctor.Check(doctor.WARN, "a", "b")]), 0)
            self.assertEqual(doctor.report([doctor.Check(doctor.FAIL, "a", "b")]), 1)


class PlanTests(unittest.TestCase):
    def test_provisioned_host_has_an_empty_plan(self):
        plan = bootstrap.build_plan(
            FakeHost(), MANIFEST, [TOOL], PATHS, ("required", "optional", "test")
        )
        self.assertTrue(plan.empty())

    def test_default_plan_covers_only_required_packages(self):
        host = FakeHost(packages=set())
        plan = bootstrap.build_plan(host, MANIFEST, [TOOL], PATHS)
        self.assertEqual(plan.packages, ["needed"])

    def test_tiers_are_added_on_request(self):
        host = FakeHost(packages=set())
        plan = bootstrap.build_plan(host, MANIFEST, [TOOL], PATHS, ("required", "optional"))
        self.assertEqual(plan.packages, ["needed", "extra"])

    def test_plan_includes_missing_groups_and_tools(self):
        host = FakeHost(groups=set(), digest=None)
        plan = bootstrap.build_plan(host, MANIFEST, [TOOL], PATHS)
        self.assertEqual(plan.groups, ["libvirt"])
        self.assertEqual(plan.tools, [TOOL])


class RunTests(unittest.TestCase):
    def run_bootstrap(self, host, dry_run=False, assume_yes=False):
        with (
            mock.patch.object(bootstrap, "apply") as apply,
            mock.patch("builtins.print"),
            mock.patch("sys.stdin.isatty", return_value=False),
        ):
            status = bootstrap.run(
                host, MANIFEST, [TOOL], PATHS, ("required",), dry_run, assume_yes
            )
        return status, apply

    def test_refuses_to_run_as_root(self):
        status, apply = self.run_bootstrap(FakeHost(root=True, packages=set()), assume_yes=True)
        self.assertEqual(status, 1)
        apply.assert_not_called()

    def test_nothing_to_do_changes_nothing(self):
        status, apply = self.run_bootstrap(FakeHost())
        self.assertEqual(status, 0)
        apply.assert_not_called()

    def test_dry_run_changes_nothing(self):
        status, apply = self.run_bootstrap(FakeHost(packages=set()), dry_run=True, assume_yes=True)
        self.assertEqual(status, 0)
        apply.assert_not_called()

    def test_no_terminal_without_yes_changes_nothing(self):
        status, apply = self.run_bootstrap(FakeHost(packages=set()))
        self.assertEqual(status, 1)
        apply.assert_not_called()

    def test_yes_applies_the_plan(self):
        status, apply = self.run_bootstrap(FakeHost(packages=set()), assume_yes=True)
        self.assertEqual(status, 0)
        apply.assert_called_once()


class InstallToolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.paths = Paths(config=root / "c", data=root / "d", cache=root / "k")
        self.body = b"tool bytes"
        self.tool = Tool(
            "emu",
            "1.0",
            "https://example.invalid/emu",
            hashlib.sha256(self.body).hexdigest(),
            "emu.bin",
            "testing",
        )

    def fake_curl(self, body):
        def run(command, check):
            Path(command[command.index("-o") + 1]).write_bytes(body)

        return mock.patch.object(subprocess, "run", side_effect=run)

    def test_verified_download_is_installed_executable(self):
        with self.fake_curl(self.body):
            bootstrap.install_tool(self.tool, self.paths)
        target = self.paths.tool_path(self.tool)
        self.assertEqual(target.read_bytes(), self.body)
        self.assertTrue(target.stat().st_mode & 0o100)

    def test_bad_download_is_rejected_and_never_installed(self):
        with self.fake_curl(b"evil"), self.assertRaisesRegex(RuntimeError, "mismatch"):
            bootstrap.install_tool(self.tool, self.paths)
        self.assertFalse(self.paths.tool_path(self.tool).exists())
        self.assertEqual(list(self.paths.download_path(self.tool).parent.iterdir()), [])

    def test_cached_download_is_reused(self):
        with self.fake_curl(self.body):
            bootstrap.install_tool(self.tool, self.paths)
        self.paths.tool_path(self.tool).unlink()
        with self.fake_curl(b"should not be fetched") as run:
            bootstrap.install_tool(self.tool, self.paths)
        run.assert_not_called()
        self.assertEqual(self.paths.tool_path(self.tool).read_bytes(), self.body)


if __name__ == "__main__":
    unittest.main()

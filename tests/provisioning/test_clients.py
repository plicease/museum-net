"""Unit tests for the emulated client layer. Nothing here needs an emulator.

Run from the repository root:
    PYTHONPATH=dev/lib python3 -m unittest discover -s tests/provisioning
"""

import configparser
import json
import tempfile
import unittest
from pathlib import Path

from museum_dev import clients as module
from museum_dev.clients import (
    Asset,
    Box86,
    Client,
    ClientError,
    attach_commands,
    assigned_address,
    emulator_for,
    ensure_tap,
    find_client,
    load_clients,
    missing_assets,
    tap_master,
)
from museum_dev.common import Paths
from museum_dev.lab import load_lab


def paths_in(directory):
    return Paths.from_env({"HOME": str(directory)})


def make_client(directory, **overrides):
    values = dict(
        name="retro",
        description="test",
        guest="other-os",
        emulator="86box",
        directory=Path(directory),
        config="86box.cfg",
        lab_network="museum_a",
        host=20,
        card="wd8003e",
        assets=(Asset("disk.img", "a disk"),),
        capabilities=("ftp",),
    )
    return Client(**{**values, **overrides})


class TrackedClients(unittest.TestCase):
    def setUp(self):
        self.clients = load_clients()
        self.lab = load_lab({})

    def test_clients_are_discovered_from_their_directories(self):
        self.assertEqual({c.name for c in self.clients}, {"dos", "win311"})

    def test_every_client_uses_a_lab_museum_network(self):
        names = {n.key for n in self.lab.networks}
        for client in self.clients:
            self.assertIn(client.lab_network, names)
            self.assertTrue(client.lab_network.startswith("museum"), client.name)

    def test_client_addresses_are_clear_of_lab_machines(self):
        used = {
            (interface.network, interface.host)
            for machine in self.lab.machines
            for interface in machine.interfaces
        }
        for client in self.clients:
            self.assertNotIn((client.lab_network, client.host), used)

    def test_shared_code_names_no_client(self):
        source = Path(module.__file__).read_text()
        for client in self.clients:
            self.assertNotIn(f'"{client.name}"', source)

    def test_only_machine_configuration_is_tracked(self):
        for client in self.clients:
            files = {path.name for path in client.directory.iterdir()}
            self.assertEqual(files, {"client.yaml", client.config})


class Discovery(unittest.TestCase):
    def write(self, directory, text):
        path = Path(directory) / "new" / "client.yaml"
        path.parent.mkdir()
        path.write_text(text)

    def descriptor(self, version=1, name_extra=""):
        return f"""
version: {version}
description: x
guest: y
emulator: some-other-emulator
config: machine.conf
network: {{lab_network: museum_a, host: 30, card: x}}
assets: []
capabilities: []
"""

    def test_a_new_directory_is_a_new_client(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write(directory, self.descriptor())
            self.assertEqual([c.name for c in load_clients(Path(directory))], ["new"])

    def test_an_unknown_version_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write(directory, self.descriptor(version=2))
            with self.assertRaises(ClientError):
                load_clients(Path(directory))

    def test_an_unsupported_emulator_is_reported(self):
        with self.assertRaises(ClientError):
            emulator_for(make_client("/", emulator="mini-vmac"))

    def test_a_long_name_that_cannot_be_a_tap_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a-very-long-client-name" / "client.yaml"
            path.parent.mkdir()
            path.write_text(self.descriptor())
            with self.assertRaises(ClientError):
                load_clients(Path(directory))

    def test_an_unknown_client_lists_the_real_ones(self):
        with self.assertRaisesRegex(ClientError, "retro"):
            find_client([make_client("/")], "nope")


class Tap(unittest.TestCase):
    def test_commands_only_touch_the_named_tap_and_lab_bridge(self):
        commands = attach_commands("mnc-retro", "mnd-museum-a", "user")
        self.assertIn("user", commands[0])
        self.assertEqual(commands[1][-3:], ["master", "mnd-museum-a", "up"])

    def sysfs(self, directory, master=None):
        device = Path(directory) / "mnc-retro"
        device.mkdir()
        if master:
            target = Path(directory) / master
            target.mkdir()
            (device / "master").symlink_to(target)

    def test_master_distinguishes_absent_detached_and_attached(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(tap_master("mnc-retro", Path(directory)))
            self.sysfs(directory)
            self.assertEqual(tap_master("mnc-retro", Path(directory)), "")
        with tempfile.TemporaryDirectory() as directory:
            self.sysfs(directory, "mnd-museum-a")
            self.assertEqual(tap_master("mnc-retro", Path(directory)), "mnd-museum-a")

    def with_master(self, value):
        original = module.tap_master
        module.tap_master = lambda tap: value
        self.addCleanup(setattr, module, "tap_master", original)

    def test_a_tap_on_any_other_bridge_is_refused(self):
        self.with_master("virbr0")
        calls = []
        with self.assertRaisesRegex(ClientError, "virbr0"):
            ensure_tap(make_client("/"), "mnd-museum-a", run=lambda *a, **k: calls.append(a))
        self.assertEqual(calls, [])

    def test_an_attached_tap_is_left_alone(self):
        self.with_master("mnd-museum-a")
        calls = []
        ensure_tap(make_client("/"), "mnd-museum-a", run=lambda *a, **k: calls.append(a))
        self.assertEqual(calls, [])


class Box86Config(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "86box.cfg").write_text("[Machine]\nmachine = isa486\n")
        self.state = self.root / "state"
        self.state.mkdir()
        self.client = make_client(self.root)

    def read(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read(self.state / "86box.cfg")
        return parser

    def test_the_network_is_only_ever_the_lab_switch(self):
        socket = self.state / "vde"
        Box86().write_config(self.client, self.state, socket, paths_in(self.root))
        network = self.read()["Network"]
        self.assertEqual(network["net_01_net_type"], "vde")
        self.assertEqual(network["net_01_host_device"], str(socket))

    def test_a_saved_setting_cannot_move_the_machine_off_the_lab(self):
        (self.state / "86box.cfg").write_text(
            "[Machine]\nmachine = other\n\n[Network]\nnet_01_net_type = tap\n"
            "net_01_host_device = virbr0\nnet_02_net_type = tap\n"
        )
        Box86().write_config(self.client, self.state, self.state / "vde", paths_in(self.root))
        config = self.read()
        self.assertEqual(config["Network"]["net_01_net_type"], "vde")
        self.assertNotIn("net_02_net_type", config["Network"])
        self.assertEqual(config["Machine"]["machine"], "other", "other settings are kept")

    def test_the_tracked_file_is_not_written(self):
        tracked = self.root / "86box.cfg"
        before = tracked.read_text()
        Box86().write_config(self.client, self.state, self.state / "vde", paths_in(self.root))
        self.assertEqual(tracked.read_text(), before)

    def test_missing_tools_and_roms_are_reported(self):
        problems = Box86().problems(self.client, paths_in(self.root), {})
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("ROM" in p for p in problems))


class Assets(unittest.TestCase):
    def test_assets_live_outside_the_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = paths_in(directory)
            client = make_client(directory)
            self.assertIn("disk.img", missing_assets(client, paths)[0])
            state = paths.data / "clients" / "retro"
            state.mkdir(parents=True)
            (state / "disk.img").write_bytes(b"")
            self.assertEqual(missing_assets(client, paths), [])

    def test_the_guest_address_comes_from_the_lab_subnet(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = paths_in(directory)
            client = make_client(directory)
            self.assertIsNone(assigned_address(client, paths))
            saved = paths.data / "lab" / "subnets.json"
            saved.parent.mkdir(parents=True)
            saved.write_text(json.dumps({"museum_a": "198.18.7.0/24"}))
            self.assertEqual(assigned_address(client, paths), "198.18.7.20")


if __name__ == "__main__":
    unittest.main()

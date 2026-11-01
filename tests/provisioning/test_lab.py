"""Unit tests for lab orchestration, against fake libvirt and Nix.

Run from the repository root:
    PYTHONPATH=dev/lib python3 -m unittest discover -s tests/provisioning
"""

import json
import stat
import tarfile
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from museum_dev import lab as lab_module
from museum_dev.credentials import PENDING, Credentials
from museum_dev.lab import (
    MAX_INTERFACE_NAME,
    POOL,
    POOL_PATH,
    Lab,
    LabError,
    Machine,
    Network,
    Interface,
    Reconciler,
    allocate_subnets,
    domain_xml,
    host_address,
    load_lab,
    network_xml,
    nix_spec,
    owned_spec,
)

NETWORK = Network("museum_a", "mnd-museum-a")
MACHINE = Machine(
    "dev-box", 1, 256, 1, (Interface("eth0", "museum_a", 10, "52:54:00:4d:01:01"),)
)
LAB = Lab((NETWORK,), (MACHINE,))
TOPLEVEL = "/nix/store/abc-system"
SUBNETS = {
    "upstream": "198.18.0.0/24",
    "museum_a": "198.18.1.0/24",
    "museum_b": "198.18.2.0/24",
    "vpn": "198.18.3.0/24",
}


class FakeVirsh:
    def __init__(self):
        self.domain_defs = {}
        self.network_defs = {}
        self.pool_defs = {POOL: POOL_PATH}
        self.volume_names = set()
        self.active_networks = set()

    def domains(self):
        return set(self.domain_defs)

    def networks(self):
        return set(self.network_defs)

    def pools(self):
        return set(self.pool_defs)

    def volumes(self, pool):
        return self.volume_names

    def domain_xml(self, name):
        return self.domain_defs[name]

    def network_xml(self, name):
        return self.network_defs[name]

    def pool_path(self, name):
        return self.pool_defs[name]

    def pool_active(self, name):
        return True

    def network_active(self, name):
        return name in self.active_networks


class FakeNix:
    def evaluate(self, machine):
        return TOPLEVEL


class FakeHost:
    def __init__(self, interfaces=()):
        self.interfaces = set(interfaces)

    def interface_exists(self, name):
        return name in self.interfaces


def provisioned_virsh():
    virsh = FakeVirsh()
    virsh.volume_names = {MACHINE.disk}
    virsh.network_defs[NETWORK.libvirt_name] = network_xml(NETWORK)
    virsh.domain_defs[MACHINE.domain] = domain_xml(LAB, MACHINE, TOPLEVEL, "console=ttyS0")
    return virsh


def plan(virsh, host=None):
    reconciler = Reconciler(LAB, virsh, FakeNix(), host or FakeHost())
    steps, changed = reconciler.plan()
    return [step.description for step in steps], changed


class DescriptorTests(unittest.TestCase):
    def setUp(self):
        self.lab = load_lab({})

    def test_bridge_names_fit_linux_and_use_the_lab_prefix(self):
        for network in self.lab.networks:
            self.assertLessEqual(len(network.bridge), MAX_INTERFACE_NAME)
            self.assertTrue(network.bridge.startswith("mnd-"))

    def test_macs_are_unique_and_stable(self):
        macs = [i.mac for m in self.lab.machines for i in m.interfaces]
        self.assertEqual(len(macs), len(set(macs)))
        self.assertEqual(macs, [i.mac for m in load_lab({}).machines for i in m.interfaces])

    def test_gateway_has_two_museum_nics_and_a_management_nic(self):
        gateway = next(m for m in self.lab.machines if m.name == "dev-gateway")
        self.assertEqual(
            [i.network for i in gateway.interfaces], ["upstream", "museum_a", "museum_b"]
        )

    def test_only_museum_networks_host_clients(self):
        client = next(m for m in self.lab.machines if m.name == "dev-client")
        self.assertEqual([i.network for i in client.interfaces], ["museum_a"])

    def test_local_config_overrides_machine_size(self):
        lab = load_lab({"machines": {"dev-client": {"memory_mib": 2048}}})
        client = next(m for m in lab.machines if m.name == "dev-client")
        self.assertEqual(client.memory_mib, 2048)

    def test_example_lab_matches_the_descriptor(self):
        root = Path(lab_module.__file__).resolve().parents[3]
        example = json.loads((root / "dev/lab-example/lab.json").read_text())
        public = Credentials(Path("/nonexistent")).public(list(self.lab.machines))
        self.assertEqual(example, nix_spec(self.lab, SUBNETS, public))


class SubnetTests(unittest.TestCase):
    def test_allocates_free_distinct_subnets(self):
        subnets = allocate_subnets(load_lab({}), {}, {}, [])
        self.assertEqual(len(set(subnets.values())), 4)

    def test_skips_subnets_used_by_host_routes(self):
        subnets = allocate_subnets(LAB, {}, {}, [("198.18.0.0/24", "br0")])
        self.assertEqual(subnets["museum_a"], "198.18.1.0/24")

    def test_saved_subnets_stay_stable(self):
        saved = {"museum_a": "198.18.9.0/24"}
        self.assertEqual(allocate_subnets(LAB, {}, saved, []), saved)

    def test_config_beats_saved(self):
        chosen = allocate_subnets(
            LAB, {"museum_a": "10.0.0.0/24"}, {"museum_a": "198.18.9.0/24"}, []
        )
        self.assertEqual(chosen["museum_a"], "10.0.0.0/24")

    def test_host_address_keeps_the_prefix(self):
        self.assertEqual(host_address("198.18.1.0/24", 10), "198.18.1.10/24")


class CredentialTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.credentials = Credentials(self.root / "credentials")
        self.lab = load_lab({})
        self.machines = {m.name: m for m in self.lab.machines}
        self.credentials.ensure(list(self.lab.machines))

    def bundle_names(self, name):
        target = self.root / f"{name}.tar"
        self.credentials.bundle(self.machines[name], target)
        with tarfile.open(target) as archive:
            return set(archive.getnames())

    def test_directory_is_private(self):
        self.assertEqual(stat.S_IMODE(self.credentials.directory.stat().st_mode), 0o700)

    def test_museum_client_gets_no_credentials(self):
        self.assertFalse(self.machines["dev-client"].has_secrets)
        self.assertEqual(self.bundle_names("dev-client"), set())

    def test_a_machine_only_gets_its_own_keys(self):
        target = self.root / "gateway.tar"
        self.credentials.bundle(self.machines["dev-gateway"], target)
        with tarfile.open(target) as archive:
            data = {m.name: archive.extractfile(m).read() for m in archive.getmembers()}
        other = (self.credentials.directory / "dev-admin/vpn.key").read_bytes()
        self.assertNotIn(other, data.values())
        self.assertEqual(data["vpn.key"], (self.credentials.directory / "dev-gateway/vpn.key").read_bytes())

    def test_the_ca_key_is_never_bundled(self):
        for name in ("dev-gateway", "dev-nullray", "dev-admin"):
            self.assertNotIn("ca.key", self.bundle_names(name))

    def test_nothing_private_enters_the_nix_spec(self):
        public = self.credentials.public(list(self.lab.machines))
        spec = json.dumps(nix_spec(self.lab, SUBNETS, public))
        self.assertNotIn("PRIVATE KEY", spec)
        private = [*self.credentials.directory.rglob("*.key"), *self.credentials.directory.rglob("id_ed25519")]
        self.assertTrue(private)
        for path in private:
            self.assertNotIn(path.read_text().strip(), spec)

    def test_credentials_are_stable_across_runs(self):
        before = self.credentials.public(list(self.lab.machines))
        self.credentials.ensure(list(self.lab.machines))
        self.assertEqual(before, self.credentials.public(list(self.lab.machines)))

    def test_public_values_are_placeholders_until_generated(self):
        public = Credentials(self.root / "none").public(list(self.lab.machines))
        self.assertEqual(public["ca"], PENDING)

    def test_only_listed_machines_are_authorized(self):
        public = self.credentials.public(list(self.lab.machines))
        spec = nix_spec(self.lab, SUBNETS, public)["machines"]
        self.assertEqual(
            set(spec["dev-gateway"]["authorizedKeys"]),
            {public["identities"]["dev-nullray"], public["identities"]["dev-admin"]},
        )
        self.assertEqual(spec["dev-admin"]["authorizedKeys"], [])
        self.assertEqual(spec["dev-client"]["authorizedKeys"], [])

    def test_vpn_clients_have_fixed_addresses(self):
        public = self.credentials.public(list(self.lab.machines))
        server = nix_spec(self.lab, SUBNETS, public)["machines"]["dev-nullray"]["vpn"]
        self.assertEqual(
            server["clients"], {"dev-gateway": "198.18.3.2", "dev-admin": "198.18.3.3"}
        )

    def test_museum_client_has_no_vpn_configuration(self):
        public = self.credentials.public(list(self.lab.machines))
        client = nix_spec(self.lab, SUBNETS, public)["machines"]["dev-client"]
        self.assertIsNone(client["vpn"])
        self.assertEqual(client["knownHosts"], {})


class IsolationTests(unittest.TestCase):
    def test_networks_have_no_forwarding_or_host_address(self):
        root = ET.fromstring(network_xml(NETWORK))
        self.assertIsNone(root.find("forward"))
        self.assertIsNone(root.find("ip"))

    def test_nics_use_lab_networks_never_host_bridges(self):
        root = ET.fromstring(domain_xml(LAB, MACHINE, TOPLEVEL, ""))
        nics = root.findall("devices/interface")
        self.assertEqual([nic.get("type") for nic in nics], ["network"])
        self.assertEqual(nics[0].find("source").get("network"), NETWORK.libvirt_name)

    def test_nix_store_is_mounted_read_only(self):
        root = ET.fromstring(domain_xml(LAB, MACHINE, TOPLEVEL, ""))
        store = root.find("devices/filesystem")
        self.assertIsNotNone(store.find("readonly"))

    def test_boot_files_never_point_into_the_nix_store(self):
        root = ET.fromstring(domain_xml(LAB, MACHINE, TOPLEVEL, ""))
        self.assertTrue(root.findtext("os/kernel").startswith(POOL_PATH))
        self.assertTrue(root.findtext("os/initrd").startswith(POOL_PATH))


class OwnershipTests(unittest.TestCase):
    def test_lab_resources_carry_a_marker(self):
        self.assertIsNotNone(owned_spec(network_xml(NETWORK)))
        self.assertIsNotNone(owned_spec(domain_xml(LAB, MACHINE, TOPLEVEL, "")))

    def test_marker_absent_from_foreign_xml(self):
        self.assertIsNone(owned_spec("<domain><name>athdev</name></domain>"))

    def test_refuses_a_foreign_domain_with_a_lab_name(self):
        virsh = provisioned_virsh()
        virsh.domain_defs[MACHINE.domain] = "<domain><name>x</name></domain>"
        with self.assertRaisesRegex(LabError, "does not own"):
            plan(virsh)

    def test_refuses_a_foreign_network_with_a_lab_name(self):
        virsh = provisioned_virsh()
        virsh.network_defs[NETWORK.libvirt_name] = "<network><name>x</name></network>"
        with self.assertRaisesRegex(LabError, "does not own"):
            plan(virsh)

    def test_refuses_a_foreign_pool_with_the_lab_name(self):
        virsh = provisioned_virsh()
        virsh.pool_defs[POOL] = "/somewhere/else"
        with self.assertRaisesRegex(LabError, "does not own"):
            plan(virsh)

    def test_fails_rather_than_share_an_existing_bridge(self):
        with self.assertRaisesRegex(LabError, "not the lab's"):
            plan(provisioned_virsh(), FakeHost({NETWORK.bridge}))

    def test_active_lab_network_may_own_its_bridge(self):
        virsh = provisioned_virsh()
        virsh.active_networks.add(NETWORK.libvirt_name)
        self.assertEqual(plan(virsh, FakeHost({NETWORK.bridge}))[0], [])


class PlanTests(unittest.TestCase):
    def test_matching_lab_has_nothing_to_do(self):
        self.assertEqual(plan(provisioned_virsh()), ([], []))

    def test_empty_host_gets_everything(self):
        virsh = FakeVirsh()
        steps, changed = plan(virsh)
        self.assertEqual(len(steps), 3)
        self.assertEqual(changed, [MACHINE])

    def test_changed_system_redefines_only_the_machine(self):
        virsh = provisioned_virsh()
        virsh.domain_defs[MACHINE.domain] = domain_xml(LAB, MACHINE, "/nix/store/old", "")
        steps, changed = plan(virsh)
        self.assertEqual(steps, [f"build and define {MACHINE.domain}"])
        self.assertEqual(changed, [MACHINE])

    def test_existing_disk_is_never_recreated(self):
        virsh = provisioned_virsh()
        virsh.domain_defs[MACHINE.domain] = domain_xml(LAB, MACHINE, "/nix/store/old", "")
        self.assertFalse([step for step in plan(virsh)[0] if "disk" in step])

    def test_kernel_parameters_do_not_change_the_spec(self):
        first = owned_spec(domain_xml(LAB, MACHINE, TOPLEVEL, ""))
        self.assertEqual(first, owned_spec(domain_xml(LAB, MACHINE, TOPLEVEL, "console=ttyS0")))

    def test_network_change_invalidates_every_machine(self):
        virsh = provisioned_virsh()
        virsh.network_defs[NETWORK.libvirt_name] = network_xml(Network("museum_a", "mnd-other"))
        steps, changed = plan(virsh)
        self.assertEqual(steps, [f"replace network {NETWORK.libvirt_name}"])
        self.assertEqual(changed, [MACHINE])


if __name__ == "__main__":
    unittest.main()

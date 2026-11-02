# Museum Net — System and Development Design

**Status:** Proposed design, v0.2  
**Date:** 2026-11-01  
**Supersedes:** *Museum Net — Portable Development Environment Design*, v0.1  
**Scope:** Virtual development environment, NixOS gateways, management, serial/Ethernet services, archive distribution, and DOS bootstrap media.

## 1. Purpose

Museum Net provides a controlled, portable network for vintage computers. Each gateway exposes useful local services to machines connected by Ethernet or serial links, without granting those machines unrestricted access to modern networks. Gateways can operate at home or independently at conventions.

Development happens first in an isolated virtual lab that can be reproduced on any supported Ubuntu workstation. The same NixOS gateway modules are then deployed to physical appliances.

### Agreed platforms

| Role | Platform |
|---|---|
| Development hosts | `athena`, `battlestation`, `onley`, or later Ubuntu 26.04 x86-64 machines |
| Production gateways | `twintwist`, `topspin` — NixOS, each with two Ethernet interfaces, Wi-Fi, and two serial ports |
| Virtual gateway | NixOS under QEMU/KVM/libvirt |
| Virtual VPN server | `dev-nullray`, NixOS under KVM |
| Production VPN server | `nullray`, Debian, existing OpenVPN server |
| Vintage test clients | 86Box primarily; QEMU and DOSBox-X where useful |
| Mirror builders | `ripley` or `jalad`, Ubuntu |
| Gateway management | Rust: `museumd`, `museumctl`, Axum-based web UI |

The gateway configuration must not assume the two production appliances have the same kernel device names. Discover and bind physical devices by stable identifiers.

## 2. Architectural principles

1. **Isolation by default.** Vintage-facing interfaces must not provide unrestricted routing, NAT, or bridging to management, home, VPN, or Internet networks.
2. **Offline operation.** Local FTP/HTTP archives and serial services remain useful if Wi-Fi, upstream Ethernet, or OpenVPN is down.
3. **One configuration authority.** Local CLI and web UI use the same validated management backend.
4. **Interfaces are resources.** Physical NICs, Wi-Fi, and serial ports have configurable roles. No permanent protocol-to-port coupling.
5. **Declarative base, controlled runtime.** NixOS provisions packages, base network security, services, and management infrastructure; `museumd` manages validated operational assignments without full rebuilds for routine changes.
6. **Reproducible development.** Development hosts are interchangeable; all host-specific state and proprietary media remain outside Git.
7. **Read-only archive delivery.** Museum clients can retrieve files, not modify the mirror.
8. **Transport-independent media.** The same archive manifest and content can be distributed by SSH/rsync or on a 1 TB USB thumb drive.
9. **Conservative changes.** Changes that risk management connectivity require confirmation and automatic rollback.

## 3. Topology

### Production (logical)

```text
                    nullray (Debian)
                    OpenVPN server
                          |
                  management VPN
                          |
                +---------+---------+
                |                   |
           twintwist             topspin
             NixOS                NixOS
                |                   |
      +---------+---------+   +-----+----------------+
      |         |         |   |      |                |
   Ethernet  Ethernet   COM1/2  Ethernet x2       COM1/2
   roles     roles      serial  assignable         serial
      |         |         |       |                  |
    switches / vintage Ethernet and serial clients
                |
      local read-only archive (FTP and plain HTTP)
```

Each gateway also has Wi-Fi. Management can use a wired Ethernet NIC or Wi-Fi; the default recommended deployment is one **wired management** NIC, one **museum Ethernet** NIC, and two independently configurable serial ports. Alternate supported layout: Wi-Fi management and both Ethernet NICs serving museum networks, either separate or bridged as an intentional configuration.

**A hub or switch is appropriate for multiple clients on a museum-facing NIC.** Do not connect a management and museum segment together through external switching. If both physical NICs are assigned to the same museum LAN, use an explicitly managed Linux bridge with loop prevention and topology validation.

### Virtual development lab

```text
Ubuntu host: athena / battlestation / onley
 |
 +-- isolated upstream/management virtual network
 |     +-- dev-nullray (NixOS): OpenVPN server, SSH
 |     +-- dev-gateway (NixOS): OpenVPN client, SSH, museumd
 |     +-- dev-admin (NixOS): separate management client over the VPN
 |
 +-- isolated museum Ethernet network
 |     +-- dev-gateway museum NIC
 |     +-- QEMU test client
 |     +-- 86Box vintage PC via TAP
 |
 +-- virtual serial transport
       +-- 86Box COM1/COM2 <-> PTY/TCP bridge <-> gateway serial devices
```

The gateway VM should model **two assignable Ethernet NICs plus an optional third management/Wi-Fi surrogate**. Virtual interfaces test role changes; they do not reproduce actual wireless driver behavior. All lab networks are non-forwarding, not connected to physical NICs, and have no host NAT by default. VPN traffic stays within the virtual upstream network.

Each lab network uses its own dedicated bridge, created and owned by the lab. Lab bridges are layer 2 only: they have no host address, so the host has no route into the lab. People reach machines through the libvirt console, and tests use the QEMU guest agent. Libvirt network names use the `museum-net-dev-` prefix; bridge device names need a shorter prefix such as `mnd-` because Linux limits interface names to 15 characters. The lab must never attach VMs, TAP devices, or emulators to existing host bridges (for example `virbr0`, `br0`, or Docker bridges) or to libvirt's `default` network. The 86Box TAP device joins the lab's museum bridge. `up` must fail rather than fall back to an existing bridge.

Use distinct subnets for upstream, museum LAN(s), and VPN. Addresses must be configurable and collision-checked; examples in documentation must not become hard-coded defaults. `up` uses subnets from local configuration, otherwise it allocates free /24s from 198.18.0.0/15 and keeps them stable for that host.

## 4. Portable Ubuntu development environment

### Host provisioning

The development environment supports:

```sh
./dev/scripts/doctor
./dev/scripts/bootstrap
./dev/scripts/up
./dev/scripts/down
./dev/scripts/update --dry-run
./dev/scripts/update
./dev/scripts/destroy
```

- `doctor` is **read-only**: check Ubuntu release, architecture, KVM, `/dev/kvm`, libvirt, permissions, network conflicts, storage, Nix, emulators, ROMs, and package inventory.
- `bootstrap` installs missing host dependencies with explicit privilege escalation/approval; it is idempotent and leaves unrelated host configuration alone.
- `up` launches managed libvirt networks, `dev-nullray`, gateway, and selected test clients.
- `down` stops managed resources without deleting disks.
- `update` reconciles generated definitions and NixOS configurations; supports dry-run and preserves mutable state.
- `destroy` only removes explicitly owned resources, with separate confirmation for destructive disk deletion.

`dev/lab.yaml` describes the lab's networks and machines; shared orchestration does not name them. Lab VMs boot directly from a NixOS kernel and initrd, and mount the host's Nix store read-only, so no disk image is built and a configuration change only needs a restart. Machine disks hold mutable state only.

Use the `museum-net-dev-` resource prefix and an ownership marker in each definition to avoid deleting other libvirt assets. All scripts support `--help`, clear exit statuses, and actionable diagnostics.

### Dependency policy

Prefer Ubuntu 26.04 APT packages. Candidate packages to validate against the actual release: `qemu-system-x86`, `qemu-utils`, `libvirt-daemon-system`, `libvirt-clients`, `virtinst`, `socat`, `iproute2`, `python3`, `git`, `curl`, `ca-certificates`, `jq`, plus optional `virt-manager`, `tcpdump`, `bind9-dnsutils`, and `netcat-openbsd`. The dependency manifest distinguishes required, optional, and test-only packages and records why each is needed.

Nix installation/build workflow must be explicit and documented; `flake.lock` pins NixOS inputs. Do not silently execute downloaded installers. Hosts use Ubuntu's `nix-bin` and `nix-setup-systemd` packages.

86Box is managed separately when not available through the supported Ubuntu repositories: use a verified upstream Linux artifact, pinned version and SHA-256; install into a user-owned tools directory. Do not invent release filenames or checksums. Firmware ROMs and operating-system images are user-supplied and excluded from the repository.

### Vintage client emulation

The lab exercises real period-appropriate clients as well as modern Linux tools. DOS and Windows 3.11 under 86Box are the initial clients, not the permanent set: Macintosh, Unix, CP/M, Apple II, and other systems are expected later.

| Environment | Machine configuration | Purpose |
|---|---|---|
| MS-DOS | `dev/machines/dos/` | Primary serial-protocol target: Interloper bootstrap, Sprocket, SLIP/PPP, and subsequent FTP/HTTP access |
| Windows for Workgroups 3.11 | `dev/machines/win311/` | Additional compatibility target: emulated Ethernet and serial networking, WinSock/TCP/IP applications, historical FTP clients, and early HTTP browsers such as Mosaic and Netscape. Reflects the physical IBM PS/2 Model 70 running DOS 6.22 and Windows 3.11 |

Git tracks emulator machine configuration only. ROMs, operating-system installation media, licensed software, and installed disk images are never committed; users supply them locally.

All emulated clients are optional for the basic gateway development workflow and ordinary CI. Lightweight Linux clients carry the mandatory automated service and isolation tests; emulated clients cover historical compatibility through manual or opt-in integration tests. Windows testing stays manual until a repeatable guest-side test harness exists.

Machine configurations attach only to the lab's dedicated isolated museum Ethernet network and virtual serial bridges, never to existing host bridges.

The client-launch and connectivity layer must not assume 86Box, x86 hardware, DOS or Windows, or a particular guest network stack:

- **Client descriptor:** each client is described by its emulator/launcher, user-supplied assets (ROMs, OS images), network attachment (isolated museum Ethernet, SLIP, PPP), serial transport (PTY/TCP bridge), and supported test capabilities (FTP, HTTP, Sprocket, Interloper, and so on).
- **Common emulator interface:** emulator-specific launch and configuration logic sits behind a common interface. Network and serial infrastructure is reusable by 86Box, QEMU, Mini vMac/Basilisk II, MAME, and future emulators where their capabilities permit.
- **Emulator and guest are separate:** one emulator can host several guests, and one guest may be supported through more than one emulator.
- **No hard-coded clients:** shared orchestration discovers clients from their descriptors rather than naming `dos` or `win311`. Paths may later be grouped by platform, such as `dev/machines/pc/dos/` and `dev/machines/mac/system6/`.

New clients are added incrementally, and stay out of the mandatory bootstrap and CI paths.

### Local state

| Location | Purpose |
|---|---|
| Git repository | NixOS modules, Rust source, provisioning, manifests, tests |
| `~/.config/museum-net/` | Local paths, subnet overrides, RAM/disk choices |
| `~/.local/share/museum-net/` | ROMs, emulator state, external tools, allocated lab subnets, throwaway lab credentials |
| `/var/lib/libvirt/museum-net-dev/` | VM disks and boot files, in a lab-owned libvirt pool that QEMU can read |
| `~/.cache/museum-net/` | Download/build cache |
| Managed libvirt instance | Generated VM/network definitions |

Respect XDG environment overrides. Do not commit machine-specific paths, credentials, ROMs, archive contents, or VM disks.

## 5. Gateway networking and interface roles

Model physical devices independently from logical network zones:

- **Management:** SSH, authenticated web portal, controlled API.
- **Museum LAN A/B:** Vintage-facing Ethernet networks; may be intentionally bridged or independent.
- **Serial point-to-point:** SLIP/PPP interfaces, assigned to museum zones/policies.
- **VPN management:** OpenVPN interface; administrative traffic only.
- **Disabled/unassigned:** No service and no unintended forwarding.

Default physical layout: wired NIC 1 = management, wired NIC 2 = museum LAN, Wi-Fi = disabled/optional management. Alternate layout: Wi-Fi = management, both wired NICs = museum LAN(s). A role change must be validated against policy and available recovery paths.

Gateway firewall must block museum clients from SSH, management HTTP(S), VPN endpoints, and other management services. It must block forwarding between museum, management, VPN, and physical upstream networks unless a narrowly scoped policy explicitly allows it. Cover IPv4 and IPv6, including bridging behavior; do not rely only on IP forwarding settings. Linux answers for any local address on any interface, so a museum interface accepts traffic only for its own addresses; otherwise a client that routes through the gateway could reach its management and VPN addresses with forwarding off.

For remote network reconfiguration, use a timed commit/confirm/rollback mechanism. Preserve local console and Unix-socket management even when all network interfaces are misconfigured.

## 6. OpenVPN and `dev-nullray`

Production gateways initiate OpenVPN connections to Debian `nullray` using separate per-gateway credentials. The gateway should have a predictable VPN IP, permit SSH over that IP, and optionally permit the authenticated management portal over the VPN. The VPN must reconnect automatically and must **not** provide a default route or forwarding path for vintage clients.

`dev-nullray` is a small NixOS VM that emulates the **observable OpenVPN and SSH behavior** of production `nullray`; it does not need to replicate Debian internals. It runs an OpenVPN server, SSH server, isolated test PKI, and appropriate routing/firewall rules. Never reuse production VPN keys or silently connect development gateways to production. `modules/vpn.nix` has no default server, so a host only reaches a VPN it is explicitly configured for.

Only clients the server lists can connect, and each gets a fixed address. All lab VMs can read the shared Nix store, so private keys never enter it: `up` generates throwaway credentials on the host and gives each machine only its own, on a read-only disk. Museum clients get none.

Integration tests must verify:
- Tunnel establishment and expected address assignment.
- SSH gateway → `dev-nullray`, `dev-nullray` → gateway, and a separate authorized management client → gateway through the VPN.
- Reconnection after server restart or link interruption.
- Museum services continue when VPN is unavailable.
- Museum clients cannot use or reach the VPN/management zone.
- Invalid credentials fail closed.

Any eventual test against production `nullray` must be explicitly enabled and separately documented.

## 7. Rust management architecture

### Components

| Component | Role |
|---|---|
| `museum-core` | Shared typed models, validation, configuration schemas |
| `museumd` | Privileged management service and single authority for operational configuration |
| `museumctl` | Local CLI using `clap` derive/subcommands |
| Web routes in `museumd` | Axum HTTP server, Askama templates, optional HTMX, minimal JavaScript/CSS |
| Protocol adapters | Lifecycle/configuration interface for serial services |
| `museum-mirror` | Separate Rust mirror-building/distribution tool for Ubuntu archive hosts |

Tokio, Serde, and `tracing` are preferred supporting libraries. No Node.js/SPA/WASM toolchain is required for the first portal. Basic administration should work with ordinary server-rendered forms; JavaScript can enhance status updates.

### Control plane

```text
museumctl -- Unix domain socket --+
                                  +--> museumd --> validation/state --> systemd/services
Web browser -- authenticated HTTP+
```

The local CLI communicates through a Unix socket, so it works even with no Ethernet, Wi-Fi, or VPN. The web portal exposes the same operations and state; it is available only on configured management interfaces and VPN, never museum interfaces. The web portal's management transport/authentication is distinct from the **plain HTTP archive** service.

Use Unix permissions and explicit authorization for mutations. Do not run an unrestricted shell command constructed from web/CLI input. Keep a durable change log and provide rollback.

The socket is `root:museum` mode 0660, so `museum` group membership decides who can read. Changes also need root or the `museum-admin` group, which `museumd` checks against the kernel-reported peer for each connection. Every front end calls the same operations, which know nothing about their transport, so the portal can't drift from the CLI.

Each revision is a complete, schema-versioned snapshot in its own file, and the highest number is current. A revision is fully on disk before it takes its final name, so a crash leaves the previous one current. Revisions are never rewritten: a rollback is a new revision, and can itself be rolled back. A damaged or newer-schema current revision stops `museumd` from starting, because running on an older configuration would silently undo a change.

A change is refused if it adds a problem, and existing problems don't block it. So when a port leaves the inventory, its stale entry is reported but other ports can still be changed.

### Configuration ownership

- NixOS: packages, base firewall boundaries, service units/templates, stable interface identification, serial port inventory, OpenVPN, management infrastructure.
- `museumd`: validated runtime serial assignments, baud rates, logical zone mappings within allowed policies, enabled service instances, archive selection/status.
- Changes must survive reboot, have schema versions, and be applied transactionally where possible.
- Changing a serial mode should not require rebuilding NixOS; changing foundational security policy may require a NixOS deployment.

### Proposed CLI

```sh
museumctl status
museumctl serial list
museumctl serial set com1 --mode slip --baud 38400
museumctl serial set com2 --mode sprocket --baud 57600
museumctl serial disable com1
museumctl network list
museumctl vpn status
museumctl archive status
museumctl archive verify
museumctl config show
museumctl config validate
museumctl config history
museumctl config rollback <revision>
```

M3 implements `status`, `serial`, and `config`, plus `serial adapters`, which lists the modes the daemon supports. `network`, `vpn`, and `archive` are design targets for the milestones that add their state. `clap` is a good fit for nested subcommands and shell completions. Avoid a fixed enum of all possible future serial protocols: the CLI should discover supported adapter names from the daemon.

## 8. Serial ports and protocol adapters

Each gateway has two serial ports, independently assignable to:

| Mode | Intended behavior |
|---|---|
| Disabled | No service owns the port |
| SLIP | Point-to-point IP connectivity over serial |
| PPP | Point-to-point IP connectivity with negotiated settings |
| Sprocket | File transfer and disk imaging service |
| Interloper | Bootstrap/disk-image delivery service |
| Future adapter | Additional protocols without redesigning management |

A serial port has one exclusive owner at a time. The adapter contract should support `validate`, `start`, `stop`, `status`, and capability/configuration discovery. Changing owner follows the activation contract below. Persist failures and diagnostics.

### Activation

Accepting a configuration and activating it are separate, and no API or interface may present one as the other:

| Concept | Meaning |
|---|---|
| Desired configuration | The validated, durable revision the operator requested |
| Active configuration | What the service controller has actually applied to the port |
| Runtime status | Whether the owning service is stopped, starting, running, stopping, unhealthy, or failed, with a message |

A change's reply reports that the desired configuration is stored, and lists the ports that aren't doing what it asks. M3 has no service controller, so every port is stopped with no active configuration, and a port assigned a mode is reported as stored but not active.

The controller added with the adapters follows this contract:

1. Validate the change and durably commit it as the desired configuration, so a crash mid-transition can be recovered.
2. Reconcile only the ports whose desired and active configurations differ, one transition per port at a time.
3. Stop the old owner and confirm the device is released before starting anything, so a port never has two owners.
4. Start the new service, health-check it, and only then report it active.
5. If activation fails, stop the new service, restart the previous active configuration, and commit a new revision by `museumd` restoring it, recording the failure and its diagnostics. If the previous configuration can't be restored either, leave the port stopped and report it failed.
6. At startup, adopt services that already match the desired configuration, and reconcile the rest. Restarting `museumd` doesn't disturb a healthy port.

Clients wait for the outcome of a transition, up to a timeout, and report failure with a nonzero exit.

Serial configuration includes stable device identity, baud, framing/flow control as appropriate, mode-specific settings, and optional user-facing connected-machine label. Distinguish physical UART behavior from virtual PTY/TCP emulation; test real hardware before claiming parity.

SLIP/PPP clients should be able to reach approved local archive services and nothing more by default. Sprocket and Interloper may have different device/server roles; verify actual project protocol capabilities before implementing service adapters.

## 9. Archive services: FTP and plain HTTP

Each gateway serves a **local, read-only mirror** of the user's FTP archive over:
- Traditional anonymous FTP (port 21, active and passive transfers, defined passive port range).
- **Plain HTTP** (port 80 or configured museum-facing port), compatible with HTTP/1.0 and HTTP/1.1 and simple directory/file downloads.
- **No HTTPS requirement, TLS upgrade, JavaScript requirement, or redirect to HTTPS** for museum clients.

Both services expose the same archive tree. Archive traffic is permitted from museum Ethernet, SLIP, and PPP; management APIs/SSH remain inaccessible from those zones. Archive services must not become public Internet servers or unintentionally listen on VPN/management interfaces.

Prefer established NixOS-packaged FTP/HTTP servers (e.g., vsftpd and a lightweight static server), not a custom Rust transfer server. Test vintage clients, active/passive FTP data channels, HTTP/1.0, and filenames. Filesystem permissions and mounts enforce read-only serving.

Gateway boot, serial service, VPN, and management must work when archive media is absent. FTP/HTTP should report archive unavailable instead of silently serving an empty unrelated directory.

## 10. Archive mirroring and 1 TB USB distribution

`museum-mirror` runs on either Ubuntu machine `ripley` or `jalad`. It is separate from `museumd`, although common manifest types can live in `museum-core`. The source archive path is configurable; do not assume where the master archive lives.

Supported workflows:
1. **Build directly to USB:** populate a 1 TB thumb drive from the source archive.
2. **Incremental USB update:** synchronize changes and verify without recopying unchanged content.
3. **Serve directly from USB:** attach a populated drive to a gateway; mount read-only and serve FTP/HTTP.
4. **Import from USB:** optionally copy and verify content to internal/other gateway storage.
5. **Network distribution:** optionally synchronize a mirror over rsync/SSH for smaller updates.

Suggested interface:

```sh
museum-mirror build --source /path/to/archive --destination /media/museum-usb
museum-mirror update --source /path/to/archive --destination /media/museum-usb
museum-mirror verify --destination /media/museum-usb
museumctl archive status
museumctl archive import /media/museum-usb
```

These are design targets. Default to ext4 for the Linux-only USB workflow, with stable UUID/label identification (e.g., `MUSEUM_ARCHIVE`), `nodev,nosuid,noexec`, and read-only gateway mounts. Avoid device-name assumptions such as `/dev/sdb1`. The mirror builder may mount the drive read-write; serving gateways should not.

Suggested media layout:

```text
MUSEUM_ARCHIVE/
  archive/                 # original mirrored tree, unchanged
  metadata/
    manifest.json          # format version, files, sizes, hashes
  museum-archive.json      # mirror identity and build metadata
```

Build manifests deterministically and verify integrity before activation. An interrupted update must not be advertised as complete. Atomic activation/staging should avoid requiring two full copies of a near-1 TB archive; define an incremental strategy with a completion marker and recovery journal. For direct-from-USB serving, if an update is interrupted while the drive is detached from the gateway, the previous valid state must be identifiable. A single thumb drive cannot provide two independent offline gateways simultaneously; each gateway needs its own copy/storage.

The archive should also include generated DOS bootstrap images and their release metadata.

## 11. MS-DOS Interloper bootstrap image

**Assumption:** The vintage computer already boots an installed MS-DOS system. The Interloper image is **non-bootable utility media**, not a DOS installer or replacement OS.

Purpose: bootstrap a DOS machine from Interloper into Sprocket or SLIP-based Museum Net access, with `tico16` available as a full-screen text editor.

Desired contents:
- Interloper bootstrap/client artifacts as required by the actual protocol.
- DOS Sprocket client.
- DOS SLIP tooling and configuration examples, with compatible/licensed stack chosen during implementation.
- `tico16`.
- `SETUP.BAT`, `README.TXT`, and simple serial/network examples.

Initial target: **360 KB FAT12 non-bootable image** for broad compatibility; measure actual binaries first. If it does not fit, provide a minimal 360 KB image and a fuller 720 KB or 1.44 MB image. Do not include MS-DOS system files or a bootable DOS variant by default.

Build images reproducibly from pinned, verified Sprocket/Interloper/tico16 artifacts and documented DOS networking dependencies. Respect licenses and redistribute only permitted binaries. Store generated images in the archive and expose default image selection through Interloper configuration, contingent on verifying its image-delivery interface.

Example design target:

```sh
museumctl interloper image list
museumctl interloper image default museum-bootstrap-360
```

Test image contents and file accessibility in 86Box, then on real XT/PS/2 hardware. No assumption that the client can boot from an Interloper image.

## 12. Repository layout

```text
museum-net/
  README.md
  docs/
    DESIGN.md
    security.md
    development.md
  flake.nix
  flake.lock
  Cargo.toml
  Cargo.lock
  crates/
    museum-core/
    museumd/
    museumctl/
    museum-mirror/
  hosts/
    dev-gateway/
    dev-nullray/
    twintwist/
    topspin/
  modules/
    gateway.nix
    firewall.nix
    networking.nix
    serial.nix
    vpn.nix
    archive.nix
    management.nix
  dev/
    packages.yaml
    tools.lock.yaml
    config.example.toml
    libvirt/
    scripts/
      doctor
      bootstrap
      up
      down
      update
      destroy
    machines/
      dos/
      win311/
  bootstrap/
    dos/
      README.TXT
      SETUP.BAT
      manifests/
    scripts/
  tests/
    provisioning/
    nixos/
    vpn/
    serial/
    archive/
    bootstrap/
```

Source code and declarative configuration are tracked; generated VM images, proprietary ROMs, production secrets, mirror content, and per-host settings are not.

## 13. Testing and acceptance criteria

### Host provisioning
- Clean install and rerun on `athena`, `battlestation`, and `onley`.
- Detect missing packages, KVM permissions, emulator ROMs, and subnet collisions.
- `update --dry-run` changes nothing; `destroy` does not touch unrelated resources.

### VPN/management
- `dev-nullray` OpenVPN tunnel comes up; stable VPN address; SSH from authorized test client works.
- VPN outage/restart does not interrupt archive or serial service.
- `museumctl` operates locally with no network connectivity.
- CLI and web portal observe identical state; bad changes roll back.

### Network/serial
- Ethernet/SLIP/PPP clients reach only approved local services.
- Changing COM1 does not disturb COM2.
- Port ownership is exclusive; failures restore previous mode.
- Firewall negative tests cover management, VPN, upstream, and IPv6.

### Archive
- Identical file bytes via FTP and HTTP; HTTP/1.0 works.
- Anonymous FTP is read-only; both active and passive transfers tested.
- Archive is accessible through Ethernet, SLIP, and PPP.
- USB build/update/verify works; incomplete updates are detected.
- Missing USB drive does not prevent gateway boot or administration.

### DOS bootstrap
- Image is FAT12 and **not bootable**.
- Contains the intended Sprocket, SLIP, Interloper, and tico16 artifacts within measured capacity.
- An already-booted DOS machine can use it to reach Sprocket or SLIP.
- Generated image matches the declared manifest and checksums.

## 14. Milestones

| Phase | Deliverable |
|---|---|
| M0 | Repository, dependency manifests, `doctor`, idempotent Ubuntu `bootstrap` |
| M1 | Isolated KVM/libvirt lab with NixOS `dev-gateway` and test client |
| M2 | NixOS `dev-nullray`, OpenVPN client/server, SSH-over-VPN tests |
| M3 | Rust `museum-core`, `museumd`, local `museumctl`, persisted config |
| M4 | Museum Ethernet, lightweight Linux test client, firewall and local FTP/HTTP fixtures, opt-in 86Box clients |
| M5 | Virtual serial adapters: SLIP, PPP, Sprocket, Interloper |
| M6 | `museum-mirror`, 1 TB USB build/update/import, archive serving |
| M7 | Reproducible non-bootable DOS Interloper bootstrap image |
| M8 | Axum/Askama web management portal and network-role management |
| M9 | Physical deployment on `twintwist` and `topspin`, hardware validation |

Phases can overlap, but M0–M3 should establish the reproducible and manageable foundation. The portal is deliberately later than the local CLI; its backend/API contract is designed early.

## 15. Open implementation decisions

| Topic | Decision still needed |
|---|---|
| Physical interface identifiers | Inventory NIC, Wi-Fi, and UART identities on both gateways |
| Default network addressing | Select non-conflicting production subnets and VPN IP allocation |
| 86Box | Confirm ROM needs, TAP and serial configuration |
| Protocol adapters | Verify Interloper/Sprocket invocation, image-serving semantics, and serial handoff |
| DOS SLIP stack | Select compatible binaries and confirm redistribution rights |
| Archive size | Measure actual archive against 1 TB USB capacity, filesystem overhead, and update headroom |
| Mirror update consistency | Design crash-safe incremental updates without requiring full duplicate storage |
| Production VPN | Confirm `nullray` OpenVPN addressing, routes, PKI, and SSH authorization |
| Management authentication | Choose HTTPS and authentication for web management; local authorization is decided (§7) |
| Archive HTTP/FTP | Choose packaged servers and validate vintage compatibility |
| Serial hardware | Verify physical UART capabilities and handshaking on `twintwist`/`topspin` |

## 16. Immediate next steps

1. Commit this design and initialize the NixOS/Cargo repository skeleton.
2. Implement `doctor` and a validated Ubuntu 26.04 package manifest.
3. Implement idempotent `bootstrap`; test it on two different development hosts.
4. Launch isolated `dev-gateway` and `dev-nullray` VMs with repeatable networking.
5. Prove OpenVPN, SSH-over-VPN, and isolation using automated tests.
6. Implement the local `museumctl`/`museumd` control path before the web UI.
7. Add the first museum Ethernet client and static archive fixture.

**Success criterion:** A fresh checkout on any supported Ubuntu host can reproduce a safe, isolated Museum Net lab; the same gateway software and configuration model can later run on `twintwist` and `topspin`.

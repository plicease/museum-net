# Development setup

The lab runs on Ubuntu 26.04 x86-64. Host setup is two commands:

```sh
./dev/scripts/doctor      # read-only: reports what is missing and how to fix it
./dev/scripts/bootstrap   # installs what is missing, after showing you the plan
```

`doctor` exits 0 if nothing failed, or 1 if at least one check failed. Warnings don't fail it.

`bootstrap` is safe to rerun, because its plan only contains what is still missing. Run it as your own user; it uses `sudo` for the APT and group steps and refuses to run as root.

| Flag | Effect |
|---|---|
| `--dry-run` | Show the plan and change nothing |
| `--yes` | Skip the confirmation prompt |
| `--optional` | Also install debugging aids |
| `--test` | Also install test dependencies |
| `--vintage` | Also install 86Box and what it needs |

Log out and back in after `bootstrap` adds you to a group. `doctor` warns until the new membership is active.

## What bootstrap changes

- Installs the packages in `dev/packages.yaml`, without APT recommends.
- Adds you to the `libvirt`, `kvm`, and `nix-users` groups.
- With `--vintage`, installs the tools pinned in `dev/tools.lock.yaml` to `~/.local/share/museum-net/tools/`. A download that doesn't match its pinned SHA-256 is discarded.

It doesn't change any other host configuration, including `/etc/nix/nix.conf` and existing libvirt networks.

## The lab

```sh
./dev/scripts/up                   # build, define, and start the lab
./dev/scripts/update --dry-run     # show what differs from this checkout
./dev/scripts/update               # apply it, restarting affected machines
./dev/scripts/down                 # stop the lab and keep its disks
./dev/scripts/destroy              # remove the lab, keeping its disks
./dev/scripts/destroy --disks      # also delete the disks
```

The first `up` downloads and builds NixOS, which can take a few minutes. Later runs only rebuild what changed. Stage new files with `git add` first, because Nix only sees tracked files.

`dev/lab.yaml` defines the machines and networks. Resources are named `museum-net-dev-*`, and each lab bridge is named `mnd-*`. `up` refuses to continue if a bridge or libvirt resource with one of those names exists and isn't the lab's.

Lab bridges have no host address, so you can't SSH to a machine from the host. Use the console instead, which logs in as root automatically. Press `Ctrl+]` to leave:

```sh
virsh -c qemu:///system console museum-net-dev-gateway
```

| Machine | Role |
|---|---|
| `dev-nullray` | OpenVPN and SSH server, standing in for production `nullray` |
| `dev-gateway` | Gateway, with a management NIC and two museum NICs |
| `dev-admin` | Separate management client, connected through the VPN |
| `dev-client` | Lightweight Linux client on a museum network |

From inside a machine, SSH uses the `museum-admin` user and the lab's own key. For example, from `dev-admin`, where 198.18.3.2 stands for the gateway's VPN address:

```sh
ssh -i /run/lab-secrets/id_ed25519 museum-admin@198.18.3.2
```

Your lab's addresses are in `~/.local/share/museum-net/lab/subnets.json`.

`up` generates a throwaway test CA and SSH keys in `~/.local/share/museum-net/lab/credentials/`. They persist between runs, and `destroy --disks` leaves them in place. Delete that directory and run `update` to replace them. Never use them outside the lab.

## Management

`dev-gateway` runs `museumd`, and `museumctl` talks to it over a Unix socket, so it works with every network interface down. From the gateway's console:

```sh
museumctl status
museumctl serial adapters                          # the modes this gateway supports
museumctl serial list
museumctl serial set com1 --mode slip --baud 38400
museumctl serial disable com1                      # keeps its line settings
museumctl config history
museumctl config rollback 2                        # records a new revision
```

Add `--json` for the daemon's reply as JSON. `museumctl` exits 0 on success and 1 if the request failed or `config validate` found problems.

Assigning a mode only stores and validates it for now. No adapter runs until the serial milestone, and `kind` shows whether a port is real hardware or virtual.

To read state a user needs the `museum` group. To change it they also need `museum-admin`, or to be root. Changes are recorded under their user name.

Revisions are JSON files in `/var/lib/museumd`, and the service logs to `journalctl -u museumd`. `museumd` won't start if the newest revision is damaged or comes from a newer version; the log names the file. Move that file aside to go back to the previous revision.

## Nix

Nix comes from Ubuntu's `nix-bin` and `nix-setup-systemd` packages, so no downloaded installer is involved. `flake.lock` pins nixpkgs. Flakes aren't enabled system-wide, so pass the feature flags on each call:

```sh
nix --extra-experimental-features 'nix-command flakes' flake check --no-build
```

Flakes only see files tracked by Git, so `git add` new files before evaluating.

## 86Box ROMs

86Box is optional: gateway development and the required automated tests use lightweight Linux clients. Install it with `bootstrap --vintage` to run the DOS and Windows for Workgroups 3.11 clients under `dev/machines/`.

ROMs are user-supplied and never committed, as are installation media, licensed software, and installed disk images. Install a ROM set at `~/.local/share/museum-net/roms/`, or set `paths.roms` in the local config. `doctor` warns until it finds one.

## Local configuration

Copy `dev/config.example.toml` to `~/.config/museum-net/config.toml` to set local paths and lab subnets. `doctor` checks configured subnets against host routes and each other. Local state follows the XDG environment variables.

## Testing

```sh
# Unit tests, against a fake host
PYTHONPATH=dev/lib python3 -m unittest discover -s tests/provisioning

# The Rust crates, including the real daemon over a real socket
cargo test --workspace

# Full bootstrap in a clean Ubuntu container; needs Docker and changes nothing on the host
tests/provisioning/clean-install.sh
```

A container has no KVM or running libvirt, so `clean-install.sh` expects those two `doctor` checks to fail and starts `nix-daemon` by hand. Run `doctor` on a real host to cover them.

To test the real lab, including isolation, the VPN, SSH, `museumd`, and its full lifecycle:

```sh
tests/nixos/lab_live.py
```

This needs KVM, libvirt, and Nix. It destroys the lab and its disks when it finishes, so don't run it against a lab you want to keep.

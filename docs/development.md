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

Log out and back in after `bootstrap` adds you to a group. `doctor` warns until the new membership is active.

## What bootstrap changes

- Installs the packages in `dev/packages.yaml`, without APT recommends.
- Adds you to the `libvirt`, `kvm`, and `nix-users` groups.
- Installs the tools pinned in `dev/tools.lock.yaml` to `~/.local/share/museum-net/tools/`. A download that doesn't match its pinned SHA-256 is discarded.

It doesn't change any other host configuration, including `/etc/nix/nix.conf` and existing libvirt networks.

## Nix

Nix comes from Ubuntu's `nix-bin` and `nix-setup-systemd` packages, so no downloaded installer is involved. `flake.lock` pins nixpkgs. Flakes aren't enabled system-wide, so pass the feature flags on each call:

```sh
nix --extra-experimental-features 'nix-command flakes' flake check --no-build
```

Flakes only see files tracked by Git, so `git add` new files before evaluating.

## 86Box ROMs

ROMs are user-supplied and never committed. Install a ROM set at `~/.local/share/museum-net/roms/`, or set `paths.roms` in the local config. `doctor` warns until it finds one.

## Local configuration

Copy `dev/config.example.toml` to `~/.config/museum-net/config.toml` to set local paths and lab subnets. `doctor` checks configured subnets against host routes and each other. Local state follows the XDG environment variables.

## Testing

```sh
# Unit tests, against a fake host
PYTHONPATH=dev/lib python3 -m unittest discover -s tests/provisioning

# Full bootstrap in a clean Ubuntu container; needs Docker and changes nothing on the host
tests/provisioning/clean-install.sh
```

A container has no KVM or running libvirt, so `clean-install.sh` expects those two `doctor` checks to fail and starts `nix-daemon` by hand. Run `doctor` on a real host to cover them.

#!/bin/sh
# Runs bootstrap against a clean Ubuntu container: nothing touches the host.
#
# A container has no KVM or systemd, so the KVM and libvirt doctor checks are
# expected to fail. nix-daemon is started by hand in place of its systemd unit.
# Everything else must pass.
#
# Usage: tests/provisioning/clean-install.sh [image]
set -eu

if [ "${1:-}" != --inside ]; then
    root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
    exec docker run --rm -v "$root:/src:ro" "${1:-ubuntu:26.04}" \
        sh /src/tests/provisioning/clean-install.sh --inside
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends sudo >/dev/null
useradd -m tester
echo 'tester ALL=(ALL) NOPASSWD:ALL' >/etc/sudoers.d/tester

as_tester() {
    sudo -u tester -H sh -c "cd /src && $1"
}
state() {
    sha256sum /var/lib/dpkg/status /etc/group | sha256sum
}
fail() {
    echo "FAILED: $1" >&2
    exit 1
}

echo '== doctor without Python explains itself'
as_tester ./dev/scripts/doctor 2>&1 | grep -q 'run ./dev/scripts/bootstrap' ||
    fail 'doctor did not point at bootstrap'

echo '== dry run changes nothing'
before=$(state)
as_tester './dev/scripts/bootstrap --dry-run'
[ "$before" = "$(state)" ] || fail 'dry run changed the system'

echo '== declining by having no terminal changes nothing'
if as_tester './dev/scripts/bootstrap </dev/null'; then
    fail 'bootstrap ran without confirmation'
fi
[ "$before" = "$(state)" ] || fail 'unconfirmed run changed the system'

echo '== clean install'
as_tester './dev/scripts/bootstrap --yes --optional --test'

echo '== Nix works unprivileged through the daemon, and the flake evaluates'
nix-daemon >/dev/null 2>&1 &
sleep 2
# Flakes only see tracked files, so stage them in a private copy.
# shellcheck disable=SC2016 # expanded by the tester's shell
as_tester 'mkdir "$HOME/repo" && tar -C /src --exclude=./target -cf - . | tar -C "$HOME/repo" -xf - &&
    cd "$HOME/repo" && git add -A &&
    nix --extra-experimental-features "nix-command flakes" flake check --no-build 2>&1'

echo '== doctor is read-only and passes outside KVM, libvirtd, and ROMs'
before=$(state)
output=$(as_tester ./dev/scripts/doctor) && status=0 || status=$?
echo "$output"
[ "$before" = "$(state)" ] || fail 'doctor changed the system'
[ "$status" -eq 1 ] || fail "doctor should exit 1 without KVM, got $status"
unexpected=$(echo "$output" | grep '^\[FAIL\]' |
    grep -vE '^\[FAIL\] (/dev/kvm|libvirt):') || true
[ -z "$unexpected" ] || fail "unexpected doctor failures: $unexpected"
echo "$output" | grep -q '^\[ OK \] tool 86box' || fail '86Box not verified'

echo '== rerun is a no-op'
output=$(as_tester './dev/scripts/bootstrap --yes --optional --test')
echo "$output"
echo "$output" | grep -q '^Nothing to do' || fail 'second run found work'
[ "$before" = "$(state)" ] || fail 'second run changed the system'

echo '== the 86Box AppImage is a runnable binary'
# shellcheck disable=SC2016 # expanded by the tester's shell
as_tester '"$HOME"/.local/share/museum-net/tools/86box/*/86Box.AppImage --appimage-version'

echo '== unit tests and lint'
as_tester 'PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=dev/lib python3 -m unittest discover -s tests/provisioning 2>&1'
as_tester 'shellcheck -x dev/scripts/doctor dev/scripts/bootstrap dev/lib/python.sh tests/provisioning/clean-install.sh'

echo 'clean install: PASS'

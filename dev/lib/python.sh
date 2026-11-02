# shellcheck shell=sh disable=SC2154 # $root is set by the sourcing script
# Sourced by dev/scripts. A clean Ubuntu install may have no Python at all, so
# the entry points stay in POSIX sh until the system Python is usable.

python_ready() {
    /usr/bin/python3 -c 'import sys, yaml; sys.exit(sys.version_info < (3, 11))' \
        2>/dev/null
}

run_python() {
    module=$1
    shift
    PYTHONPATH="$root/dev/lib" PYTHONDONTWRITEBYTECODE=1 \
        exec /usr/bin/python3 -m "museum_dev.$module" "$@"
}

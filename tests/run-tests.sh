#!/bin/sh
# Runs every check of the project: the Python unit tests (with the boot hook and libxkbcommon),
# the bats tests of the boot hook functions, ShellCheck and the ASCII check. Needs python3,
# bash 5.1+, libxkbcommon, xkeyboard-config, systemd's kbd-model-map, loadkeys, bats and
# ShellCheck (e.g. in a Fedora development qube; dom0 runs the Python tests alone).
set -eu
cd "$(dirname "$0")/.."

python3 -I -B -X dev -W error -m unittest discover -s tests -v
bats tests/hook.bats
shellcheck -x dracut/module-setup.sh dracut/qubes-scramble.sh tests/run-tests.sh tests/hook.bats
if LC_ALL=C grep -rnIP '[^\x00-\x7F]' qubes-plymouth-scramble qubes-plymouth-scramble.conf README.md LICENSE versions.txt \
    .gitignore docs dracut theme tests; then
    echo "run-tests: non-ASCII characters found" >&2
    exit 1
fi
echo "run-tests: all checks passed"

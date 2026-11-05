#!/usr/bin/env bats
# Unit tests for single functions of the boot hook (dracut/qubes-scramble.sh) with exact inputs
# and outputs; the Python suite runs the whole hook on a temporary root. Needs bats 1.5 or later.

bats_require_minimum_version 1.5.0

setup() {
    HOOK=$BATS_TEST_DIRNAME/../dracut/qubes-scramble.sh
    root=$BATS_TEST_TMPDIR
}

# Defines the named functions of the hook in the test shell without running the hook
load_functions() {
    local name

    for name in "$@"; do
        eval "$(sed -n "/^$name() {/,/^}/p" "$HOOK")"
        declare -F "$name" >/dev/null
    done
}

@test "plymouth_disabled matches the conditions of plymouth-start.service exactly" {
    load_functions plymouth_disabled
    mkdir -p "$root/proc"
    for cmdline in "rd.plymouth=0" "quiet plymouth.enable=0" "a rd.plymouth=0 b"; do
        echo "$cmdline" >"$root/proc/cmdline"
        plymouth_disabled
    done
    for cmdline in "quiet" "rd.plymouth=1" "plymouth.enable=00" "xrd.plymouth=0" "rd.plymouth=0x"; do
        echo "$cmdline" >"$root/proc/cmdline"
        run ! plymouth_disabled
    done
    rm "$root/proc/cmdline"
    run ! plymouth_disabled
}

@test "shuffle_digits always gives a permutation of 0-9" {
    local digit

    load_functions shuffle_digits
    for _ in {1..300}; do
        shuffle_digits
        [ "${#map}" -eq 10 ]
        for digit in {0..9}; do
            [ "${map//[^$digit]/}" = "$digit" ]
        done
    done
}

@test "write_symbols keeps the base layout and sets only digits, keypad and F1, F2, F8" {
    load_functions write_symbols
    # shellcheck disable=SC2034 # read by write_symbols, loaded from the hook
    map=7302915864 BASE_LAYOUT='de(nodeadkeys)'
    run write_symbols
    [ "$status" -eq 0 ]
    [ "$(grep -cF 'include "de(nodeadkeys)"' <<<"$output")" -eq 2 ]
    grep -qxF '    key <AE10> { [ 7 ] };' <<<"$output"
    grep -qxF '    key <AE01> { [ 3 ] };' <<<"$output"
    grep -qxF '    key <KP0> { [ NoSymbol, KP_7 ] };' <<<"$output"
    grep -qxF '    key <KP0> { [ NoSymbol, KP_0 ] };' <<<"$output"
    [ "$(grep -c '<AE' <<<"$output")" -eq 10 ]
    [ "$(grep -c '<KP' <<<"$output")" -eq 20 ]
    [ "$(grep -c '<FK0[128]> { \[ UF70[45B] \] }' <<<"$output")" -eq 3 ]
}

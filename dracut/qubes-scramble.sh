#!/usr/bin/bash
# Scrambles the digit keys of the disk password prompt for this boot (qubes-plymouth-scramble).
#
# Runs in the initramfs before plymouthd and systemd-vconsole-setup and writes only to /etc
# and /run there (RAM; systemd keeps /usr read-only in the initramfs), readable by root only:
#   /etc/xkb/symbols/qubes-scramble  XKB layout: group "basic" types permuted digits, group
#                                    "standard" normal ones; Caps Lock switches groups
#   /etc/vconsole.conf               XKB* settings (read only by plymouthd)
#   $RUNTIME_THEME_FILE              copy of the theme file with the map in [script-env-vars]
# With Plymouth disabled on the kernel command line and CONSOLE_SCRAMBLE=1 it scrambles the
# console keymap instead (/etc/qubes-scramble.map as KEYMAP_TOGGLE) and shows the map on the
# console. If a step fails, the finished steps are undone and the prompt stays stock. The map
# is never logged.
#
# "restore" runs once the disk is unlocked: it deletes the map files (/run survives the switch
# to the real root) and puts vconsole.conf and the console keymap back.
# QUBES_SCRAMBLE_ROOT prefixes all paths to check an unpacked initramfs; the running system's
# console is then left alone.
set -Eeuo pipefail

root=${QUBES_SCRAMBLE_ROOT:-}
conf=$root/etc/qubes-plymouth-scramble.conf
symbols=$root/etc/xkb/symbols/qubes-scramble
console_map=/etc/qubes-scramble.map
vconsole=$root/etc/vconsole.conf
backup=$vconsole.qubes-scramble-orig
BASE_LAYOUT='' THEME_FILE='' RUNTIME_THEME_FILE='' CONSOLE_SCRAMBLE=0 HINT=''

fail() {
    printf 'qubes-scramble: %s; the password prompt keeps standard digits\n' "$*" >&2
    exit 1
}

check_conf() {
    [[ -f $vconsole && -n $BASE_LAYOUT && $RUNTIME_THEME_FILE == /run/* ]] || fail "invalid $conf or no $vconsole"
}

undo() {
    [[ ! -f $backup ]] || mv -f "$backup" "$vconsole"
    rm -f "$symbols" "$symbols.tmp" "$root$console_map" "$root$console_map.tmp" "$vconsole.tmp" \
        "$root$RUNTIME_THEME_FILE" "$root$RUNTIME_THEME_FILE.tmp"
}

# Writes stdin to $1 atomically, readable by root only; new directories get the usual 0755
write_file() {
    mkdir -p "${1%/*}"
    (umask 077 && cat >"$1.tmp")
    mv -f "$1.tmp" "$1"
}

# Sets map to a random permutation of 0-9 (Fisher-Yates with rejection sampling of $SRANDOM,
# 32 bits from the kernel random number generator): character n is the digit key n types
shuffle_digits() {
    local digits=(0 1 2 3 4 5 6 7 8 9) i j swap

    for ((i = 9; i > 0; i--)); do
        j=$SRANDOM
        while ((j >= 4294967296 - 4294967296 % (i + 1))); do j=$SRANDOM; done
        j=$((j % (i + 1)))
        swap=${digits[i]} digits[i]=${digits[j]} digits[j]=$swap
    done
    printf -v map '%s' "${digits[@]}"
}

# Only the digit level is set; XKB keeps the other levels of the base layout. F1, F2 and F8
# type private-use characters (U+F704, U+F705, U+F70B), because plymouthd passes no function
# keys from keyboards it reads through XKB; the keypad overlay turns them into the stock hotkeys.
# Keys defined only in "basic" apply to "standard" too.
write_symbols() {
    local key

    printf 'default partial alphanumeric_keys keypad_keys\nxkb_symbols "basic" {\n    include "%s"\n' "$BASE_LAYOUT"
    for key in {0..9}; do
        printf '    key <AE%02d> { [ %s ] };\n' $((key ? key : 10)) "${map:key:1}"
        printf '    key <KP%d> { [ NoSymbol, KP_%s ] };\n' "$key" "${map:key:1}"
    done
    printf '    key <FK01> { [ UF704 ] };\n    key <FK02> { [ UF705 ] };\n    key <FK08> { [ UF70B ] };\n};\n'
    printf 'partial alphanumeric_keys keypad_keys\nxkb_symbols "standard" {\n    include "%s"\n' "$BASE_LAYOUT"
    for key in {0..9}; do
        printf '    key <KP%d> { [ NoSymbol, KP_%d ] };\n' "$key" "$key"
    done
    printf '};\n'
}

# Console keymap changes for the top row and keypad digit keys (Linux key codes); loaded by
# systemd-vconsole-setup after KEYMAP
write_console_map() {
    local key names=(zero one two three four five six seven eight nine)
    local top=(11 2 3 4 5 6 7 8 9 10) keypad=(82 79 80 81 75 76 77 71 72 73)

    for key in {0..9}; do
        printf 'plain keycode %d = %s\n' "${top[key]}" "${names[${map:key:1}]}"
        printf 'plain keycode %d = KP_%s\n' "${keypad[key]}" "${map:key:1}"
    done
}

# Replaces the settings the hook manages with the lines in the arguments; $a\ ends the kept
# lines with a newline even if the file does not
write_vconsole() {
    cat "$vconsole" >"$backup"
    {
        # shellcheck disable=SC1003 # '$a\' is the sed command, no quote escape
        sed -E -e '/^[[:space:]]*(XKB(LAYOUT|MODEL|VARIANT|OPTIONS)|KEYMAP_TOGGLE)[[:space:]]*=/d' -e '$a\' "$vconsole"
        printf '%s\n' "$@"
    } | write_file "$vconsole"
}

write_theme() {
    if grep -q '^\[script-env-vars\]' "$root$THEME_FILE"; then
        fail "$THEME_FILE already has [script-env-vars]"
    fi
    {
        cat "$root$THEME_FILE"
        printf '\n[script-env-vars]\nqubes_scramble_map=%s\n' "$map"
        # UTF-8 of the characters F1, F2 and F8 type (see write_symbols)
        printf 'qubes_scramble_f1=\357\234\204\nqubes_scramble_f2=\357\234\205\nqubes_scramble_f8=\357\234\213\n'
    } | write_file "$root$RUNTIME_THEME_FILE"
}

plymouth_disabled() {
    local cmdline=''

    [[ ! -r $root/proc/cmdline ]] || cmdline=" $(<"$root/proc/cmdline") "
    [[ $cmdline == *' plymouth.enable=0 '* || $cmdline == *' rd.plymouth=0 '* ]]
}

print_console_map() {
    local key keys='  key:    ' entered='  enters: '

    for key in 1 2 3 4 5 6 7 8 9 0; do
        keys+=" $key" entered+=" ${map:key:1}"
    done
    printf 'Disk password keys for this boot:\n%s\n%s\n' "$keys" "$entered" >/dev/console
}

scramble() {
    local plymouth=1

    [[ -z $HINT ]] || printf '%s\n' "$HINT"
    if plymouth_disabled; then
        plymouth=0
    fi
    # plymouthd reads keys through XKB; the console keymap matters only without plymouthd
    ((plymouth || CONSOLE_SCRAMBLE)) || return 0
    [[ ${SRANDOM:-} =~ ^[0-9]+$ ]] || fail 'bash does not provide SRANDOM'
    check_conf

    trap undo EXIT
    shuffle_digits
    if ((plymouth)); then
        write_symbols | write_file "$symbols"
        write_vconsole XKBLAYOUT=qubes-scramble,qubes-scramble XKBMODEL=pc105 XKBVARIANT=basic,standard \
            XKBOPTIONS=grp:caps_toggle,grp_led:caps
        write_theme
    else
        write_console_map | write_file "$root$console_map"
        write_vconsole "KEYMAP_TOGGLE=$console_map"
    fi
    trap - EXIT

    if ((!plymouth)) && [[ -z $root ]]; then
        print_console_map
    fi
}

# Prints the console keymap of vconsole.conf: the last KEYMAP assignment counts, as in
# systemd's env-file parser
console_keymap() {
    local keymap

    keymap=$(sed -n -E 's/^[[:space:]]*KEYMAP[[:space:]]*=[[:space:]]*//p' "$vconsole" | tr -d '"'"'")
    printf '%s' "${keymap##*$'\n'}"
}

restore() {
    local keymap

    check_conf
    rm -f "$symbols" "$root$RUNTIME_THEME_FILE"
    rmdir "$root${RUNTIME_THEME_FILE%/*}" "$root${RUNTIME_THEME_FILE%/*/*}" 2>/dev/null || true
    [[ -f $backup ]] || return 0
    mv -f "$backup" "$vconsole"
    [[ -f $root$console_map ]] || return 0
    rm -f "$root$console_map"
    keymap=$(console_keymap)
    # systemd-vconsole-setup does not run again after unlocking
    if [[ -z $root && -n $keymap ]]; then
        loadkeys -q "$keymap"
    fi
}

# shellcheck source=/dev/null
. "$conf"
case ${1:-scramble} in
scramble) scramble ;;
restore) restore ;;
*) fail "unknown action '$1'" ;;
esac

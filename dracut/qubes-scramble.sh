#!/usr/bin/bash
# Scrambles the digit keys of the disk password prompt for this boot (qubes-plymouth-scramble).
#
# Runs in the initramfs before plymouthd and writes only to /etc and /run there (RAM; systemd
# keeps /usr read-only in the initramfs), readable by root only:
#   /etc/xkb/symbols/qubes-scramble  XKB layout: group "basic" types permuted digits, group
#                                    "standard" normal ones; Caps Lock switches groups
#   /etc/vconsole.conf               XKB* settings, read only by plymouthd; the console keymap
#                                    (KEYMAP) stays as it is
#   $RUNTIME_THEME_FILE              copy of the theme file with the map in [script-env-vars]
# With Plymouth disabled on the kernel command line it changes nothing. If a step fails, the
# finished steps are undone and the prompt stays stock. The map is never logged.
#
# "restore" runs once the disk is unlocked: it deletes the map files (/run survives the switch
# to the real root) and puts vconsole.conf back.
# QUBES_SCRAMBLE_ROOT prefixes all paths to check an unpacked initramfs.
set -Eeuo pipefail

root=${QUBES_SCRAMBLE_ROOT:-}
conf=$root/etc/qubes-plymouth-scramble.conf
symbols=$root/etc/xkb/symbols/qubes-scramble
vconsole=$root/etc/vconsole.conf
backup=$vconsole.qubes-scramble-orig
BASE_LAYOUT='' THEME_FILE='' RUNTIME_THEME_FILE=''

fail() {
    printf 'qubes-scramble: %s; the password prompt keeps standard digits\n' "$*" >&2
    exit 1
}

check_conf() {
    [[ -f $vconsole && -n $BASE_LAYOUT && $RUNTIME_THEME_FILE == /run/* ]] || fail "invalid $conf or no $vconsole"
}

undo() {
    [[ ! -f $backup ]] || mv -f "$backup" "$vconsole"
    rm -f "$symbols" "$symbols.tmp" "$vconsole.tmp" "$root$RUNTIME_THEME_FILE" "$root$RUNTIME_THEME_FILE.tmp"
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

# Replaces the XKB* settings of vconsole.conf with the scrambled layout and keeps every other
# line; $a\ ends the kept lines with a newline even if the file does not
write_vconsole() {
    cat "$vconsole" >"$backup"
    {
        # shellcheck disable=SC1003 # '$a\' is the sed command, no quote escape
        sed -E -e '/^[[:space:]]*XKB(LAYOUT|MODEL|VARIANT|OPTIONS)[[:space:]]*=/d' -e '$a\' "$vconsole"
        printf '%s\n' XKBLAYOUT=qubes-scramble,qubes-scramble XKBMODEL=pc105 XKBVARIANT=basic,standard \
            XKBOPTIONS=grp:caps_toggle,grp_led:caps
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

# The kernel command line options that keep plymouth-start.service from running
plymouth_disabled() {
    local cmdline=''

    [[ ! -r $root/proc/cmdline ]] || cmdline=" $(<"$root/proc/cmdline") "
    [[ $cmdline == *' plymouth.enable=0 '* || $cmdline == *' rd.plymouth=0 '* ]]
}

scramble() {
    if plymouth_disabled; then
        return 0
    fi
    [[ ${SRANDOM:-} =~ ^[0-9]+$ ]] || fail 'bash does not provide SRANDOM'
    check_conf

    trap undo EXIT
    shuffle_digits
    write_symbols | write_file "$symbols"
    write_vconsole
    write_theme
    trap - EXIT
}

restore() {
    check_conf
    rm -f "$symbols" "$root$RUNTIME_THEME_FILE"
    rmdir "$root${RUNTIME_THEME_FILE%/*}" "$root${RUNTIME_THEME_FILE%/*/*}" 2>/dev/null || true
    [[ ! -f $backup ]] || mv -f "$backup" "$vconsole"
}

# shellcheck source=/dev/null
. "$conf"
case ${1:-scramble} in
scramble) scramble ;;
restore) restore ;;
*) fail "unknown action '$1'" ;;
esac

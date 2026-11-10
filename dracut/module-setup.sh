#!/usr/bin/bash
# dracut module of qubes-plymouth-scramble: per-boot scrambled digit keys for the Plymouth
# disk password prompt. The RPM installs the module; a completed theme enables it. If anything
# does not match the validated installation, the image keeps the stock prompt.
# dracut provides moddir, initdir, systemdsystemunitdir and DRACUT_TMPDIR; the conf sets the rest.
# shellcheck disable=SC2154

# XKB file for the grp_led:caps option that plymouth-populate-initrd does not install
QUBES_SCRAMBLE_XKB_EXTRA=/usr/share/X11/xkb/compat/grp_led
QUBES_SCRAMBLE_THEME=/var/lib/qubes-plymouth-scramble/theme
QUBES_SCRAMBLE_TOOL=/usr/sbin/qubes-plymouth-scramble

# Without an applied theme dracut skips the module
check() {
    [[ -f $QUBES_SCRAMBLE_THEME/keypad.script ]]
}

depends() {
    echo plymouth
}

# Prints why the feature cannot be added to this image, or nothing. The theme script itself is
# checked by validate-tree.
qubes_scramble_blocker() {
    if [[ ! -f $initdir$THEME_FILE || ! -f $initdir$THEME_SCRIPT ]]; then
        echo "theme files $THEME_FILE or $THEME_SCRIPT are not in the image"
    elif grep -q '^\[script-env-vars\]' "$initdir$THEME_FILE" || ! grep -q '^ScriptFile=/' "$initdir$THEME_FILE"; then
        echo "$THEME_FILE has [script-env-vars] or no absolute ScriptFile"
    elif [[ ! -f $initdir/usr/share/X11/xkb/rules/evdev || ! -f $QUBES_SCRAMBLE_XKB_EXTRA ]]; then
        echo "XKB data is not in the image"
    elif [[ -e $initdir/etc/xkb/symbols/qubes-scramble || -e $initdir$RUNTIME_THEME_FILE ]]; then
        echo "the image already has /etc/xkb/symbols/qubes-scramble or $RUNTIME_THEME_FILE"
    elif grep -q '^qubes_scramble.sets = [1-9]' "$QUBES_SCRAMBLE_THEME/keypad.script" &&
        [[ ! -f $QUBES_SCRAMBLE_THEME/qubes-scramble-tiles.json ]]; then
        echo "the masked digit tiles are missing in $QUBES_SCRAMBLE_THEME"
    fi
}

install() {
    local THEME_FILE='' RUNTIME_THEME_FILE='' THEME_SCRIPT=''
    local blocker image link unitdir=$initdir$systemdsystemunitdir
    local links=(sysinit.target.wants/qubes-scramble.service
        {initrd,emergency,rescue}.target.wants/qubes-scramble-restore.service)
    local stock_script=$DRACUT_TMPDIR/qubes-scramble-theme-script
    local conf=$DRACUT_TMPDIR/qubes-plymouth-scramble.conf

    # The tool names its reason, such as an unsupported keyboard layout
    if ! "$QUBES_SCRAMBLE_TOOL" module-conf >"$conf"; then
        dwarn "qubes-plymouth-scramble: $QUBES_SCRAMBLE_TOOL module-conf failed; the password prompt stays stock"
        return 0
    fi
    # shellcheck source=/dev/null
    . "$conf"
    blocker=$(qubes_scramble_blocker)
    if [[ -n $blocker ]]; then
        dwarn "qubes-plymouth-scramble: $blocker; the password prompt stays stock"
        return 0
    fi

    inst_multiple bash sed grep cat mv rm rmdir mkdir
    inst_simple "$QUBES_SCRAMBLE_XKB_EXTRA"
    inst_simple "$conf" /etc/qubes-plymouth-scramble.conf
    inst_script "$moddir/qubes-scramble.sh" /usr/libexec/qubes-plymouth-scramble/qubes-scramble.sh
    inst_simple "$moddir/qubes-scramble.service" "$systemdsystemunitdir/qubes-scramble.service"
    inst_simple "$moddir/qubes-scramble-restore.service" "$systemdsystemunitdir/qubes-scramble-restore.service"
    # Complete theme assets, retained in dom0 for every later initramfs build
    for image in "$QUBES_SCRAMBLE_THEME"/qubes-scramble-*.png "$QUBES_SCRAMBLE_THEME"/qubes-scramble-tiles.json; do
        if [[ -f $image ]]; then
            inst_simple "$image" "${THEME_SCRIPT%/*}/${image##*/}"
        fi
    done

    # The keypad overlay goes into the image's copy of the theme script only
    cp "$initdir$THEME_SCRIPT" "$stock_script"
    cat "$QUBES_SCRAMBLE_THEME/keypad.script" >>"$initdir$THEME_SCRIPT"
    for link in "${links[@]}"; do
        mkdir -p "$unitdir/${link%/*}"
        ln -sf "../${link##*/}" "$unitdir/$link"
    done

    if ! "$QUBES_SCRAMBLE_TOOL" validate-tree "$initdir"; then
        dwarn "qubes-plymouth-scramble: image validation failed; the password prompt stays stock"
        for link in "${links[@]}"; do
            rm -f "$unitdir/$link"
        done
        cp "$stock_script" "$initdir$THEME_SCRIPT"
    fi
    return 0
}

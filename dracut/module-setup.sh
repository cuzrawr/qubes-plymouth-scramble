#!/bin/bash
# dracut module of qubes-plymouth-scramble: per-boot scrambled digit keys for the Plymouth
# disk password prompt. qubes-plymouth-scramble writes this directory: this file, the files
# made from its configuration, and in source/ the tool itself with its templates. If anything
# does not match the validated installation, the image keeps the stock prompt.
# dracut provides moddir, initdir, systemdsystemunitdir and DRACUT_TMPDIR; the conf sets the rest.
# shellcheck disable=SC2154

# XKB file for the grp_led:caps option that plymouth-populate-initrd does not install
QUBES_SCRAMBLE_XKB_EXTRA=/usr/share/X11/xkb/compat/grp_led

# Without the configuration file (enabled = no) dracut skips the module
check() {
    [[ -f $moddir/qubes-plymouth-scramble.conf ]]
}

depends() {
    echo plymouth
}

# Prints why the feature cannot be added to this image, or nothing. The theme script itself is
# checked by validate-tree.
qubes_scramble_blocker() {
    if [[ ! -x $src/qubes-plymouth-scramble ]]; then
        echo "$src/qubes-plymouth-scramble is missing"
    elif [[ ! -f $initdir$THEME_FILE || ! -f $initdir$THEME_SCRIPT ]]; then
        echo "theme files $THEME_FILE or $THEME_SCRIPT are not in the image"
    elif grep -q '^\[script-env-vars\]' "$initdir$THEME_FILE" || ! grep -q '^ScriptFile=/' "$initdir$THEME_FILE"; then
        echo "$THEME_FILE has [script-env-vars] or no absolute ScriptFile"
    elif [[ ! -f $initdir/usr/share/X11/xkb/rules/evdev || ! -f $QUBES_SCRAMBLE_XKB_EXTRA ]]; then
        echo "XKB data is not in the image"
    elif [[ -e $initdir/etc/xkb/symbols/qubes-scramble || -e $initdir$RUNTIME_THEME_FILE ]]; then
        echo "the image already has /etc/xkb/symbols/qubes-scramble or $RUNTIME_THEME_FILE"
    fi
}

install() {
    local THEME_FILE='' RUNTIME_THEME_FILE='' THEME_SCRIPT='' CONSOLE_SCRAMBLE=0
    local blocker image link src=$moddir/source unitdir=$initdir$systemdsystemunitdir
    local links=(sysinit.target.wants/qubes-scramble.service
        {initrd,emergency,rescue}.target.wants/qubes-scramble-restore.service)
    local stock_script=$DRACUT_TMPDIR/qubes-scramble-theme-script

    # shellcheck source=/dev/null
    . "$moddir/qubes-plymouth-scramble.conf"
    blocker=$(qubes_scramble_blocker)
    if [[ -n $blocker ]]; then
        dwarn "qubes-plymouth-scramble: $blocker; the password prompt stays stock"
        return 0
    fi

    inst_multiple bash sed grep cat mv rm rmdir mkdir tr
    inst_simple "$QUBES_SCRAMBLE_XKB_EXTRA"
    inst_simple "$moddir/qubes-plymouth-scramble.conf" /etc/qubes-plymouth-scramble.conf
    inst_script "$src/dracut/qubes-scramble.sh" /usr/libexec/qubes-plymouth-scramble/qubes-scramble.sh
    inst_simple "$src/dracut/qubes-scramble.service" "$systemdsystemunitdir/qubes-scramble.service"
    inst_simple "$src/dracut/qubes-scramble-restore.service" "$systemdsystemunitdir/qubes-scramble-restore.service"
    for image in "$src"/theme/qubes-scramble-*.png; do
        inst_simple "$image" "${THEME_SCRIPT%/*}/${image##*/}"
    done
    if ((CONSOLE_SCRAMBLE)); then
        inst_multiple loadkeys
    fi

    # The keypad overlay goes into the image's copy of the theme script only
    cp "$initdir$THEME_SCRIPT" "$stock_script"
    cat "$moddir/keypad.script" >>"$initdir$THEME_SCRIPT"
    for link in "${links[@]}"; do
        mkdir -p "$unitdir/${link%/*}"
        ln -sf "../${link##*/}" "$unitdir/$link"
    done

    if ! "$src/qubes-plymouth-scramble" validate-tree "$initdir"; then
        dwarn "qubes-plymouth-scramble: image validation failed; the password prompt stays stock"
        for link in "${links[@]}"; do
            rm -f "$unitdir/$link"
        done
        cp "$stock_script" "$initdir$THEME_SCRIPT"
    fi
    return 0
}

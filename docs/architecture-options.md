# Scrambled LUKS PIN prompt: architecture decision

Status: decided 2026-10-28. Constraint: minimal intrusion into the stock Qubes unlock path; any
failure of the feature falls back to the stock prompt; uninstall returns a vanilla system.

## Decision

A dracut module adds a boot hook and a theme overlay to the initramfs. The hook writes a
per-boot XKB layout (scrambled group plus standard group, Caps Lock toggles) on top of the
system keyboard layout and hands the map to the theme through `[script-env-vars]`; a second
unit deletes the map once the disk is unlocked. Plymouth, systemd, GRUB, LUKS and the files on
disk stay stock; all boot-time state lives in initramfs RAM.

Rejected options:

| Option | Reason |
| --- | --- |
| Key provider socket in crypttab | stale socket blocks unlock without fallback; consumes a `tries=` attempt |
| Replacement password agent | a hung agent leaves no way to unlock in that boot |
| Patched plymouthd (per-key reshuffle) | forks a core package, rebuilt for every update (stopped patch kept elsewhere) |
| Generator rebuilding the initramfs every boot | cannot remap keys; writes /boot on every boot |

## Verified facts the design relies on (Qubes R4.3 dom0)

- Versions: plymouth 24.004.60-14.fc41 (stock Fedora), systemd 256.17, dracut 103, libxkbcommon 1.7.0,
  xkeyboard-config 2.42, bash 5.2.32, python3 3.13.9, qubes-artwork-plymouth 4.3.9.
- Unlock path: rd.luks.uuid -> systemd-cryptsetup -> `systemd-tty-ask-password-agent --plymouth`; the console
  agent is skipped while plymouthd runs.
- Plymouth reads keys through XKB/evdev only if vconsole.conf has XKBLAYOUT (ply-device-manager.c:852), else the
  tty with the kernel keymap. With XKB it passes no function keys, so F1/F2/F8 type private-use characters
  that the overlay maps back to the stock hotkeys. XKB merges per level: letters, symbols and Shift levels of
  the base layout stay; Caps Lock becomes the group switch and no longer capitalizes.
- Themes get `[script-env-vars]` of their .plymouth file as globals (script plugin.c:154); plymouthd searches
  `/run/plymouth/themes` first. /run survives the switch to the real root, so the restore unit deletes the
  map; the hook makes its files readable by root only and keeps directory modes stock.
- Plymouth script assignment makes a reference (script-object.c:399) and reading a missing hash element creates
  it (script-execute.c:98): the overlay writes only through aliases of existing objects and reads
  `global.dialog` only after the stock prompt callback created it.
- `Plymouth.GetCapslockState()` is the XKB "Caps Lock" LED (ply-input-device.c:494); `grp_led:caps` ties it to
  the group switch. plymouth-populate-initrd does not install `compat/grp_led`; the module does.
- systemd keeps /usr read-only in the initramfs; libxkbcommon also searches `/etc/xkb`.
- The system keyboard layout is the first XKBLAYOUT of vconsole.conf, else the layout systemd's
  `kbd-model-map` gives for the console KEYMAP (as localectl converts it).
- Xen loads a single initrd module; a theme script parse error gives a blank screen with working input.
- `$SRANDOM` reads getrandom(GRND_NONBLOCK) with a glibc arc4random fallback; the kernel logged `crng init done`
  before the hook ran on both test systems.
- Kernel packages build the initramfs through `kernel-install add` -> `50-dracut.install`, which runs
  `dracut -f --kernel-image /boot/vmlinuz-K [--no-uefi] --kver K` (`--no-uefi` for the bls layout); the other
  plugins rewrite the GRUB and Xen configuration. In the `other` layout the plugin passes an empty argument
  that dracut takes as the output file, so it always writes `/boot/initramfs-K.img`; the tool therefore asks
  `kernel-install inspect` for the layout and calls dracut with the plugin's arguments and its own output file.
- The installer-made initramfs differs from any later build (hostonly picked up the installer's loaded
  drivers); a stock image is defined as what kernel-install builds on the installed system.

## Theme compatibility

The overlay needs from `qubes-dark.script`: the functions `display_prompt_callback`,
`display_normal_callback`, `refresh_callback` and `keyboard_input_callback` with their
registrations, `global.dialog.entry` with `sprite` and `image`, and the F1/F2/F8 escape sequences;
the script must not set an input filter itself. Install and every dracut build check these
structurally, so colour, image and position changes of the theme need no new version of the tool.

## Install model

- Footprint: the module directory (with a copy of the tool in `source/`), `/etc/qubes-plymouth-scramble.conf`,
  and a link in `/usr/local/sbin`. Every change is listed and confirmed before it is made.
- Atomic switch: new images are built to temporary files, unpacked and validated; the module directory is
  switched by rename and rolled back, and the settings file removed again, if any image fails. A lock on the
  modules directory prevents concurrent runs; SIGTERM and SIGHUP run the same rollback.
- Uninstall rebuilds the images with the kernel-install dracut call; no image copies are kept.
- Console scrambling applies only with Plymouth disabled on the kernel command line, when the console shows
  the map; with plymouthd running the console keymap is not used for the prompt and stays stock.

## Not covered

AEM, several LUKS volumes, TPM2 PIN, keypad rendering with other display resolutions and graphics drivers.

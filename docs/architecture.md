# Architecture

A dracut module adds a boot hook and a keypad overlay to the initramfs. The hook gives the
Plymouth password prompt a new random digit layout on every boot; the overlay shows the mapping.
Plymouth, systemd, GRUB, LUKS and the files on disk stay stock, and all boot-time state lives
in the initramfs (RAM). Any failure of the feature leaves the stock prompt.

## Files

| Path | Content |
| --- | --- |
| `/usr/lib/dracut/modules.d/91qubes-scramble/` | the dracut module: `module-setup.sh`, the generated settings, the overlay, the tiles, and the tool with its templates in `source/` |
| `/etc/qubes-plymouth-scramble.conf` | the user's settings |
| `/usr/local/sbin/qubes-plymouth-scramble` | link to the tool in the module |

In each initramfs the module installs:

- `qubes-scramble.sh`, run by `qubes-scramble.service` before `plymouth-start.service`;
- `qubes-scramble-restore.service`, after `cryptsetup.target`;
- the overlay, appended to the image's copy of `qubes-dark.script`;
- `compat/grp_led` of xkeyboard-config, which plymouth-populate-initrd leaves out;
- for masked digits, the tile PNGs and their manifest.

## Boot

1. plymouthd reads keys through XKB when `/etc/vconsole.conf` names an XKB layout. The hook
   writes, readable by root only:
   - `/etc/xkb/symbols/qubes-scramble`: the system keyboard layout with permuted digits in
     group `basic` and normal ones in group `standard`;
   - `XKB*` lines in `/etc/vconsole.conf`, which systemd's console setup ignores, so the console
     keymap stays stock;
   - a copy of the theme file with the map in `[script-env-vars]` under
     `/run/plymouth/themes`, where plymouthd looks first.
2. Caps Lock switches the groups (`grp:caps_toggle`); its LED (`grp_led:caps`) lights the dot.
3. plymouthd passes no function keys from XKB keyboards, so F1, F2 and F8 type private-use
   characters. The overlay runs the stock hotkeys for them and keeps them out of the password.
4. The overlay wraps the stock callbacks and calls them first, so the stock prompt is unchanged.
5. Once the disk is unlocked, the restore unit deletes the map (`/run` survives the switch to
   the real root) and restores `vconsole.conf`.
6. With `plymouth.enable=0` or `rd.plymouth=0` the hook changes nothing: the text console
   prompt keeps standard digits.

The map is a Fisher-Yates permutation drawn from `$SRANDOM` (getrandom) and is never logged.

## Install and uninstall

- `install` reads the settings and the system keyboard layout: the first `XKBLAYOUT` of
  `vconsole.conf`, else the layout systemd's `kbd-model-map` gives for `KEYMAP`.
- It builds every affected image with the dracut call of kernel-install's `50-dracut.install`
  (`--no-uefi` in the bls layout) into a temporary file.
- `validate-tree` unpacks each image and runs the hook and its restore step on a copy. It
  compiles the resulting keymap with libxkbcommon, checks Caps Lock and the F-keys, checks that
  the hook changes nothing without Plymouth, and checks the tiles against their manifest.
- The images are replaced only when all pass; otherwise the module, the settings and the images
  stay as before. A lock prevents concurrent runs; SIGTERM and SIGHUP roll back.
- Later initramfs builds (kernel updates) include the module and repeat the validation. A
  failure gives a stock image and a dracut warning.
- `uninstall` removes the three paths and rebuilds the images with the same dracut call. No
  image copies are kept.

## Theme compatibility

The overlay needs from `qubes-dark.script`:

- the functions `display_prompt_callback`, `display_normal_callback`, `refresh_callback` and
  `keyboard_input_callback`, with their registrations;
- `global.dialog.entry` with `sprite` and `image`;
- the F1/F2/F8 escape sequences;
- no input filter of its own.

These are checked at install and in every build. Colours, images and positions may change.

## Masked digits

After IllusionPIN (Papadopoulos et al., arXiv 1708.06508), with the paper's names.

- **Composition.** A tile is pedestal + alpha * high-pass(real digit) + beta * low-pass(decoy),
  in linear light, encoded as sRGB. Tiles are opaque, so no theme colour enters.
  - `highpass`: no decoy.
  - `hybrid`: the decoy is the key's standard digit (the paper's shoulder-surfer keypad).
  - `white`: the decoy is a white button.
- **High-pass filter.**
  - `tapered`: a raised-cosine step that passes nothing below the cut-off and everything above
    twice it.
  - `gaussian`: the paper's `1 - G`.
- **Cut-off.**
  - `auto`: the paper's visibility algorithm (distance-as-filtering low-pass, per-button MSSIM,
    threshold 0.93), for an onlooker at `safety_distance`, 30 degrees to the side. It picks the
    lowest cut-off that hides the digits, but never below `camera_floor`.
  - A number in `highpass_cutoff`: that cut-off.
  - Install refuses cut-offs finer than the screen can show.
- **Quantization.**
  - `diffusion` (default): Floyd-Steinberg error diffusion in linear light, so the 8-bit
    rounding leaves almost no coarse trace of the digit.
  - `round`: each pixel rounded on its own.
  - `compensate`: rounded, with a correction for Plymouth's scale-2 interpolation.
- **Screens.**
  - Install reads the EDID of every connected display and makes a tile set for each scale
    Plymouth may give it, at the logical density.
  - Plymouth's rules: scale 2 for 1200 or more lines with more than 192 dpi, or on simpledrm
    for 1200 or more lines with 2560 or more columns.
  - The overlay picks the set made for the logical screen size; otherwise the first set, and
    text if a tile does not load.
  - Plymouth draws the same sprites on every display, so with several screens one set serves
    all of them.
- **Dependencies.** The tiles are made in dom0 with numpy and Pillow, which every R4.3 dom0 has
  through python3-qubesimgconverter. Text digits need only the standard library.

## Rejected designs

| Option | Reason |
| --- | --- |
| Key provider socket in crypttab | a stale socket blocks unlocking without fallback and uses a `tries=` attempt |
| Replacement password agent | a hung agent leaves no way to unlock in that boot |
| Patched plymouthd (a new map per key press) | forks a core package, rebuilt for every update |
| Rebuilding the initramfs every boot | cannot remap keys; writes `/boot` on every boot |

# Architecture

A dracut module adds a boot hook and a keypad overlay to the initramfs. The hook gives the
Plymouth password prompt a new random digit layout on every boot; the overlay shows the mapping.
Plymouth, systemd, GRUB, LUKS and the files on disk stay stock, and all boot-time state lives
in the initramfs (RAM). A failed build check or boot hook leaves the stock prompt.

## Files

| Path | Content |
| --- | --- |
| `/usr/sbin/qubes-plymouth-scramble` | the dom0 tool, from the RPM: apply, remove, status and the image checks |
| `/usr/lib/dracut/modules.d/91qubes-scramble/` | the dracut module, from the RPM: `module-setup.sh`, the boot hook and its two units |
| `/var/lib/qubes-plymouth-scramble/theme/` | the applied theme: the overlay, the key images, the tiles and their manifest |
| builder: source tree | the generator, the overlay template and the key images; it loads the dom0 tool for the shared checks |
| builder: `~/.config/qubes-plymouth-scramble.conf` | the settings |

dom0 holds no settings. In each initramfs the module installs:

- `qubes-scramble.sh`, run by `qubes-scramble.service` before `plymouth-start.service`;
- `qubes-scramble-restore.service`, after `cryptsetup.target`;
- the overlay, appended to the image's copy of `qubes-dark.script`;
- `compat/grp_led` of xkeyboard-config, which plymouth-populate-initrd leaves out;
- the key images and, for masked digits, the tile PNGs, the digit panels and their manifest;
- the hook's settings, made at every build from the system keyboard layout: the first
  `XKBLAYOUT` of `vconsole.conf`, else the layout systemd's `kbd-model-map` gives for `KEYMAP`.

## Theme archive

- The builder writes a flat, uncompressed tar: `keypad.script`, the four key images and, for
  masked digits, the tiles, the panels and `qubes-scramble-tiles.json`. Before saving it, the
  builder runs the import checks of the dom0 tool on it.
- `apply` reads the file once, at most 64 MiB, and hashes, checks and unpacks that copy. It
  refuses links, sparse files, directories, duplicates, unknown names, more than 4096 files, a
  manifest whose fields, tile names, image sizes or hashes do not match, and files the manifest
  does not use.
- These checks establish consistency, not intent. The overlay is code that Plymouth runs at the
  password prompt, and the images go to its PNG decoder: the builder is trusted with the prompt.

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

## Apply and remove

- The module is inert until `/var/lib/qubes-plymouth-scramble/theme/keypad.script` exists:
  without it dracut skips the module.
- `apply FILE` unpacks and checks the archive in a staging directory, lists every change, asks,
  then switches the theme directory by rename. `apply` alone keeps the theme.
- Both build every affected image (the running kernel's, and each that has the hook) with the
  dracut call of kernel-install's `50-dracut.install` (`--no-uefi` in the bls layout) into a
  temporary file.
- `validate-tree` unpacks each image and runs the hook and its restore step on a copy. It
  compiles the resulting keymap with libxkbcommon, checks Caps Lock and the F-keys, checks that
  the hook changes nothing without Plymouth, and checks the overlay and the tiles against
  their manifest.
- The images are replaced only when all pass; otherwise the theme and the images stay as
  before. The replacement is one rename per image, not one step for all. No image copies are kept.
- A lock prevents concurrent runs; SIGTERM and SIGHUP roll back. What a killed run leaves in
  the state directory is listed and deleted by the next `apply` or `remove`.
- Later initramfs builds (kernel updates) include the module with the applied theme and repeat
  the validation. A failure gives a stock image and a dracut warning.
- `remove` deletes the theme and rebuilds the images that have the hook.
- The RPM: an upgrade runs `apply` for an applied theme and prints the command to repeat if it
  fails. Erasing runs `remove` first and stops if that fails. Installing refuses while a 0.1.x
  installation exists.

No theme switch is involved: the overlay goes into the initramfs copy of qubes-dark, and
`plymouth-set-default-theme` is only asked for the active theme.

## Theme compatibility

The overlay needs from `qubes-dark.script`:

- the functions `display_prompt_callback`, `display_normal_callback`, `refresh_callback` and
  `keyboard_input_callback`, with their registrations;
- `global.dialog.entry` with `sprite` and `image`;
- the F1/F2/F8 escape sequences;
- no input filter of its own.

These are checked when applying a theme and in every build. Colours, images and positions may change.

## Masked digits

After IllusionPIN (Papadopoulos et al., arXiv 1708.06508), with the paper's names.

- **Composition.** A tile is pedestal + alpha * high-pass(real digit) + beta * low-pass(decoy),
  in linear light, encoded as sRGB. Tiles are opaque, so no theme colour enters.
  - `decoy_amplitude = 0`: no decoy (internal manifest mode `highpass`).
  - `hybrid`: the decoy is the key's standard digit (the paper's shoulder-surfer keypad).
  - `white`: the decoy is a white button.
  - `decoy_amplitude` sets beta from 0 to 0.6, default 0.3; zero uses ten tiles rather than
    the hundred real/decoy combinations of hybrid mode.
- **High-pass filter.**
  - `tapered`: a raised-cosine step that passes nothing below the cut-off and everything above
    twice it.
  - `gaussian`: the paper's `1 - G`.
- **Cut-off.**
  - `auto`: the paper's visibility algorithm (distance-as-filtering low-pass, per-button MSSIM,
    threshold 0.93), for an onlooker at `safety_distance`, 30 degrees to the side. It picks the
    lowest cut-off that hides the digits, but never below `camera_floor`.
  - A number in `highpass_cutoff`: that cut-off.
  - A screen that cannot show the cut-off gets text digits (see Screens).
- **Quantization.**
  - `diffusion` (default): Floyd-Steinberg error diffusion in linear light, so the 8-bit
    rounding leaves almost no coarse trace of the digit.
  - `round`: each pixel rounded on its own.
  - `compensate`: rounded, with a correction for Plymouth's scale-2 interpolation.
- **Size.**
  - `size` scales the digit and its tile, and the font of text digits; key caps and arrows keep
    their size. The decoy's low-pass width scales with the digit; the high-pass cut-off stays in
    cycles per mm.
  - The cut-off search and the safety estimate run on the size-1 digit. At a fixed cut-off the
    outline keeps its width while a larger digit grows, so the per-tile MSSIM rises with size
    although the digit gets easier to read; the tapered filter leaves nothing below the cut-off at
    any size. How much farther a larger digit can be read near the estimate is not measured.
  - Tiles under 24 px give text digits (see Screens). The builder refuses a keypad the screen cannot
    fit and names the largest size that fits.
- **Panel.** With `panel = yes` one opaque PNG in the tiles' background code, made in the VM at
  the size of the digit row plus a margin, lies under the tiles, so no gap shows between them. It
  hides with the map. The builder draws every image at its final size; the overlay scales nothing.
- **Screens.**
  - The builder uses the configured screen modes and physical dimensions to make a tile set
    for each scale Plymouth may give them, at the logical density.
  - Plymouth's rules: scale 2 for 1200 or more lines with more than 192 dpi, or on simpledrm
    for 1200 or more lines with 2560 or more columns.
  - The overlay picks the set made for the logical screen size; otherwise the first set, and
    text if a tile does not load.
  - A screen that cannot show masked digits gets a text set instead: no reported size (and no
    `ppi`), tiles under 24 px at the configured size, or a cut-off finer than its pixels, as for
    large monitors at the simpledrm guess. The builder names each one; its prompt shows text digits.
  - Plymouth draws the same sprites on every display, so with several screens one set serves
    all of them.
- **Dependencies.** The builder uses numpy, Pillow and fontconfig. The dom0 tool uses only
  Python's standard library and the platform's installed dracut, Plymouth and XKB components.

## Rejected designs

| Option | Reason |
| --- | --- |
| Key provider socket in crypttab | a stale socket blocks unlocking without fallback and uses a `tries=` attempt |
| Replacement password agent | a hung agent leaves no way to unlock in that boot |
| Patched plymouthd (a new map per key press) | forks a core package, rebuilt for every update |
| Rebuilding the initramfs every boot | cannot remap keys; writes `/boot` on every boot |

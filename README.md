# qubes-plymouth-scramble

Scrambled digit keys for the Qubes OS disk password prompt. Every boot gets a new random
mapping of the keys 0-9; the stock Qubes Plymouth prompt shows it under the password field:

```
  [1] [2] [3] [4] [5] [6] [7] [8] [9] [0]    key to press
   v   v   v   v   v   v   v   v   v   v
   7   3   0   9   1   5   8   2   6   4     digit it enters
```

A camera that sees only the keyboard records keys, not the digits. Caps Lock switches to
standard digits; a dot left of the field lights red while standard digits are active.

## Passphrases

Any passphrase works, also words mixed with digits: only the unshifted digit keys (top row
and keypad) move. Letters, symbols and Shift+digit symbols type as before. Two differences
at the prompt: Caps Lock switches the digit mode instead of capitalizing (use Shift for
capitals), and only the digits are hidden from a keyboard camera, not the letters. A
numeric PIN in an extra LUKS key slot gets the most from scrambling (optional, keep the
current passphrase for recovery):

```sh
sudo cryptsetup luksAddKey /dev/<luks-partition>
```

## How it works

- Stock Plymouth reads keys through XKB when `vconsole.conf` names an XKB layout
  (otherwise it uses the console keymap). The console keymap is not changed.
- A dracut module adds a hook to the initramfs that runs before plymouthd and writes, in
  the initramfs (RAM) only and readable by root only: an XKB layout with permuted digits
  plus a standard group on top of the system keyboard layout, `XKB*` settings in
  `/etc/vconsole.conf`, and the map as `[script-env-vars]` of the theme. Once the disk is
  unlocked, a second step deletes the map and restores `vconsole.conf`.
- The keypad is a small overlay appended to the image's copy of `qubes-dark.script`. It
  wraps the stock callbacks and calls them first, so the stock prompt is unchanged.
- No system setting changes: GRUB, the kernel command line, dracut, console and Plymouth
  configuration, the theme files on disk and LUKS stay stock.

## Requirements

A stock Qubes R4.3 dom0; nothing else is installed. Used: python3 (standard library only),
dracut, kbd (`loadkeys`), libxkbcommon, xkeyboard-config, systemd (`kernel-install`,
`kbd-model-map`), plymouth-scripts and the qubes-dark theme of qubes-artwork-plymouth. The
theme script may change colours, images and positions; it must keep the callbacks and the
password field the overlay uses (checked at install and at every initramfs build).

## Install (dom0)

Copy this directory to dom0 (for example `qvm-run --pass-io <qube> 'tar -cf - -C ~
qubes-plymouth-scramble' | tar -xf -`), then:

```sh
sudo ./qubes-plymouth-scramble install    # lists the changes, asks y/N
sudo qubes-plymouth-scramble status
```

`install` lists every change and asks before making any (`--yes` skips the question; without
a terminal it is required). It adds exactly:

| Path | Content |
| --- | --- |
| `/usr/lib/dracut/modules.d/91qubes-scramble/` | dracut module made from the settings, with a copy of this tool in `source/` |
| `/etc/qubes-plymouth-scramble.conf` | settings (copy of the bundled file if absent) |
| `/usr/local/sbin/qubes-plymouth-scramble` | link to the tool in the module |

and rebuilds `/boot/initramfs-<kernel>.img` for the running kernel and every kernel whose
image has the module, with the dracut call that kernel-install makes on kernel updates
(its boot loader plugins are not run). Each new image is unpacked, the boot hook and its
restore step run on a copy and libxkbcommon compiles the resulting keymap; the images are
replaced only when all pass. If a step fails, the module, the settings and the images stay
as before. Later initramfs builds (kernel updates) include the module and repeat the
checks; if one fails, they build a stock image with a dracut warning.

## Configure

`/etc/qubes-plymouth-scramble.conf` (see the bundled `qubes-plymouth-scramble.conf`):

- `enabled`: `no` and `install` keep only the tool in the module directory (dracut then
  skips the module) and rebuild the images that have it as stock images
- `console`: also scramble the text console prompt, which is used when Plymouth is disabled
  on the kernel command line; the console then shows the keys of the boot
- `[digits]`: optional letter group and colour under each digit, e.g. `2 = ABC #ffb74d`

The keyboard layout follows the system (`/etc/vconsole.conf`): its XKB layout, or the one
systemd maps the console keymap to. Layouts that need Shift for digits (e.g. fr) are refused.
Run `sudo qubes-plymouth-scramble install` after every change, also after changing the
system keyboard layout.

## Uninstall

`sudo qubes-plymouth-scramble uninstall` lists its changes, asks, removes the three paths
above and rebuilds every image that has the module with the same kernel-install dracut
call. No copies of old images are kept: the result has the same files and contents as an
image that kernel-install builds for a stock system.

## Recovery

- At the prompt: Caps Lock for standard digits; the passphrase can then be typed as usual
  (capitals with Shift).
- Esc shows the text view; keys stay scrambled there; Esc again shows the keypad.
- Without a working prompt: in the GRUB menu press `e`, add `plymouth.enable=0` to the
  `module2 /vmlinuz...` line and boot with Ctrl+X (once; nothing is saved). The text
  console asks for the password with standard digits (with `console = yes` it prints the
  keys of the boot). Then run `sudo qubes-plymouth-scramble uninstall`.

## Limits

- One mapping per boot: retries use the same mapping (Plymouth compiles the keymap once).
  A new mapping per key press needs a patched Plymouth.
- A recording of keys with one mapping narrows a PIN of k distinct digits to at most
  10!/(10-k)! candidates; use a long PIN. Scrambling adds no entropy to the password.
- A camera that sees the screen and the keys defeats it.

## Development

`tests/run-tests.sh` runs every check: the Python unit tests (standard library; they need
libxkbcommon, xkeyboard-config and loadkeys and run in dom0 as well), the bats tests of the
boot hook functions, ShellCheck and the ASCII check (bats and ShellCheck in a development
qube). `docs/` holds the design notes and the verification record; `versions.txt` the
release history.

## License

MIT, see [LICENSE](LICENSE).

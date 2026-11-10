# qubes-plymouth-scramble

Scrambled digit keys for the Qubes OS disk password prompt. Every boot gets a new random
mapping of the keys 0-9, shown under the password field of the stock Plymouth prompt:

```
  [1] [2] [3] [4] [5] [6] [7] [8] [9] [0]    key to press
   v   v   v   v   v   v   v   v   v   v
   7   3   0   9   1   5   8   2   6   4     digit it enters
```

A camera that sees only the keyboard records keys, not digits. Caps Lock switches to standard
digits; the dot left of the field turns red while they are active. The digits are drawn as
masked images after IllusionPIN: readable up close, they fade with distance.

Requires Qubes OS 4.3 with an encrypted disk and the stock qubes-dark boot theme.

| Where | What |
| --- | --- |
| dom0 | one RPM: a dracut module and a tool that uses only Python's standard library |
| builder qube | this source tree: the settings, the theme generator and its preview |

dom0 needs the builder only to make a theme. Kernel and package updates rebuild the
initramfs with the applied theme.

## Quick start

The builder is a Fedora qube with `rpm-build git python3-numpy python3-pillow fontconfig`.
Install them in its template: system changes in an app qube do not survive a restart.

1. Builder: get the source and build the RPM.

   ```sh
   git clone https://github.com/cuzrawr/qubes-plymouth-scramble
   cd qubes-plymouth-scramble
   git archive --prefix=qubes-plymouth-scramble-0.2.0/ -o qubes-plymouth-scramble-0.2.0.tar.gz HEAD
   rpmbuild -tb qubes-plymouth-scramble-0.2.0.tar.gz
   ```

2. dom0: copy the RPM and install it, with `BUILDER` replaced by the qube's name.

   ```sh
   qvm-run --pass-io BUILDER 'cat ~/rpmbuild/RPMS/noarch/qubes-plymouth-scramble-0.2.0-1.noarch.rpm' > qubes-plymouth-scramble.rpm
   sudo dnf install ./qubes-plymouth-scramble.rpm
   ```

3. Builder: the first run creates `~/.config/qubes-plymouth-scramble.conf`. Set `screens` in
   it: the mode that `xrandr` in dom0 marks with `+`, and the screen size in mm.

   ```sh
   ./qubes-plymouth-scramble-theme
   ```

   ```ini
   screens = 1920x1080 300x170mm
   ```

4. Builder: look at the preview, then build the theme.

   ```sh
   ./qubes-plymouth-scramble-theme --preview
   ./qubes-plymouth-scramble-theme
   ```

5. dom0: run the two commands the build printed. They copy the theme and apply it; `apply`
   lists every change and asks first. Reboot and type the passphrase with the keys the
   prompt shows.

## Settings

All settings are in the builder's `~/.config/qubes-plymouth-scramble.conf`, each explained there.

- `screens`: one screen per indented line. Without a size in mm a screen gets text digits,
  unless `ppi` is set. A pasted `xrandr` "connected" line is accepted, but it names the active
  mode, which must be the one marked `+`.
- `display`: `hybrid` (default), `white` or `text`.
- `decoy_amplitude`: strength of the decoy digits, 0 to 0.6 (default 0.3); 0 shows no decoy.
- `size`: digit size as a factor; the shipped settings use 1.5, without the line it is 1.
- `panel`: masked digits on one grey panel (`yes`, default) or on separate tiles (`no`).
- `output`: directory of the theme archive and the previews, `~/QubesPlymouthScramble`.

`--preview` shows the first screen's keypad above the view the model gives an onlooker. View it
at 100% zoom. Without a desktop viewer it prints the path of the saved PNG. The preview uses a
fixed example mapping, and its labels use the builder's font.

A screen that cannot show masked digits (no size, or too coarse) gets text digits; the build
names it. Keyboard layouts that need Shift for digits (for example fr) are refused. Only the
digit keys move; Caps Lock switches the digits instead of capitalizing, so use Shift for capitals.

## Commands in dom0

| Command | Effect |
| --- | --- |
| `sudo qubes-plymouth-scramble apply THEME.tar` | install the theme; rebuild the initramfs of the running kernel and of every kernel that has the keypad |
| `sudo qubes-plymouth-scramble apply` | rebuild those images with the applied theme |
| `sudo qubes-plymouth-scramble status` | show the theme and what each initramfs contains |
| `sudo qubes-plymouth-scramble remove` | remove the theme and rebuild stock images; the RPM stays, inactive |
| `sudo dnf remove qubes-plymouth-scramble` | the same, then remove the RPM |

`apply` and `remove` list every change and ask first; `--yes` skips the question. `apply` prints
the SHA-256 of the archive: it must equal the one the build printed.

## Trust

- The RPM is code for dom0. Build it from source you trust, in a qube you trust. Qubes advises
  against copying files into dom0: see [how to copy from dom0][copy].
- A theme contains Plymouth script code and images, which Plymouth runs and decodes at the
  password prompt. dom0 checks an archive's names, sizes, structure and hashes; it cannot tell
  whether the code is harmless. Use a dedicated builder qube; it needs no network once it has
  the source and the packages.

## Recovery

- At the prompt, Caps Lock gives standard digits.
- If the prompt does not work: in the GRUB menu press `e`, append `plymouth.enable=0` to the
  `module2 /vmlinuz...` line and boot with Ctrl+X (once; nothing is saved). The text console
  asks with standard digits. Then run `sudo qubes-plymouth-scramble remove`.
- A failed build or validation changes nothing: the images are replaced only after every new
  one passed. Images are replaced one by one, so a crash at that moment can leave kernels
  with different themes; run `apply` again.
- After a killed `apply` or `remove`, run it again: it lists and deletes what was left behind.
- If `dnf remove` stops because the stock rebuild failed, fix the reported cause and repeat it.

## Limits

- One mapping per boot: retries use the same mapping.
- A camera that sees both the screen and the keys defeats it. Masked digits only make the screen
  harder to read from farther away; their safety distance is a model estimate.
- With several screens at boot, all show the same keypad, with the images of one tile set.

How it works: [docs/architecture.md](docs/architecture.md).

[copy]: https://doc.qubes-os.org/en/latest/user/how-to-guides/how-to-copy-from-dom0.html

## License

MIT, see [LICENSE](LICENSE).

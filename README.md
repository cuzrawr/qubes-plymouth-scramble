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

Requires Qubes OS 4.3 with an encrypted disk and the stock qubes-dark boot theme. Nothing else
is installed in dom0.

## Install

1. In a networked qube, download the project:

   ```sh
   curl -fL --create-dirs -o ~/Downloads/qubes-plymouth-scramble.tar.gz \
       https://github.com/cuzrawr/qubes-plymouth-scramble/archive/refs/heads/main.tar.gz
   ```

2. In dom0, with `DOWNLOAD_QUBE` replaced by that qube's name:

   ```sh
   qvm-run --pass-io DOWNLOAD_QUBE 'cat ~/Downloads/qubes-plymouth-scramble.tar.gz' > qubes-plymouth-scramble.tar.gz
   tar -xzf qubes-plymouth-scramble.tar.gz
   cd qubes-plymouth-scramble-main
   sudo ./qubes-plymouth-scramble install
   ```

   `install` lists every change and asks first. Files copied into dom0 run as root there; read
   them in the download qube first if in doubt. The unpacked directory can be deleted
   afterwards; the installation keeps its own copy.

3. Reboot and type the passphrase with the keys the prompt shows.

```sh
sudo qubes-plymouth-scramble status
```

## Configure

The settings are in `/etc/qubes-plymouth-scramble.conf`, each explained there. Apply changes
with `sudo qubes-plymouth-scramble install`. For example, plain digits instead of masked ones:

```ini
[scramble]
enabled = yes
display = text
```

- `display`: `hybrid` (default), `highpass`, `white` or `text`.
- `size`: digit size as a factor; the shipped settings use 1.5, without the line it is 1. Key
  caps keep their size.
- `panel`: masked digits on one grey panel (`yes`, default) or on separate tiles (`no`).
- Masked digits are made for the screens connected at install: run `install` again after
  changing screens.
- A screen that cannot show masked digits (no reported size, or too coarse at Plymouth's scale)
  gets text digits; `install` names it.
- Only the digit keys move: letters and symbols type as before. Caps Lock switches the digits
  instead of capitalizing; use Shift for capitals.
- Keyboard layouts that need Shift for digits (for example fr) are refused.

## Uninstall

```sh
sudo qubes-plymouth-scramble uninstall
```

It removes the dracut module, the settings and the link, and rebuilds the initramfs images the
way kernel-install does.

## Recovery

- At the prompt, Caps Lock gives standard digits.
- If the prompt does not work: in the GRUB menu press `e`, append `plymouth.enable=0` to the
  `module2 /vmlinuz...` line and boot with Ctrl+X (once; nothing is saved). The text console
  asks with standard digits. Then run `sudo qubes-plymouth-scramble uninstall`.

## Limits

- One mapping per boot: retries use the same mapping.
- A camera that sees both the screen and the keys defeats it. Masked digits only make the screen
  harder to read from farther away; their safety distance is a model estimate.
- With several screens at boot, all show the same keypad, with images made for the largest.

How it works: [docs/architecture.md](docs/architecture.md).

## License

MIT, see [LICENSE](LICENSE).

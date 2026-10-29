# Verification record

Target: Qubes R4.3 dom0 with the package versions listed in `architecture-options.md`.
Runs of 2026-10-28/29 UTC. "Final build" is the code in this repository; "earlier build" had
the same keypad drawing and digit layout with an older installer, hook hardening and overlay text.

## Automated checks

| Check | Where | Result |
| --- | --- | --- |
| `tests/run-tests.sh`: 50 unit tests (`-X dev -W error`), 5 bats tests, ShellCheck 0.11.0, ASCII | Fedora 44 dev qube | pass |
| Unit tests, including the installed qubes-dark script | R4.3 dom0 of both test systems | pass |
| Line coverage of the tool by the unit tests (stdlib trace) | development host | 93.8% (rest: system command wrappers) |
| ruff 0.17 (default and bug rules), pyflakes, pylint 4.0 (10.00/10) | development host, dev qube | clean |
| ShellCheck with all optional checks (except brace style SC2250 and SC2310) | dev qube | clean |
| bandit 1.9 | development host | accepted only: fixed-argument subprocess calls, 0755 module dir, private tempdir |
| mypy 2.4 (untyped code) | development host | no defects (only guarded `re` matches, configparser `optionxform` idiom) |
| Imports and programs | R4.3 dom0 | standard library only; every program from a stock RPM; `rpm -V` clean |

The unit tests include seeded fuzzing of the configuration, vconsole.conf, theme file and
kbd-model-map parsers, a bash round trip of the generated shell settings, exact vconsole.conf
edits and byte-exact restores for random inputs, 15 image mutations that validation must reject
by the intended check, keymaps for random maps and common layouts, and the install, uninstall
and image switch transactions in a temporary root.

## QEMU VM (R4.3, BIOS, LUKS2 root, layout other)

Final build:

- Keypad pixel-identical to the earlier build (note, dot, key caps, labels, arrows, field); a
  new map each boot; the PIN typed through the map unlocks; Caps Lock shows the red dot and
  hides the map (maximum deviation 2/255 from the background); F8 shows a scrambled digit.
- After unlocking: no map file left in /run, /run/plymouth mode 755, restore unit finished,
  console digits stock, no related journal warnings, no failed units.
- Install, reinstall through the installed link, `enabled = no` and enabling again, and an install
  whose image fails validation (fr layout), which left images, module, settings and link unchanged.
- Uninstall: the image equals `kernel-install add` run with only `50-dracut.install` (all names,
  types, modes, link targets and 2116 file contents, including the recorded dracut arguments);
  GRUB and Xen files unchanged; no files of the tool remain.

Earlier build:

- A wrong attempt returns to the prompt with the same map; Esc text view and back.
- `console = yes` with Plymouth disabled by a one-time boot option: the printed map matches the
  keys, the PIN unlocks, the console keymap is stock after unlocking.
- The uninstalled image boots to the stock prompt and unlocks with standard digits.
- The kernel logged `crng init done` 17 ms before the hook started.

## Second test system (R4.3, UEFI, no disk encryption, kernel-install layout bls)

- Final build: unit tests pass with the installed theme; install validates and records the
  dracut arguments with `--no-uefi`; uninstall gives an image identical to the bls build of
  `50-dracut.install` (2094 file contents); the ESP is unchanged; the system is stock again.
- Earlier build: the scrambled image booted; the hook ran 51 ms after `crng init done`, Plymouth
  started, no failed units, no related warnings; console digits stock.

## Not verified

- Keypad rendering with other display resolutions and graphics drivers (only the QEMU VM has a LUKS prompt).
- `console = yes` boot with the final build (unit tests only); `console = no` with Plymouth
  disabled at boot (unit test only).
- Several installed kernels with the module (tested with one kernel per test system).
- AEM, several LUKS volumes, TPM2 PIN.

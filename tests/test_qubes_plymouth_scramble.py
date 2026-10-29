"""Tests for qubes-plymouth-scramble: generator, boot hook and image validation.

Needs bash 5.1 or later, libxkbcommon, xkeyboard-config, systemd's kbd-model-map and loadkeys
(as in Qubes dom0 or Fedora). The fuzz tests use fixed seeds, so every run checks the same inputs.
"""

import contextlib
import glob
import importlib.machinery
import importlib.util
import io
import math
import os
import random
import re
import shutil
import signal
import string
import struct
import subprocess
import tempfile
import unittest
import zlib
from unittest import mock

SOURCE = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
_loader = importlib.machinery.SourceFileLoader("scramble", os.path.join(SOURCE, "qubes-plymouth-scramble"))
scramble = importlib.util.module_from_spec(importlib.util.spec_from_loader("scramble", _loader))
_loader.exec_module(scramble)

HOOK = os.path.join(SOURCE, "dracut", "qubes-scramble.sh")
TEMPLATE = os.path.join(SOURCE, "theme", "keypad.script.in")
STOCK_THEME = "[Plymouth Theme]\nName=Qubes Dark\nModuleName=script\n\n[script]\nImageDir=/x\nScriptFile=/x/s\n"
# The parts of the qubes-dark theme script (qubes-artwork-plymouth 4.3.9) the keypad overlay uses
STOCK_SCRIPT = """Window.SetBackgroundTopColor(0.263, 0.294, 0.325);
fun refresh_callback () {
    logo.sprite.SetX((Window.GetWidth() - logo.image.GetWidth()) / 2);
}
Plymouth.SetRefreshFunction(refresh_callback);
fun dialog_setup(text) {
    local.entry;
    entry.image = Image("entry.png");
    entry.sprite = Sprite(entry.image);
    entry.sprite.SetPosition(Window.GetX() + (Window.GetWidth() - entry.image.GetWidth()) / 2, 300, 1);
    global.dialog.entry = entry;
}
fun display_normal_callback() { global.status = "normal"; }
fun display_prompt_callback(prompt, password, is_secret) { dialog_setup(prompt); }
fun keyboard_input_callback(key){
    if (key == "\x1b[[A") { global.show_dots = 0; } else if (key == "\x1b[[B") { global.show_dots = 1; }
    else if (key == "\x1b[19~") { global.show_plaintext_password = 1; }
}
Plymouth.SetKeyboardInputFunction(keyboard_input_callback);
Plymouth.SetDisplayNormalFunction(display_normal_callback);
Plymouth.SetDisplayPromptFunction(display_prompt_callback);
"""
STOCK_VCONSOLE = 'KEYMAP="us"\nFONT="eurlatgr"\n'
HOOK_ENV = {"PATH": "/usr/bin:/usr/sbin", "LC_ALL": "C"}
# vconsole.conf lines the hook writes for plymouthd
XKB_LINES = ["XKBLAYOUT=qubes-scramble,qubes-scramble", "XKBMODEL=pc105", "XKBVARIANT=basic,standard",
             "XKBOPTIONS=grp:caps_toggle,grp_led:caps"]
# Lines in the format of systemd's kbd-model-map (tab separated)
MODEL_MAP = ("# console\tlayout\tmodel\tvariant\toptions\n"
             "us\t\t\tus\tpc105+inet\t-\t\tterminate:ctrl_alt_bksp\n"
             "de-latin1-nodeadkeys\tde\tpc105\t\tnodeadkeys\tterminate:ctrl_alt_bksp\n"
             "mk-utf\t\t\tmk,us\tpc105\t\t-\t\tterminate:ctrl_alt_bksp,grp:shifts_toggle\n"
             "uk\t\t\tgb\tpc105\t\t-\t\tterminate:ctrl_alt_bksp\n")
# The XKB files plymouth-populate-initrd (Plymouth 24.004.60) puts into the initramfs, plus
# compat/grp_led from the dracut module; every top-level symbols file is installed as well
IMAGE_XKB_FILES = ["compat/" + name for name in ("accessx", "basic", "caps", "complete", "iso9995", "ledcaps", "lednum",
                                                 "ledscroll", "level5", "misc", "mousekeys", "xfree86", "grp_led")] + \
    ["keycodes/aliases", "keycodes/evdev", "rules/evdev"] + \
    ["types/" + name for name in ("basic", "complete", "extra", "iso9995", "level5", "mousekeys", "numpad", "pc")]


def copy_image_xkb(root):
    """Copies the system XKB data that an initramfs contains into root."""
    source, target = scramble.XKB_ROOT, root + scramble.XKB_ROOT
    for name in IMAGE_XKB_FILES + ["symbols/" + entry.name for entry in os.scandir(source + "/symbols")
                                   if entry.is_file()]:
        os.makedirs(os.path.dirname(os.path.join(target, name)), exist_ok=True)
        shutil.copyfile(os.path.join(source, name), os.path.join(target, name))


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as stream:
        stream.write(data.encode() if isinstance(data, str) else data)


def read(path):
    with open(path, encoding="utf-8") as stream:
        return stream.read()


def read_bytes(path):
    with open(path, "rb") as stream:
        return stream.read()


def config_from(text):
    with tempfile.NamedTemporaryFile(suffix=".conf", delete=False) as stream:
        stream.write(text.encode("utf-8", "surrogatepass") if isinstance(text, str) else text)
    try:
        return scramble.Config(stream.name)
    finally:
        os.unlink(stream.name)


def png_alpha(path):
    """Width, height and alpha(x, y) of an 8-bit RGBA PNG written without filters."""
    data = read_bytes(path)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    raw = zlib.decompress(data[data.index(b"IDAT") + 4:data.index(b"IEND") - 8])
    return width, height, lambda x, y: raw[y * (1 + 4 * width) + 1 + 4 * x + 3]


def write_image_conf(root, text="", layout="us"):
    write(root + scramble.IMAGE_CONF, scramble.module_conf(config_from(text), layout))


def run_hook(root, *args):
    """Runs the boot hook on root with the umask systemd gives services (0022)."""
    return subprocess.run(["bash", "-c", 'umask 022 && exec bash "$0" "$@"', HOOK, *args], capture_output=True,
                          text=True, check=False, env={**HOOK_ENV, "QUBES_SCRAMBLE_ROOT": root})


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        config = config_from("")
        self.assertEqual((config.enabled, config.console, config.hint), (True, False, ""))

    def test_values(self):
        config = config_from("[scramble]\nenabled = no\nconsole = yes\n[digits]\n2 = ABC #ff8000\n7 = PQRS\n")
        self.assertEqual((config.enabled, config.console), (False, True))
        self.assertEqual((config.letters[2], config.colors[2], config.colors[7]),
                         ("ABC", (1.0, 128 / 255, 0.0), (1.0, 1.0, 1.0)))
        self.assertEqual(config.hint, "Disk password letters: 2 ABC, 7 PQRS")

    def test_invalid(self):
        for text in ("[scramble]\nlayout = us\n", "[scramble]\nenabled = maybe\n", "[scramble]\ncolour = red\n",
                     "[digits]\n12 = AB\n", "[digits]\n3 = abc\n", "[digits]\n3 = AB'C\n", "[digits]\n3 = ABCDEFGHI\n",
                     "[digits]\n3 = ABC #12345\n", "[other]\nx = 1\n", "no section\n", "[digits]\n1 = A\n1 = B\n"):
            with self.subTest(text=text), self.assertRaises(scramble.ScrambleError):
                config_from(text)


class SystemLayoutTest(unittest.TestCase):
    """The prompt keeps the system keyboard layout for letters, so a recovery passphrase types the same."""

    def layout(self, vconsole, model_map=MODEL_MAP):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as stream:
            stream.write(model_map)
        self.addCleanup(os.unlink, stream.name)
        return scramble.system_layout(vconsole, stream.name)

    def test_layouts(self):
        for vconsole, expected in (({"KEYMAP": "us"}, "us"), ({}, "us"), ({"KEYMAP": "uk"}, "gb"),
                                   ({"KEYMAP": "de-latin1-nodeadkeys"}, "de(nodeadkeys)"), ({"KEYMAP": "mk-utf"}, "mk"),
                                   ({"KEYMAP": "us", "XKBLAYOUT": "de,us", "XKBVARIANT": "nodeadkeys,"}, "de(nodeadkeys)")):
            with self.subTest(vconsole=vconsole):
                self.assertEqual(self.layout(vconsole), expected)

    def test_unknown_or_unsafe(self):
        for vconsole in ({"KEYMAP": "xx"}, {"KEYMAP": "#"}, {"XKBLAYOUT": "us;rm"}, {"XKBLAYOUT": "us", "XKBVARIANT": "a b"}):
            with self.subTest(vconsole=vconsole), self.assertRaises(scramble.ScrambleError):
                self.layout(vconsole)
        with self.assertRaisesRegex(scramble.ScrambleError, "cannot read"):
            scramble.system_layout({"KEYMAP": "us"}, "/nonexistent/kbd-model-map")

    def test_system_table(self):
        self.assertEqual(scramble.system_layout({"KEYMAP": "us"}), "us")


class ThemeScriptTest(unittest.TestCase):
    """The overlay needs the stock callbacks and the password field, not a particular version."""

    def test_stock_and_shifted_themes(self):
        shifted = (STOCK_SCRIPT.replace("0.263, 0.294, 0.325", "0.1, 0.1, 0.4").replace("entry.png", "field.png")
                   .replace(", 300, 1)", ", 420, 2)").replace("fun refresh_callback ()", "fun refresh_callback()")
                   + 'fun progress_callback(d, p) { }\nPlymouth.SetBootProgressFunction(progress_callback);\n')
        for text in (STOCK_SCRIPT, shifted, STOCK_SCRIPT.replace("\x1b", "\\e")):
            scramble.check_theme_script(text)

    @unittest.skipUnless(os.path.exists(scramble.THEME_SCRIPT), "needs the installed qubes-dark theme")
    def test_installed_theme(self):
        scramble.check_theme_script(read(scramble.THEME_SCRIPT))

    def test_missing_parts_are_named(self):
        for description, pattern in scramble.THEME_REQUIREMENTS:
            with self.subTest(requirement=description):
                broken = re.sub(pattern, "removed", STOCK_SCRIPT)
                with self.assertRaisesRegex(scramble.ScrambleError, re.escape(description)):
                    scramble.check_theme_script(broken)

    def test_conflicts(self):
        for extra in ("Plymouth.SetValidateInputFunction(filter);\n", "qubes_scramble.cell = 1;\n"):
            with self.subTest(extra=extra), self.assertRaisesRegex(scramble.ScrambleError, "input filter"):
                scramble.check_theme_script(STOCK_SCRIPT + extra)


class GeneratedFilesTest(unittest.TestCase):
    def test_theme_images(self):
        sizes = []
        for name in scramble.IMAGES:
            width, height, alpha = png_alpha(os.path.join(SOURCE, "theme", name))
            sizes.append((width, height))
            self.assertEqual(alpha(0, 0), 0, f"{name} has an opaque corner")
        self.assertEqual(sizes, [(30, 30), (10, 14), (12, 12), (12, 12)])
        self.assertEqual(png_alpha(os.path.join(SOURCE, "theme", "qubes-scramble-dot-on.png"))[2](6, 6), 255)
        self.assertEqual(png_alpha(os.path.join(SOURCE, "theme", "qubes-scramble-dot-off.png"))[2](6, 6), 0)

    def test_overlay(self):
        overlay = scramble.render_overlay(config_from("[digits]\n2 = ABC #ff0000\n"), read(TEMPLATE))
        self.assertIn(scramble.OVERLAY_MARKER, overlay)
        self.assertIn('qubes_scramble.letters["2"] = "ABC"; qubes_scramble.red["2"] = 1.000; '
                      'qubes_scramble.green["2"] = 0.000;', overlay)
        self.assertNotIn("@", overlay)
        overlay.encode("ascii")
        # The stock qubes-dark F1/F2/F8 handler compares escape sequences that start with ESC
        for sequence in ('"\\e[[A"', '"\\e[[B"', '"\\e[19~"'):
            self.assertIn(f"keyboard_input_callback({sequence})", overlay)

    def test_module_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            scramble.write_module(directory, config_from("[digits]\n2 = ABC\n"), "us")
            for name in ("module-setup.sh", "qubes-plymouth-scramble.conf", "keypad.script", "source/README.md",
                         "source/qubes-plymouth-scramble.conf", "source/theme/keypad.script.in"):
                self.assertTrue(os.path.isfile(os.path.join(directory, name)), name)
            self.assertTrue(os.access(os.path.join(directory, "source", scramble.PROG), os.X_OK))
            self.assertIn('letters["2"] = "ABC"', read(os.path.join(directory, "keypad.script")))
            module_setup = read(os.path.join(directory, "module-setup.sh"))
            self.assertEqual(module_setup, read(os.path.join(SOURCE, "dracut", "module-setup.sh")))
            referenced = re.findall(r'"\$src/([^" ]+)"', module_setup) + re.findall(r'"\$src"/([^ ;]+)', module_setup)
            self.assertGreaterEqual(len(referenced), 5)
            for pattern in referenced:
                self.assertTrue(glob.glob(os.path.join(directory, "source", pattern)), pattern)
        with tempfile.TemporaryDirectory() as directory:
            scramble.write_module(directory, config_from("[scramble]\nenabled = no\n"), "")
            self.assertEqual(sorted(os.listdir(directory)), ["module-setup.sh", "source"], "disabled: dracut skips it")
            self.assertTrue(os.access(os.path.join(directory, "source", scramble.PROG), os.X_OK))

    def test_module_conf(self):
        text = scramble.module_conf(config_from("[digits]\n2 = ABC\n"), "de(nodeadkeys)")
        for line in ("BASE_LAYOUT='de(nodeadkeys)'", "CONSOLE_SCRAMBLE=0",
                     "HINT='Disk password letters: 2 ABC'", f"RUNTIME_THEME_FILE='{scramble.RUNTIME_THEME_FILE}'"):
            self.assertIn(line + "\n", text)


class HookTest(unittest.TestCase):
    """Runs the boot hook against a temporary root with the XKB data of an initramfs."""

    @classmethod
    def setUpClass(cls):
        cls.xkb = scramble.Xkb()
        cls.root = tempfile.mkdtemp(prefix="scramble-test-")
        cls.addClassCleanup(shutil.rmtree, cls.root)
        copy_image_xkb(cls.root)

    def setUp(self):
        write_image_conf(self.root)
        write(self.root + "/etc/vconsole.conf", STOCK_VCONSOLE)
        write(self.root + scramble.THEME_FILE, STOCK_THEME)
        for path in (scramble.CONSOLE_MAP, "/proc/cmdline", scramble.XKB_EXTRA + "/symbols/qubes-scramble",
                     "/etc/vconsole.conf.qubes-scramble-orig", scramble.RUNTIME_THEME_FILE):
            if os.path.exists(self.root + path):
                os.unlink(self.root + path)
        self.usr = scramble.tree_snapshot(self.root + "/usr")

    def tearDown(self):
        self.assertEqual(scramble.tree_snapshot(self.root + "/usr"), self.usr, "the hook must not write below /usr")

    def state(self):
        """The hook's vconsole.conf settings and the theme's digit map."""
        return (scramble.read_vconsole(self.root + "/etc/vconsole.conf"),
                scramble.read_theme_map(self.root + scramble.RUNTIME_THEME_FILE))

    def test_scrambles_plymouth(self):
        result = run_hook(self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        vconsole, digit_map = self.state()
        self.assertEqual(read(self.root + "/etc/vconsole.conf"), STOCK_VCONSOLE + "\n".join(XKB_LINES) + "\n")
        scramble.check_keymap(self.xkb, self.root, vconsole, digit_map)
        self.assertEqual(read(self.root + scramble.THEME_FILE), STOCK_THEME)
        self.assertFalse(os.path.exists(self.root + scramble.CONSOLE_MAP))
        self.assertNotIn(digit_map, result.stdout + result.stderr)
        for path in (scramble.RUNTIME_THEME_FILE, scramble.XKB_EXTRA + "/symbols/qubes-scramble", "/etc/vconsole.conf"):
            self.assertEqual(os.stat(self.root + path).st_mode & 0o777, 0o600, f"{path} must be readable by root only")
        # Directories keep the usual mode; plymouthd later uses /run/plymouth
        for path in ("/run/plymouth", os.path.dirname(scramble.RUNTIME_THEME_FILE), scramble.XKB_EXTRA + "/symbols"):
            self.assertEqual(os.stat(self.root + path).st_mode & 0o777, 0o755, path)

    def test_hook_commands_are_in_the_image(self):
        installed = set(" ".join(re.findall(r"inst_multiple ([^\n]*)", read(os.path.join(SOURCE, "dracut",
                                                                                     "module-setup.sh")))).split())
        code = re.sub(r"(?m)^\s*#.*$", "", read(HOOK))
        for command in ("cat", "cp", "cut", "find", "grep", "head", "ln", "loadkeys", "mkdir", "mv", "od", "rm", "rmdir",
                        "sed", "sort", "stat", "tail", "touch", "tr", "wc"):
            if re.search(rf"(^|[\s;|&(]){command}\s", code):
                self.assertIn(command, installed, f"the hook runs {command}, which the dracut module does not install")

    def test_restore_deletes_the_map(self):
        self.assertEqual(run_hook(self.root).returncode, 0)
        result = run_hook(self.root, "restore")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(read(self.root + "/etc/vconsole.conf"), STOCK_VCONSOLE)
        for path in (scramble.RUNTIME_THEME_FILE, os.path.dirname(os.path.dirname(scramble.RUNTIME_THEME_FILE)),
                     scramble.XKB_EXTRA + "/symbols/qubes-scramble", "/etc/vconsole.conf.qubes-scramble-orig"):
            self.assertFalse(os.path.exists(self.root + path), path)
        self.assertEqual(run_hook(self.root, "restore").returncode, 0, "restore runs again without effect")

    def test_random_maps_compile(self):
        for _ in range(25):
            write(self.root + "/etc/vconsole.conf", STOCK_VCONSOLE)
            self.assertEqual(run_hook(self.root).returncode, 0)
            scramble.check_keymap(self.xkb, self.root, *self.state())

    def test_only_unshifted_digits_change(self):
        """Mixed passphrases type as before: letters, symbols and Shift levels come from the base
        layout; Caps Lock switches the digit group and does not capitalize."""
        self.assertEqual(run_hook(self.root).returncode, 0)
        vconsole, digit_map = self.state()
        include = (self.root + scramble.XKB_EXTRA, self.root + scramble.XKB_ROOT)
        keys = [f"A{row}{column:02d}" for row, count in (("E", 12), ("D", 12), ("C", 11), ("B", 10))
                for column in range(1, count + 1)] + ["TLDE", "BKSL", "SPCE"]
        with self.xkb.keymap(include, vconsole["XKBLAYOUT"], vconsole["XKBVARIANT"], vconsole["XKBOPTIONS"]) as scrambled, \
                self.xkb.keymap(include, "us") as base:
            for key in keys:
                for level in (0, 1):
                    expected = base.syms(key, 0, level)
                    if level == 0 and key in scramble.DIGIT_KEYS:
                        expected = [digit_map[scramble.DIGIT_KEYS.index(key)]]
                    self.assertEqual(scrambled.syms(key, 0, level), expected, f"<{key}> level {level + 1}")
            scrambled.tap("CAPS")
            self.assertEqual((scrambled.text("AC01"), scrambled.text("AE01")), ("a", "1"))

    def test_common_layouts_compile(self):
        for layout in ("gb", "de(nodeadkeys)", "es", "it", "pl", "us(intl)"):
            with self.subTest(layout=layout):
                write_image_conf(self.root, layout=layout)
                write(self.root + "/etc/vconsole.conf", STOCK_VCONSOLE)
                self.assertEqual(run_hook(self.root).returncode, 0)
                scramble.check_keymap(self.xkb, self.root, *self.state())

    def test_layout_with_shifted_digits_is_refused(self):
        write_image_conf(self.root, layout="fr")
        self.assertEqual(run_hook(self.root).returncode, 0)
        with self.assertRaisesRegex(scramble.ScrambleError, "need Shift"):
            scramble.check_keymap(self.xkb, self.root, *self.state())

    def test_failure_restores_files(self):
        write(self.root + scramble.THEME_FILE, STOCK_THEME + "[script-env-vars]\nx=1\n")
        self.usr = scramble.tree_snapshot(self.root + "/usr")
        result = run_hook(self.root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already has [script-env-vars]", result.stderr)
        self.assertEqual(read(self.root + "/etc/vconsole.conf"), STOCK_VCONSOLE)
        leftovers = [path for path in (scramble.XKB_EXTRA + "/symbols/qubes-scramble", scramble.RUNTIME_THEME_FILE,
                                       "/etc/vconsole.conf.qubes-scramble-orig", "/etc/vconsole.conf.tmp")
                     if os.path.exists(self.root + path)]
        self.assertEqual(leftovers, [])

    def test_console_stays_stock_while_plymouth_runs(self):
        write_image_conf(self.root, "[scramble]\nconsole = yes\n")
        self.assertEqual(run_hook(self.root).returncode, 0)
        vconsole = scramble.read_vconsole(self.root + "/etc/vconsole.conf")
        self.assertEqual((vconsole.get("KEYMAP_TOGGLE"), vconsole.get("XKBLAYOUT")), (None, "qubes-scramble,qubes-scramble"))
        self.assertFalse(os.path.exists(self.root + scramble.CONSOLE_MAP))

    def test_console_scrambling_and_restore(self):
        write(self.root + "/proc/cmdline", "root=/dev/x plymouth.enable=0 quiet\n")
        write_image_conf(self.root, "[scramble]\nconsole = yes\n[digits]\n2 = ABC\n")
        result = run_hook(self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Disk password letters: 2 ABC", result.stdout)
        vconsole = scramble.read_vconsole(self.root + "/etc/vconsole.conf")
        self.assertEqual((vconsole["KEYMAP"], vconsole.get("XKBLAYOUT")), ("us", None))
        scramble.check_console_map(self.root, vconsole)
        self.assertFalse(os.path.exists(self.root + scramble.RUNTIME_THEME_FILE))
        self.assertEqual(run_hook(self.root, "restore").returncode, 0)
        self.assertEqual(read(self.root + "/etc/vconsole.conf"), STOCK_VCONSOLE)
        self.assertFalse(os.path.exists(self.root + scramble.CONSOLE_MAP))

    def test_plymouth_disabled_without_console_scrambling(self):
        write(self.root + "/proc/cmdline", "root=/dev/x rd.plymouth=0 quiet\n")
        self.assertEqual(run_hook(self.root).returncode, 0)
        self.assertEqual(read(self.root + "/etc/vconsole.conf"), STOCK_VCONSOLE)
        self.assertFalse(os.path.exists(self.root + scramble.RUNTIME_THEME_FILE))

    def test_unknown_action(self):
        result = run_hook(self.root, "bogus")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown action", result.stderr)

    def test_maps_are_uniform(self):
        runs = 1200
        counts = [[0] * 10 for _ in range(10)]
        for _ in range(runs):
            write(self.root + "/etc/vconsole.conf", STOCK_VCONSOLE)
            result = run_hook(self.root)
            self.assertEqual(result.returncode, 0, result.stderr)
            for key, digit in enumerate(self.state()[1]):
                counts[key][int(digit)] += 1
        expected, limit = runs / 10, 6 * math.sqrt(runs * 0.1 * 0.9)
        for key in range(10):
            for digit in range(10):
                self.assertLess(abs(counts[key][digit] - expected), limit, f"key {key} digit {digit}")


class ValidateTreeTest(unittest.TestCase):
    """validate_tree on a minimal image tree built like the dracut module does."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="scramble-image-")
        self.addCleanup(shutil.rmtree, self.root)
        copy_image_xkb(self.root)
        write_image_conf(self.root)
        write(self.root + "/etc/vconsole.conf", STOCK_VCONSOLE)
        write(self.root + scramble.THEME_FILE, STOCK_THEME)
        write(self.root + scramble.THEME_SCRIPT, STOCK_SCRIPT + scramble.render_overlay(config_from(""), read(TEMPLATE)))
        write(self.root + scramble.IMAGE_HOOK, read(HOOK))
        for wants, unit in ((scramble.IMAGE_WANTS, "qubes-scramble.service"),
                            (scramble.IMAGE_RESTORE_WANTS, "qubes-scramble-restore.service")):
            os.makedirs(os.path.dirname(self.root + wants), exist_ok=True)
            os.symlink(f"../{unit}", self.root + wants)
        for name in scramble.IMAGES:
            shutil.copyfile(os.path.join(SOURCE, "theme", name), f"{self.root}{scramble.THEME_DIR}/{name}")

    def test_valid_tree(self):
        for text in ("", "[scramble]\nconsole = yes\n"):
            with self.subTest(text=text):
                write_image_conf(self.root, text)
                scramble.validate_tree(self.root)
        self.assertEqual(read(self.root + "/etc/vconsole.conf"), STOCK_VCONSOLE, "validation must not touch the tree")

    def test_broken_images_are_refused(self):
        hook = read(HOOK)
        mutations = [(f"missing {path}", lambda path=path: os.unlink(self.root + path), "missing in the image")
                     for path in (scramble.IMAGE_CONF, scramble.IMAGE_HOOK, scramble.IMAGE_WANTS, scramble.IMAGE_RESTORE_WANTS,
                                  scramble.THEME_FILE, scramble.XKB_ROOT + "/rules/evdev",
                                  f"{scramble.THEME_DIR}/{scramble.IMAGES[0]}")]
        mutations += [
            ("no overlay", lambda: write(self.root + scramble.THEME_SCRIPT, STOCK_SCRIPT), "no keypad overlay"),
            ("theme changed", lambda: write(self.root + scramble.THEME_SCRIPT, read(self.root + scramble.THEME_SCRIPT)
                                            .replace("fun display_prompt_callback", "fun prompt_callback", 1)),
             "lacks fun display_prompt_callback"),
            ("hook does nothing", lambda: write(self.root + scramble.IMAGE_HOOK, "exit 0\n"), "not a permutation"),
            ("restore keeps the map", lambda: write(self.root + scramble.IMAGE_HOOK, hook.replace(
                'rm -f "$symbols" "$root$RUNTIME_THEME_FILE"', ":")), "restore step leaves"),
            ("other layout", lambda: write_image_conf(self.root, layout="de"), "another keyboard layout"),
            ("console map broken", lambda: (write_image_conf(self.root, "[scramble]\nconsole = yes\n"), write(
                self.root + scramble.IMAGE_HOOK, hook.replace("names[${map:key:1}]", "names[0]"))),
             "console keymap is not a permutation"),
            ("base layout missing", lambda: os.unlink(self.root + scramble.XKB_ROOT + "/symbols/us"), "cannot compile"),
            ("grp_led missing", lambda: os.unlink(self.root + scramble.XKB_ROOT + "/compat/grp_led"), "cannot compile"),
        ]
        snapshot = tempfile.mkdtemp(prefix="scramble-image-copy-")
        self.addCleanup(shutil.rmtree, snapshot)
        shutil.copytree(self.root, snapshot, symlinks=True, dirs_exist_ok=True)
        for name, mutate, message in mutations:
            with self.subTest(mutation=name):
                shutil.rmtree(self.root)
                shutil.copytree(snapshot, self.root, symlinks=True)
                mutate()
                with self.assertRaisesRegex(scramble.ScrambleError, message):
                    scramble.validate_tree(self.root)


class ConfirmTest(unittest.TestCase):
    """The change list and question before install and uninstall change anything."""

    CHANGES = (("add", "/etc/x.conf", "copy of y"), ("rebuild", "/boot/initramfs-k.img", ""))

    def ask(self, answer, tty=True, assume_yes=False):
        output = io.StringIO()
        reply = {"side_effect": answer} if isinstance(answer, type) else {"return_value": answer}
        with mock.patch("sys.stdin", mock.Mock(isatty=lambda: tty)), mock.patch("builtins.input", **reply), \
                contextlib.redirect_stdout(output):
            return scramble.confirm("install", self.CHANGES, assume_yes), output.getvalue()

    def test_lists_changes_and_accepts_yes(self):
        for answer in ("y", " YES "):
            with self.subTest(answer=answer):
                accepted, output = self.ask(answer)
                self.assertTrue(accepted)
                self.assertIn("  add      /etc/x.conf  (copy of y)\n  rebuild  /boot/initramfs-k.img\n", output)
                self.assertIn("Nothing else changes", output)

    def test_anything_else_cancels(self):
        for answer, tty, message in (("", True, "cancelled"), ("n", True, "cancelled"), ("yes please", True, "cancelled"),
                                     (EOFError, True, "cancelled"), (KeyboardInterrupt, True, "cancelled"),
                                     ("y", False, "pass --yes")):
            with self.subTest(answer=answer, tty=tty), self.assertRaisesRegex(scramble.ScrambleError, message):
                self.ask(answer, tty)

    def test_yes_option_and_no_changes(self):
        self.assertTrue(self.ask("n", tty=False, assume_yes=True)[0])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(scramble.confirm("uninstall", [], False))
        self.assertIn("nothing to change", output.getvalue())


class FlowTestCase(unittest.TestCase):
    """A temporary root for the paths install and uninstall change."""

    def setUp(self):
        self.top = tempfile.mkdtemp(prefix="scramble-flow-")
        self.addCleanup(shutil.rmtree, self.top)
        self.running = os.uname().release
        write(self.image(self.running), "stock image")
        write(self.top + "/etc/vconsole.conf", STOCK_VCONSOLE)
        for directory in ("/modules.d", "/sbin", "/lib/modules/" + self.running):
            os.makedirs(self.top + directory)
        self.patch(MODULE_DIR=self.top + "/modules.d/91qubes-scramble", CONFIG_FILE=self.top + "/etc/scramble.conf",
                   VCONSOLE_CONF=self.top + "/etc/vconsole.conf", INSTALLED_CLI=self.top + "/sbin/qubes-plymouth-scramble",
                   KERNEL_MODULES=self.top + "/lib/modules", boot_image=self.image)
        sync = mock.patch("os.sync")
        sync.start()
        self.addCleanup(sync.stop)

    def patch(self, **names):
        for name, value in names.items():
            patcher = mock.patch.object(scramble, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def image(self, kernel):
        return f"{self.top}/boot/initramfs-{kernel}.img"


class InstallFlowTest(FlowTestCase):
    """install and uninstall with the image builds recorded: the module, the settings file and
    the link change together or not at all."""

    def setUp(self):
        super().setUp()
        self.builds, self.failing, self.hooked = [], False, []

        def build(kernels, with_hook):
            self.builds.append((list(kernels), with_hook))
            if self.failing:
                raise scramble.ScrambleError("image validation failed")

        self.patch(build_and_switch=build, check_theme=lambda: None, hooked_kernels=lambda: list(self.hooked))

    def run_command(self, command, config_text=None):
        path = None
        if config_text is not None:
            path = self.top + "/input/given.conf"
            write(path, config_text)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            command(mock.Mock(config=path, yes=True))
        return output.getvalue()

    def state(self):
        """Every file below the root except the images and test inputs, with its content or link target."""
        return {os.path.relpath(path, self.top): os.readlink(path) if os.path.islink(path) else read_bytes(path)
                for path in glob.glob(self.top + "/**", recursive=True)
                if (os.path.isfile(path) or os.path.islink(path)) and not re.search("/(boot|input)/", path)}

    def test_install_adds_module_settings_and_link(self):
        output = self.run_command(scramble.cmd_install)
        self.assertEqual(self.builds, [([self.running], True)])
        self.assertIn("BASE_LAYOUT='us'\n", read(scramble.MODULE_DIR + "/qubes-plymouth-scramble.conf"))
        self.assertEqual(read(scramble.CONFIG_FILE), read(os.path.join(SOURCE, "qubes-plymouth-scramble.conf")))
        self.assertEqual(os.readlink(scramble.INSTALLED_CLI), scramble.MODULE_DIR + "/source/" + scramble.PROG)
        for text in (f"add      {scramble.MODULE_DIR}", f"add      {scramble.CONFIG_FILE}", "keyboard layout us",
                     f"rebuild  {self.image(self.running)}"):
            self.assertIn(text, output)
        self.assertEqual(os.listdir(self.top + "/modules.d"), ["91qubes-scramble"], "no staging leftovers")

    def test_install_follows_the_system_layout_and_kernels_with_the_module(self):
        write(scramble.VCONSOLE_CONF, 'KEYMAP="uk"\n')
        self.hooked = ["0.0.1-other"]
        self.run_command(scramble.cmd_install)
        self.assertEqual(self.builds, [(sorted([self.running, "0.0.1-other"]), True)])
        self.assertIn("BASE_LAYOUT='gb'\n", read(scramble.MODULE_DIR + "/qubes-plymouth-scramble.conf"))

    def test_failed_install_changes_nothing(self):
        self.failing = True
        with self.assertRaisesRegex(scramble.ScrambleError, "validation failed"):
            self.run_command(scramble.cmd_install)
        self.assertEqual(self.state(), {"etc/vconsole.conf": STOCK_VCONSOLE.encode()})
        self.failing = False
        self.run_command(scramble.cmd_install)
        installed, self.failing = self.state(), True
        with self.assertRaises(scramble.ScrambleError):
            self.run_command(scramble.cmd_install, "[scramble]\nconsole = yes\n")
        self.assertEqual(self.state(), installed, "the previous installation is restored")

    def test_disable_keeps_the_tool_and_builds_stock_images(self):
        self.run_command(scramble.cmd_install)
        self.hooked = [self.running]
        write(scramble.CONFIG_FILE, "[scramble]\nenabled = no\n")
        output = self.run_command(scramble.cmd_install)
        self.assertEqual(self.builds[-1], ([self.running], False))
        self.assertEqual(sorted(os.listdir(scramble.MODULE_DIR)), ["module-setup.sh", "source"])
        self.assertTrue(os.path.isfile(scramble.INSTALLED_CLI), "the link still reaches the tool")
        self.assertIn("disabled", output)

    def test_uninstall_removes_everything_or_nothing(self):
        self.run_command(scramble.cmd_install)
        installed, self.hooked, self.failing = self.state(), [self.running], True
        with self.assertRaises(scramble.ScrambleError):
            self.run_command(scramble.cmd_uninstall)
        self.assertEqual(self.state(), installed)
        self.failing = False
        output = self.run_command(scramble.cmd_uninstall)
        self.assertEqual(self.builds[-1], ([self.running], False))
        self.assertEqual(self.state(), {"etc/vconsole.conf": STOCK_VCONSOLE.encode()})
        self.assertEqual(os.listdir(self.top + "/modules.d"), [])
        self.assertIn(f"remove   {scramble.CONFIG_FILE}", output)
        self.hooked = []
        self.assertIn("nothing to change", self.run_command(scramble.cmd_uninstall))

    def test_status(self):
        self.patch(image_has_hook=lambda image: True)
        self.run_command(scramble.cmd_install)
        output = self.run_command(scramble.cmd_status)
        for text in ("dracut module:      installed", f"initramfs:          {self.image(self.running)} scrambles"):
            self.assertIn(text, output)


class BuildAndSwitchTest(FlowTestCase):
    """The image switch with dracut and lsinitrd recorded: the dracut call of kernel-install,
    and new images replace the current ones only when all of them passed."""

    def setUp(self):
        super().setUp()
        self.kernels = sorted([self.running, "0.0.1-other"])
        write(self.image("0.0.1-other"), "stock image")
        self.commands, self.hook_in_image, self.valid = [], True, {kernel: True for kernel in self.kernels}

        def run(command, **kwargs):
            self.commands.append(command)
            if command[0] == "dracut":
                write(command[-1], "new image " + command[command.index("--kver") + 1])
            elif self.hook_in_image:
                os.makedirs(os.path.dirname(kwargs["cwd"] + scramble.IMAGE_WANTS))
                os.symlink("../qubes-scramble.service", kwargs["cwd"] + scramble.IMAGE_WANTS)

        def validate(_unpacked):
            kernel = [command for command in self.commands if command[0] == "dracut"][-1][5]
            expect = self.valid[kernel]
            if not expect:
                raise scramble.ScrambleError(f"broken image for {kernel}")

        self.patch(run=run, validate_tree=validate, boot_layout=lambda kernel: "other")

    def leftovers(self):
        return [name for name in os.listdir(self.top + "/boot") if name.endswith("-new")]

    def test_switch_after_all_images_passed(self):
        scramble.build_and_switch(self.kernels, with_hook=True)
        for kernel in self.kernels:
            self.assertEqual(read(self.image(kernel)), "new image " + kernel)
            self.assertIn(["dracut", "-f", "--kernel-image", f"/boot/vmlinuz-{kernel}", "--kver", kernel,
                           self.image(kernel) + f".{scramble.PROG}-new"], self.commands)
        self.assertEqual(self.leftovers(), [])

    def test_dracut_call_follows_the_boot_layout(self):
        image = self.image(self.running)
        for layout, extra in (("other", []), ("bls", ["--no-uefi"])):
            with self.subTest(layout=layout), mock.patch.object(scramble, "boot_layout", lambda kernel, layout=layout: layout):
                self.assertEqual(scramble.dracut_command(self.running, image),
                                 ["dracut", "-f", "--kernel-image", f"/boot/vmlinuz-{self.running}", *extra,
                                  "--kver", self.running, image])

    def test_one_failure_keeps_every_image(self):
        self.valid[self.kernels[-1]] = False
        with self.assertRaisesRegex(scramble.ScrambleError, "broken image"):
            scramble.build_and_switch(self.kernels, with_hook=True)
        self.assertEqual([read(self.image(kernel)) for kernel in self.kernels], ["stock image"] * 2)
        self.assertEqual(self.leftovers(), [])

    def test_hook_presence_must_match(self):
        for with_hook, in_image, message in ((False, True, "still has"), (True, False, "has no scramble hook")):
            with self.subTest(with_hook=with_hook):
                self.hook_in_image = in_image
                with self.assertRaisesRegex(scramble.ScrambleError, message):
                    scramble.build_and_switch(self.kernels, with_hook)
                self.assertEqual(read(self.image(self.kernels[0])), "stock image")


class BootLayoutTest(unittest.TestCase):
    """The layout kernel-install reports decides the dracut options, as in 50-dracut.install."""

    def test_layouts(self):
        for output, expected in (("  Layout: other\n", "other"), ("     Layout: bls\nBoot Root: /boot/efi\n", "bls")):
            with self.subTest(output=output), mock.patch("subprocess.run", return_value=mock.Mock(stdout=output)):
                self.assertEqual(scramble.boot_layout("6.1"), expected)

    def test_unsupported_or_unknown(self):
        for output, message in (("Layout: uki\n", "does not support"), ("", "shows no layout")):
            with self.subTest(output=output), mock.patch("subprocess.run", return_value=mock.Mock(stdout=output, stderr="")), \
                    self.assertRaisesRegex(scramble.ScrambleError, message):
                scramble.boot_layout("6.1")
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("kernel-install")), \
                self.assertRaisesRegex(scramble.ScrambleError, "cannot run kernel-install"):
            scramble.boot_layout("6.1")


class MainTest(unittest.TestCase):
    def setUp(self):
        for number in (signal.SIGTERM, signal.SIGHUP):
            self.addCleanup(signal.signal, number, signal.getsignal(number))

    def test_errors_are_reported_without_traceback(self):
        for argv, message in ((["status"], "run as root"), (["validate-tree", "/nonexistent"], "missing in the image")):
            with self.subTest(argv=argv), mock.patch("os.geteuid", return_value=1000), \
                    contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(scramble.main(argv), 1)
            self.assertIn(f"{scramble.PROG}: error: ", errors.getvalue())
            self.assertIn(message, errors.getvalue())


class LockTest(unittest.TestCase):
    def test_second_run_is_refused(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(scramble, "MODULE_DIR", os.path.join(directory, "91qubes-scramble")):
            descriptor = scramble.lock_modules_dir()
            self.addCleanup(os.close, descriptor)
            with self.assertRaisesRegex(scramble.ScrambleError, "in progress"):
                scramble.lock_modules_dir()


class FuzzTest(unittest.TestCase):
    """Seeded random inputs: parsers fail only with ScrambleError, generated shell settings
    round-trip through bash, and the boot hook edits vconsole.conf exactly and restores it."""

    def setUp(self):
        self.random = random.Random(20261029)

    def test_config_parser(self):
        fragments = ["[scramble]", "[digits]", "[DEFAULT]", "[other]", "[scramble", "enabled = yes", "enabled = no",
                     "enabled = maybe", "console = 1", "console =", "layout = us", "0 = ABC", "9 = WXYZ #00ff00",
                     "5 = abc", "x = 1", "1 = ", "2 = TOOLONGNAME", "3 = AB #12", "= value", "novalue", "  indented = 1",
                     "# comment", "; comment", "enabled: yes", "%(x)s = 1", "1 = A\n  continuation", "\x00", "\u00e9 = 1"]
        valid = 0
        for _ in range(3000):
            text = "\n".join(self.random.choice(fragments) for _ in range(self.random.randint(0, 8)))
            try:
                config = config_from(text)
            except scramble.ScrambleError:
                continue
            valid += 1
            self.assertIsInstance(config.enabled, bool)
            self.assertIsInstance(config.console, bool)
            for letters, colour in zip(config.letters, config.colors):
                self.assertRegex(letters, r"^[A-Z]{0,8}$")
                self.assertTrue(len(colour) == 3 and all(0 <= channel <= 1 for channel in colour))
        self.assertGreater(valid, 100, "the generator must also produce valid configurations")

    def test_parsers_on_random_bytes(self):
        lines = [b"KEYMAP=us", b"XKBLAYOUT=de", b"XKBVARIANT=", b"[script-env-vars]", b"qubes_scramble_map=0123456789",
                 b"[Plymouth Theme]", b"x", b"=", b"KEYMAP='de-latin1-nodeadkeys'", b"\xff\xfe", b"\x00", b"a=b=c"]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "input")
            for _ in range(1500):
                if self.random.random() < 0.5:
                    data = self.random.randbytes(self.random.randint(0, 200))
                else:
                    data = b"\n".join(self.random.choice(lines) for _ in range(self.random.randint(0, 6)))
                write(path, data)
                for parse in (lambda: scramble.Config(path), lambda: scramble.read_theme_map(path),
                              lambda: scramble.system_layout(scramble.read_vconsole(path), path)):
                    try:
                        parse()
                    except scramble.ScrambleError:
                        pass

    def test_module_conf_round_trips_through_bash(self):
        script = '. /dev/stdin; printf "%s\\0" "$BASE_LAYOUT" "$HINT" "$CONSOLE_SCRAMBLE"'
        for _ in range(150):
            letters = ["".join(self.random.choices(string.ascii_uppercase, k=self.random.randint(0, 8))) for _ in range(10)]
            console = self.random.choice(["yes", "no"])
            config = config_from(f"[scramble]\nconsole = {console}\n[digits]\n" +
                                 "".join(f"{digit} = {group}\n" for digit, group in enumerate(letters)))
            layout = self.random.choice(["us", "de(nodeadkeys)", "gb", "fr(bepo_afnor)", "a-b_c(D-1)"])
            result = subprocess.run(["bash", "-c", script], input=scramble.module_conf(config, layout),
                                    capture_output=True, text=True, check=True, env=HOOK_ENV)
            self.assertEqual(result.stdout.split("\0")[:3], [layout, config.hint, str(int(config.console))])

    def test_hook_edits_vconsole_exactly(self):
        pool = [b'KEYMAP="us"', b"KEYMAP=us", b"KEYMAP='us'", b'FONT="eurlatgr"', b"# comment", b"", b"  ",
                b"XKBLAYOUT=de", b"XKBLAYOUT = de", b"  XKBVARIANT=nodeadkeys", b"\tXKBOPTIONS=ctrl:nocaps", b"XKBMODEL=pc104",
                b"KEYMAP_TOGGLE=foo", b"FONT_MAP=8859-1", b"UNKNOWN=1", b"XKBLAYOUTX=keep", b"#XKBLAYOUT=keep",
                b"KEYMAP=us\r", b"x" * 300]
        managed = re.compile(rb"^[ \t]*(XKB(LAYOUT|MODEL|VARIANT|OPTIONS)|KEYMAP_TOGGLE)[ \t]*=")
        root = tempfile.mkdtemp(prefix="scramble-fuzz-")
        self.addCleanup(shutil.rmtree, root)
        for _ in range(150):
            original = b"\n".join(self.random.choices(pool, k=self.random.randint(0, 7))) + self.random.choice([b"", b"\n"])
            console, failing = self.random.random() < 0.3, self.random.random() < 0.2
            write(root + "/etc/vconsole.conf", original)
            write(root + scramble.THEME_FILE, STOCK_THEME + ("[script-env-vars]\nx=1\n" if failing else ""))
            write(root + "/proc/cmdline", "plymouth.enable=0\n" if console else "quiet\n")
            write_image_conf(root, "[scramble]\nconsole = yes\n" if console else "")
            result = run_hook(root)
            edited = read_bytes(root + "/etc/vconsole.conf")
            if failing and not console:
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(edited, original, "a failed hook leaves vconsole.conf as it was")
                continue
            self.assertEqual(result.returncode, 0, result.stderr)
            kept = [line for line in original.split(b"\n")[:-1 if original.endswith(b"\n") else None]
                    if not managed.match(line)] if original else []
            added = [f"KEYMAP_TOGGLE={scramble.CONSOLE_MAP}".encode()] if console else [line.encode() for line in XKB_LINES]
            self.assertEqual(edited, b"".join(line + b"\n" for line in kept + added), repr(original))
            self.assertEqual(run_hook(root, "restore").returncode, 0)
            self.assertEqual(read_bytes(root + "/etc/vconsole.conf"), original, "restore is byte-exact")
            self.assertEqual(scramble.etc_run_files(root).keys(), {"etc/vconsole.conf", "etc/qubes-plymouth-scramble.conf"})


if __name__ == "__main__":
    unittest.main()

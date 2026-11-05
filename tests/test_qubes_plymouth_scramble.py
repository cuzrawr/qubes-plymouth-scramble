"""Tests for qubes-plymouth-scramble: generator, boot hook and image validation.

Needs bash 5.1 or later, libxkbcommon, xkeyboard-config and systemd's kbd-model-map (as in Qubes
dom0 or Fedora). The fuzz tests use fixed seeds, so every run checks the same inputs.
"""

import contextlib
import glob
import importlib.machinery
import importlib.util
import io
import json
import math
import os
import random
import re
import shutil
import signal
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
# Digit tiles as install makes them for a 160 ppi screen (the tool's TILE_MM, DIGIT_MM and BUTTON_SHARE)
PPI, TILE_PX, GLYPH_PX, BUTTON_PX = 160, 56, 35, 45
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
# Files below /etc and /run of a test root before the hook runs
BASE_FILES = {"etc/vconsole.conf", "etc/qubes-plymouth-scramble.conf"}
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


def write_image_conf(root, layout="us"):
    write(root + scramble.IMAGE_CONF, scramble.module_conf(layout))


def imaging_image(data):
    """A PNG from bytes as a Pillow image."""
    return scramble.imaging()[1].Image.open(io.BytesIO(data))


def bold_font():
    """A bold sans-serif font of the system (masked digit tests), or None."""
    try:
        path = subprocess.run(["fc-match", "-f", "%{file}", "Sans:bold"], capture_output=True, text=True,
                              check=False).stdout
    except OSError:
        return None
    return path if os.path.isfile(path) else None


def synthetic_edid(width, height, width_mm, height_mm, *, width_cm=0, height_cm=0):
    """An EDID base block with one detailed timing for the mode and image size given."""
    data = bytearray(128)
    data[:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    data[21], data[22] = width_cm, height_cm
    timing = bytearray(18)
    timing[0], timing[1] = 0x01, 0x1D  # pixel clock, any non-zero value
    timing[2], timing[4] = width & 0xFF, (width >> 4) & 0xF0
    timing[5], timing[7] = height & 0xFF, (height >> 4) & 0xF0
    timing[12], timing[13] = width_mm & 0xFF, height_mm & 0xFF
    timing[14] = ((width_mm >> 4) & 0xF0) | ((height_mm >> 8) & 0x0F)
    data[54:72] = timing
    data[127] = -sum(data[:127]) % 256
    return bytes(data)


def run_hook(root, *args):
    """Runs the boot hook on root with the umask systemd gives services (0022)."""
    return subprocess.run(["bash", "-c", 'umask 022 && exec bash "$0" "$@"', HOOK, *args], capture_output=True,
                          text=True, check=False, env={**HOOK_ENV, "QUBES_SCRAMBLE_ROOT": root})


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        config = config_from("")
        self.assertEqual((config.enabled, config.letters, config.colors), (True, [""] * 10, [(1.0, 1.0, 1.0)] * 10))

    def test_values(self):
        config = config_from("[scramble]\nenabled = no\ndisplay = text\n[digits]\n2 = ABC #ff8000\n7 = PQRS\n")
        self.assertFalse(config.enabled)
        self.assertEqual((config.letters[2], config.colors[2], config.letters[7], config.colors[7]),
                         ("ABC", (1.0, 128 / 255, 0.0), "PQRS", (1.0, 1.0, 1.0)))

    def test_invalid(self):
        for text in ("[scramble]\nlayout = us\n", "[scramble]\nenabled = maybe\n", "[scramble]\ncolour = red\n",
                     "[digits]\n12 = AB\n", "[digits]\n3 = abc\n", "[digits]\n3 = AB'C\n", "[digits]\n3 = ABCDEFGHI\n",
                     "[digits]\n3 = ABC #12345\n", "[other]\nx = 1\n", "no section\n", "[digits]\n1 = A\n1 = B\n"):
            with self.subTest(text=text), self.assertRaises(scramble.ScrambleError):
                config_from(text)

    def test_unknown_settings_are_named_with_the_valid_ones(self):
        with self.assertRaisesRegex(scramble.ScrambleError, r"unknown sections or settings: console, layout \(valid are "
                                                            r"enabled, display, safety_distance, .*, ppi in \[scramble\] "
                                                            r"and the digits 0-9 in \[digits\]\)"):
            config_from("[scramble]\nenabled = yes\nconsole = no\nlayout = us\n")

    def test_masked_digit_settings(self):
        config = config_from("")
        self.assertEqual((config.display, config.highpass_filter, config.safety_distance, config.highpass_cutoff,
                          config.camera_floor, config.lowpass_sigma, config.quantization, config.ppi),
                         ("hybrid", "tapered", 114.0, None, scramble.CAMERA_FLOOR, scramble.PAPER_SIGMA_LF, "diffusion",
                          None))
        config = config_from("[scramble]\ndisplay = hybrid\nsafety_distance = 152\nhighpass_filter = gaussian\n"
                             "highpass_cutoff = 1.63\ncamera_floor = 0\nlowpass_sigma = 0.3\nquantization = compensate\n"
                             "ppi = 157\n")
        self.assertEqual((config.display, config.highpass_filter, config.safety_distance, config.highpass_cutoff,
                          config.camera_floor, config.lowpass_sigma, config.quantization, config.ppi),
                         ("hybrid", "gaussian", 152.0, 1.63, 0.0, 0.3, "compensate", 157.0))
        self.assertIsNone(config_from("[scramble]\nhighpass_cutoff = auto\n").highpass_cutoff)
        for text, message in (("display = hybrid2", "display = hybrid2: expected text, highpass, hybrid, white"),
                              ("highpass_filter = sinc", "expected tapered or gaussian"),
                              ("quantization = dither", "quantization = dither: expected diffusion, round, compensate"),
                              ("safety_distance = 5", "safety_distance = 5: expected a number from 20 to 1000 [(]cm[)]"),
                              ("safety_distance = far", "safety_distance = far: expected a number"),
                              ("highpass_cutoff = 0", "highpass_cutoff = 0: expected a number from 0.1 to 10"),
                              ("camera_floor = -1", "camera_floor = -1: expected a number from 0 to 10"),
                              ("ppi = nan", "ppi = nan: expected a number from 50 to 1000"),
                              ("size = 4", "size = 4: expected a number from 0.5 to 3"),
                              ("panel = maybe", "cannot read configuration")):
            with self.subTest(text=text), self.assertRaisesRegex(scramble.ScrambleError, message):
                config_from(f"[scramble]\n{text}\n")
        with self.assertRaisesRegex(scramble.ScrambleError, "display = white draws the digits as images without letters"):
            config_from("[scramble]\ndisplay = white\n[digits]\n2 = ABC\n")
        self.assertEqual(config_from("[scramble]\ndisplay = text\n[digits]\n2 = ABC\n").letters[2], "ABC")


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
        overlay = scramble.render_overlay(config_from("[scramble]\ndisplay = text\n[digits]\n2 = ABC #ff0000\n"), read(TEMPLATE))
        self.assertIn(scramble.OVERLAY_MARKER, overlay)
        self.assertIn('qubes_scramble.letters["2"] = "ABC"; qubes_scramble.red["2"] = 1.000; '
                      'qubes_scramble.green["2"] = 0.000;', overlay)
        self.assertNotIn("@", overlay)
        overlay.encode("ascii")
        # The stock qubes-dark F1/F2/F8 handler compares escape sequences that start with ESC
        for sequence in ('"\\e[[A"', '"\\e[[B"', '"\\e[19~"'):
            self.assertIn(f"keyboard_input_callback({sequence})", overlay)

    def test_overlay_with_tile_sets(self):
        text = scramble.render_overlay(config_from(""), read(TEMPLATE))
        self.assertIn("qubes_scramble.sets = 0;", text)
        manifest = {"display": "hybrid", "sets": [
            {"prefix": "qubes-scramble-tile0-", "cell": 56, "width": 1920, "height": 1080},
            {"prefix": "qubes-scramble-tile1-", "cell": 40, "width": 960, "height": 600}]}
        text = scramble.render_overlay(config_from("[scramble]\ndisplay = hybrid\n"), read(TEMPLATE), manifest)
        self.assertIn("qubes_scramble.sets = 2;", text)
        self.assertIn('qubes_scramble.set[1].prefix = "qubes-scramble-tile1-"; qubes_scramble.set[1].keyed = 1; '
                      "qubes_scramble.set[1].cell = 40; qubes_scramble.set[1].width = 960; "
                      "qubes_scramble.set[1].height = 600;", text)
        self.assertNotIn("@", text)

    def test_module_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            scramble.write_module(directory, config_from("[scramble]\ndisplay = text\n[digits]\n2 = ABC\n"), "us")
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
        """The settings the boot hook and module-setup.sh source give exactly these values in bash."""
        script = 'set -u; . /dev/stdin; printf "%s\\0" "$BASE_LAYOUT" "$THEME_FILE" "$RUNTIME_THEME_FILE" "$THEME_SCRIPT"'
        text = scramble.module_conf("de(nodeadkeys)")
        result = subprocess.run(["bash", "-c", script], input=text, capture_output=True, text=True, check=True, env=HOOK_ENV)
        self.assertEqual(result.stdout.split("\0")[:4],
                         ["de(nodeadkeys)", scramble.THEME_FILE, scramble.RUNTIME_THEME_FILE, scramble.THEME_SCRIPT])
        self.assertEqual(len(text.splitlines()), 4)


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
        for path in ("/proc/cmdline", scramble.XKB_EXTRA + "/symbols/qubes-scramble",
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
        self.assertEqual(scramble.etc_run_files(self.root).keys(), BASE_FILES | {
            "etc/vconsole.conf.qubes-scramble-orig", "etc/xkb/symbols/qubes-scramble", scramble.RUNTIME_THEME_FILE[1:]})
        self.assertEqual(result.stdout + result.stderr, "", "the hook prints nothing, the map is never logged")
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
        for command in installed - {"bash"}:
            self.assertRegex(code, rf"(^|[\s;|&(]){command}\s", f"the dracut module installs {command}, unused by the hook")

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

    def test_plymouth_disabled_changes_nothing(self):
        """The text console prompt of a boot without Plymouth stays stock."""
        files = scramble.etc_run_files(self.root)
        for cmdline in ("root=/dev/x rd.plymouth=0 quiet", "plymouth.enable=0"):
            with self.subTest(cmdline=cmdline):
                write(self.root + "/proc/cmdline", cmdline + "\n")
                for action in ("scramble", "restore"):
                    result = run_hook(self.root, action)
                    self.assertEqual((result.returncode, result.stdout + result.stderr), (0, ""))
                    self.assertEqual(scramble.etc_run_files(self.root), files)

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
        scramble.validate_tree(self.root)
        self.assertEqual(read(self.root + "/etc/vconsole.conf"), STOCK_VCONSOLE, "validation must not touch the tree")

    @unittest.skipUnless(importlib.util.find_spec("numpy") and importlib.util.find_spec("PIL") and bold_font(),
                         "masked digits need numpy, Pillow and a bold sans-serif font")
    def test_masked_tree(self):
        config = config_from("[scramble]\ndisplay = highpass\nhighpass_cutoff = 1\n")
        files, manifest = scramble.make_tiles(config, [("HDMI-A-1", 1920, 1080, 527, 296)], bold_font())
        overlay = scramble.render_overlay(config, read(TEMPLATE), manifest)
        write(self.root + scramble.THEME_SCRIPT, STOCK_SCRIPT + overlay)
        for name, data in files.items():
            write(f"{self.root}{scramble.THEME_DIR}/{name}", data)
        write(f"{self.root}{scramble.THEME_DIR}/{scramble.TILE_MANIFEST}", json.dumps(manifest))
        scramble.validate_tree(self.root)
        text_overlay = scramble.render_overlay(config_from("[scramble]\ndisplay = text\n[digits]\n2 = ABC\n"),
                                               read(TEMPLATE), manifest)
        for broken, message in ((overlay.replace("sets = 1;", "sets = 2;"), "list different tile sets"),
                                (text_overlay, "show letters"), (overlay.replace("qubes_scramble.sets = 1;", ""),
                                                                 "declares no tile sets")):
            with self.subTest(message=message), self.assertRaisesRegex(scramble.ScrambleError, message):
                write(self.root + scramble.THEME_SCRIPT, STOCK_SCRIPT + broken)
                scramble.validate_tree(self.root)
        write(self.root + scramble.THEME_SCRIPT, STOCK_SCRIPT + overlay)
        os.unlink(f"{self.root}{scramble.THEME_DIR}/qubes-scramble-tile0-4.png")
        with self.assertRaisesRegex(scramble.ScrambleError, "tile qubes-scramble-tile0-4.png is not readable"):
            scramble.validate_tree(self.root)

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
            ("scrambles without Plymouth", lambda: write(self.root + scramble.IMAGE_HOOK, hook.replace(
                "if plymouth_disabled; then", "if false; then")), "with Plymouth disabled the boot hook changes files"),
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

        self.patch(build_and_switch=build, check_theme=lambda: None, hooked_kernels=lambda: list(self.hooked),
                   connected_displays=lambda: [("HDMI-A-1", 1920, 1080, 527, 296)], system_font=bold_font)

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
                     f"rebuild  {self.image(self.running)}", "hybrid digit tiles"):
            self.assertIn(text, output)
        self.assertEqual(os.listdir(self.top + "/modules.d"), ["91qubes-scramble"], "no staging leftovers")

    @unittest.skipUnless(importlib.util.find_spec("numpy") and importlib.util.find_spec("PIL") and bold_font(),
                         "masked digits need numpy, Pillow and a bold sans-serif font")
    def test_masked_install_writes_tiles(self):
        output = self.run_command(scramble.cmd_install, "[scramble]\ndisplay = highpass\nhighpass_cutoff = 1\n")
        with open(f"{scramble.MODULE_DIR}/tiles/{scramble.TILE_MANIFEST}", encoding="ascii") as stream:
            manifest = json.load(stream)
        self.assertEqual(sorted(os.listdir(scramble.MODULE_DIR + "/tiles")),
                         sorted([scramble.TILE_MANIFEST, *manifest["sets"][0]["files"], manifest["sets"][0]["panel"]["name"]]))
        self.assertIn("highpass digit tiles (tapered filter, cut-off 1.0 cycles per mm, quantization diffusion) for "
                      "HDMI-A-1 1920x1080", output)
        self.assertIn("qubes_scramble.sets = 1;", read(scramble.MODULE_DIR + "/keypad.script"))

    def test_install_names_screens_with_text_digits(self):
        self.patch(connected_displays=lambda: [("DP-1", 1920, 1080, 0, 0)])
        output = self.run_command(scramble.cmd_install)
        self.assertIn(f"{scramble.PROG}: note: DP-1 1920x1080, Plymouth scale 1 shows text digits, not masked digits: it "
                      "reports no physical size; set ppi in the settings for masked digits", output)
        keypad = read(scramble.MODULE_DIR + "/keypad.script")
        self.assertIn("qubes_scramble.sets = 1;\nqubes_scramble.set[0].text = 1;", keypad)
        self.assertEqual(os.listdir(scramble.MODULE_DIR + "/tiles"), [scramble.TILE_MANIFEST])

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
            self.run_command(scramble.cmd_install, "[scramble]\ndisplay = text\n[digits]\n2 = ABC\n")
        self.assertEqual(self.state(), installed, "the previous installation is restored")

    def test_unknown_setting_stops_install_before_any_change(self):
        self.run_command(scramble.cmd_install)
        installed, builds = self.state(), len(self.builds)
        write(scramble.CONFIG_FILE, "[scramble]\nenabled = yes\nconsole = no\n")
        with self.assertRaisesRegex(scramble.ScrambleError, "unknown sections or settings: console"):
            self.run_command(scramble.cmd_install)
        installed[os.path.relpath(scramble.CONFIG_FILE, self.top)] = read_bytes(scramble.CONFIG_FILE)
        self.assertEqual((self.state(), len(self.builds)), (installed, builds))

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

    def test_reinstalls_with_changed_settings_then_uninstall_leave_the_stock_state(self):
        stock = self.state()
        self.run_command(scramble.cmd_install)
        self.hooked = [self.running]
        for settings in ("[scramble]\ndisplay = text\n", "[scramble]\ndisplay = white\nhighpass_cutoff = 1\n",
                         "[scramble]\nenabled = no\n", "[scramble]\ndisplay = hybrid\n"):
            write(scramble.CONFIG_FILE, settings)
            self.run_command(scramble.cmd_install)
        self.run_command(scramble.cmd_uninstall)
        self.assertEqual(self.state(), stock)
        self.assertEqual(self.builds[-1], ([self.running], False))
        self.assertEqual(os.listdir(self.top + "/modules.d"), [], "no module, staging or previous copies left")

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


@unittest.skipUnless(importlib.util.find_spec("numpy") and importlib.util.find_spec("PIL"),
                     "masked digits need numpy and Pillow (Qubes dom0 has both)")
class MaskedDigitsTest(unittest.TestCase):
    def setUp(self):
        self.np = scramble.imaging()[0]

    def masks(self):
        font = bold_font()
        if not font:
            self.skipTest("no bold sans-serif font (fc-match Sans:bold)")
        return scramble.digit_masks(font, GLYPH_PX, TILE_PX)

    def test_srgb_reference_points(self):
        np = self.np
        self.assertEqual(float(scramble.srgb_encode(0.0)), 0.0)
        self.assertAlmostEqual(float(scramble.srgb_encode(1.0)), 1.0, places=12)
        self.assertAlmostEqual(float(scramble.srgb_encode(0.0031308)), 0.04045, places=5)
        self.assertAlmostEqual(float(scramble.srgb_decode(0.5)), 0.214041, places=6)
        values = np.linspace(0, 1, 1001)
        self.assertTrue(np.allclose(scramble.srgb_decode(scramble.srgb_encode(values)), values, atol=1e-12))
        self.assertTrue(np.all(np.diff(scramble.srgb_encode(values)) > 0))

    def test_filter_gains(self):
        np = self.np
        frequency = scramble.spatial_frequencies(64)
        self.assertEqual(frequency.shape, (64, 33))
        self.assertEqual(float(frequency[0, 0]), 0.0)
        self.assertAlmostEqual(float(scramble.lowpass_gain(np.array(0.1), 0.1)), math.exp(-0.5), places=12)
        self.assertTrue(np.allclose(scramble.gaussian_highpass_gain(frequency, 0.2),
                                    1 - scramble.lowpass_gain(frequency, 0.2)))
        steps = scramble.tapered_highpass_gain(np.array([0.0, 0.1, 0.15, 0.2, 0.3, 0.5]), 0.1, 0.2)
        self.assertTrue(np.allclose(steps, [0, 0, 0.5, 1, 1, 1]))
        ramp = scramble.tapered_highpass_gain(np.linspace(0, 0.5, 501), 0.1, 0.2)
        self.assertTrue(np.all(np.diff(ramp) >= 0))
        # the paper's sigma_lf converted to its phone screen, and the cycles per pixel of a display
        self.assertAlmostEqual(scramble.PAPER_SIGMA_LF, 35 / (2560 / 493 * 25.4), places=3)
        self.assertAlmostEqual(scramble.cycles_per_pixel(1.0, 254), 0.1)

    def test_filtering_is_linear_and_complementary(self):
        np = self.np
        rng = np.random.default_rng(20261105)
        images = rng.random((3, 32, 48))
        frequency = np.hypot(np.fft.fftfreq(32)[:, None], np.fft.rfftfreq(48)[None, :])
        low = scramble.filtered(images, scramble.lowpass_gain(frequency, 0.07))
        high = scramble.filtered(images, scramble.gaussian_highpass_gain(frequency, 0.07))
        self.assertTrue(np.allclose(low + high, images, atol=1e-12))
        self.assertTrue(np.allclose(high.mean(axis=(1, 2)), 0, atol=1e-12))
        self.assertTrue(np.allclose(scramble.filtered(np.full((8, 8), 0.4), scramble.gaussian_highpass_gain(
            scramble.spatial_frequencies(8), 0.1)), 0, atol=1e-15))

    def test_tiles(self):
        np = self.np
        masks = self.masks()
        self.assertEqual(masks.shape, (10, TILE_PX, TILE_PX))
        self.assertTrue(masks.min() >= 0 and masks.max() <= 1 and all(mask.sum() > 50 for mask in masks))
        frequency = scramble.spatial_frequencies(TILE_PX)
        clean = scramble.tapered_highpass_gain(frequency, 0.17, 0.34)
        low = scramble.lowpass_gain(frequency, 0.04)
        background = float(scramble.srgb_decode(130 / 255))
        highpass = scramble.tile_light("highpass", masks, clean, low, scramble.TileDesign(0.35, 130))
        self.assertEqual(sorted(highpass), [(None, digit) for digit in range(10)])
        for image in highpass.values():
            self.assertAlmostEqual(float(image.mean()), background, places=12)
            spectrum = np.abs(np.fft.rfft2(image - background))
            self.assertLess(float(spectrum[frequency < 0.17].max()), 1e-9, "nothing below the lower edge")
        hybrid = scramble.tile_light("hybrid", masks, clean, low, scramble.TileDesign(0.35, 130, 0.3))
        self.assertEqual(len(hybrid), 100)
        decoys = 0.3 * scramble.filtered(masks, low)
        self.assertTrue(np.allclose(hybrid[(2, 7)] - hybrid[(5, 7)], decoys[2] - decoys[5], atol=1e-12))
        white = scramble.tile_light("white", masks, clean, low,
                                    scramble.TileDesign(0.35, 130, 0.2, scramble.button_mask(TILE_PX, BUTTON_PX)))
        self.assertTrue(np.allclose(white[(None, 3)] - highpass[(None, 3)], white[(None, 4)] - highpass[(None, 4)]))
        codes = scramble.tile_codes(highpass)
        self.assertEqual(codes[(None, 0)].dtype, np.uint8)
        plain = scramble.tile_codes({(None, 0): np.full((4, 4), background)})
        self.assertTrue(np.all(plain[(None, 0)] == 130), "the background sits exactly on its code")
        with self.assertRaisesRegex(scramble.ScrambleError, "outside the display range 0-1; lower alpha"):
            scramble.tile_codes(scramble.tile_light("highpass", masks, clean, low, scramble.TileDesign(3.0, 130)))
        with self.assertRaisesRegex(scramble.ScrambleError, "do not fit"):
            scramble.digit_masks(bold_font(), TILE_PX - 2, TILE_PX)

    def test_observer_geometry(self):
        width, height, distance = 100.0, 40.0, 500.0
        angle_x, angle_y = scramble.observer_angles(width, height, scramble.Observer(distance, math.pi / 2, 0))
        self.assertAlmostEqual(angle_x, math.degrees(2 * math.atan(width / 2 / distance)), places=10)
        self.assertAlmostEqual(angle_y, math.degrees(2 * math.atan(height / 2 / distance)), places=10)
        side_x, _ = scramble.observer_angles(width, height, scramble.Observer(distance))
        self.assertLess(side_x, angle_x, "the image looks narrower from 30 degrees to the side")
        self.assertEqual(scramble.angle_factor(math.pi / 2, 0), 1.0)
        self.assertAlmostEqual(scramble.angle_factor(math.pi / 2, math.pi / 6), 1 - (1 / 3) ** 3)
        self.assertAlmostEqual(scramble.angle_factor(math.pi / 3, 0), scramble.angle_factor(2 * math.pi / 3, 0))
        np = self.np
        self.assertTrue(np.allclose(scramble.daf_gain(np.array([0, 5, 10.3, 31, 40])), [1, 1, 1, 0, 0]))
        curve = scramble.daf_gain(np.linspace(0, 40, 401))
        self.assertTrue(np.all(np.diff(curve) <= 1e-12))

    def test_structural_similarity(self):
        np = self.np
        rng = np.random.default_rng(7)
        image = rng.random((40, 40))
        self.assertAlmostEqual(scramble.mssim(image, image), 1.0, places=12)
        self.assertAlmostEqual(scramble.mssim(image, image * 0.5 + 0.2), scramble.mssim(image * 0.5 + 0.2, image))
        self.assertLess(scramble.mssim(image, np.full_like(image, 0.5)), 0.2)

    def test_visibility_index_and_protection(self):
        masks = self.masks()
        frequency = scramble.spatial_frequencies(TILE_PX)
        low = scramble.lowpass_gain(frequency, scramble.cycles_per_pixel(scramble.PAPER_SIGMA_LF, PPI))

        def make(sigma):
            high = scramble.gaussian_highpass_gain(frequency, scramble.cycles_per_pixel(sigma, PPI))
            tiles = scramble.tile_light("hybrid", masks, high, low, scramble.TileDesign(0.45, 130, 0.3))
            light = {key: value for key, value in tiles.items()
                     if key[0] in (1, 2)}
            decoys = scramble.tile_light("hybrid", masks, high, low, scramble.TileDesign(0, 130, 0.3))
            return light, {key: decoys[key] for key in light}

        indices = [scramble.visibility_index(*make(1.63), PPI, scramble.Observer(distance)) for distance in (300, 600, 1200)]
        self.assertEqual(indices, sorted(indices), "the real digit fades with distance")
        _, decoys = make(1.63)
        self.assertAlmostEqual(scramble.visibility_index(decoys, decoys, PPI, scramble.Observer(300)), 1.0, places=9)
        width = scramble.protection_width(make, PPI, scramble.Observer(1140), (0.3, 4.4), steps=10)
        self.assertTrue(0.3 < width < 4.4)
        self.assertGreaterEqual(scramble.visibility_index(*make(width), PPI, scramble.Observer(1140)), scramble.PAPER_THRESHOLD)
        self.assertIsNone(scramble.protection_width(make, PPI, scramble.Observer(150), (0.3, 0.5), steps=4))
        distance = scramble.estimated_safety_distance(*make(1.63), PPI)  # whole cm: the index passes within 5 mm
        self.assertTrue(10 < distance < 1000)
        self.assertGreaterEqual(scramble.visibility_index(*make(1.63), PPI, scramble.Observer(10 * distance + 5)),
                                scramble.PAPER_THRESHOLD)
        self.assertEqual(scramble.estimated_safety_distance(decoys, decoys, PPI), 0, "hidden at 10 cm already")
        bright = {key: image * 0 + 0.9 for key, image in decoys.items()}
        self.assertIsNone(scramble.estimated_safety_distance(bright, decoys, PPI), "visible even at 10 m")

    def test_plymouth_scale2_truncation(self):
        np = self.np
        device = scramble.plymouth_scale2(np.array([[1, 1], [9, 200]], dtype=np.uint8))
        self.assertEqual(device.shape, (4, 4, 3))
        self.assertTrue(np.array_equal(device[0::2, 0::2, 1], [[1, 1], [9, 200]]), "even pixels are the image")
        # between two pixels of code 1: red and green keep 1, blue truncates each half to 0
        self.assertEqual(list(device[0, 1]), [1, 1, 0])
        # the last column blends its pixel with itself, in two truncated halves as well; the centre of
        # four pixels is their truncated mean
        self.assertEqual(list(device[0, 3]), [1, 1, 0])
        self.assertEqual(int(device[1, 1, 0]), (1 + 1 + 9 + 200) // 4)

    def test_error_diffusion(self):
        np = self.np
        masks = self.masks()
        frequency = scramble.spatial_frequencies(TILE_PX)
        clean = scramble.tapered_highpass_gain(frequency, 0.12, 0.24)
        light = scramble.tile_light("highpass", masks, clean, frequency * 0, scramble.TileDesign(0.35, 130))
        # below half the cut-off, where cameras too coarse for the high-pass band still see the
        # rounding error; error diffusion pushes it toward the cut-off
        keep = (frequency < 0.06).astype(float)

        def coarse_error(codes):
            return float(np.sqrt(np.mean([np.mean(scramble.filtered(scramble.srgb_decode(codes[key] / 255) - light[key],
                                                                    keep) ** 2) for key in light])))

        rounded, diffused = scramble.tile_codes(light), scramble.diffused_codes(light)
        self.assertLess(coarse_error(diffused), coarse_error(rounded) / 3)
        self.assertLessEqual(max(int(np.abs(diffused[key].astype(int) - rounded[key]).max()) for key in light), 1)
        self.assertTrue(all(np.array_equal(diffused[key], scramble.diffused_codes(light)[key]) for key in light))
        with self.assertRaisesRegex(scramble.ScrambleError, "outside the display range 0-1"):
            scramble.diffused_codes(scramble.tile_light("highpass", masks, clean, frequency * 0,
                                                        scramble.TileDesign(3.0, 130)))

    def test_scale2_compensation(self):
        np = self.np
        masks = self.masks()
        frequency = scramble.spatial_frequencies(TILE_PX)
        clean = scramble.tapered_highpass_gain(frequency, 0.12, 0.24)
        light = {key: value for key, value in scramble.tile_light(
            "highpass", masks, clean, frequency * 0, scramble.TileDesign(0.29, 160)).items() if key[1] in (0, 8)}
        keep = (frequency < 0.06).astype(float)

        def coarse_error(codes):
            shown = scramble.device_light(scramble.plymouth_scale2(codes))
            mean = shown.reshape(TILE_PX, 2, TILE_PX, 2).mean(axis=(1, 3))
            return float(np.sqrt(np.mean(scramble.filtered(mean - light[(None, 8)], keep) ** 2)))

        plain = scramble.tile_codes(light)
        compensated, remaining = scramble.scale2_tile_codes(light, keep)
        self.assertLess(coarse_error(compensated[(None, 8)]), coarse_error(plain[(None, 8)]) / 2)
        self.assertLess(remaining, 5e-4)

    def test_device_scale_rules(self):
        scale = scramble.plymouth_device_scale
        self.assertEqual(scale(1920, 1080, 344, 194), 1)
        self.assertEqual(scale(2560, 1600, 286, 179), 2)
        self.assertEqual(scale(2560, 1600, 0, 0), 1)
        self.assertEqual(scale(2560, 1440, 160, 90), 1)
        self.assertEqual(scale(3840, 2160, 600, 340), 1)
        self.assertEqual(scale(2560, 1199, 100, 50), 1)
        self.assertEqual(scale(2560, 1600, 0, 0, guess=True), 2)
        self.assertEqual(scale(1920, 1200, 0, 0, guess=True), 1)

    def test_edid(self):
        self.assertEqual(scramble.edid_mode(synthetic_edid(1920, 1080, 344, 194)), (1920, 1080, 344, 194))
        self.assertEqual(scramble.edid_mode(synthetic_edid(3840, 2400, 0, 0, width_cm=34, height_cm=21)), (3840, 2400, 340, 210))
        broken = bytearray(synthetic_edid(1920, 1080, 344, 194))
        broken[60] ^= 1
        with self.assertRaisesRegex(scramble.ScrambleError, "checksum"):
            scramble.edid_mode(bytes(broken))
        rng = random.Random(20261105)
        for _ in range(300):
            data = bytes(rng.randrange(256) for _ in range(rng.choice((0, 64, 128, 256))))
            if rng.random() < 0.5 and len(data) >= 128:
                data = b"\x00\xff\xff\xff\xff\xff\xff\x00" + data[8:127] + bytes([-sum(b"\x00\xff\xff\xff\xff\xff\xff\x00"
                                                                                    + data[8:127]) % 256]) + data[128:]
            try:
                mode = scramble.edid_mode(data)
            except scramble.ScrambleError:
                continue
            self.assertEqual(len(mode), 4)
            self.assertTrue(all(isinstance(value, int) and value >= 0 for value in mode))

    def test_connected_displays(self):
        root = tempfile.mkdtemp(prefix="scramble-sysfs-")
        self.addCleanup(shutil.rmtree, root)
        drm = root + scramble.DRM_CLASS
        for connector, status, edid in (("card0-eDP-1", "connected", synthetic_edid(1920, 1080, 344, 194)),
                                        ("card0-DP-1", "disconnected", b""), ("card1-HDMI-A-1", "connected", bytes(128)),
                                        ("card1-DP-2", "connected", b"")):
            write(f"{drm}/{connector}/status", status + "\n")
            write(f"{drm}/{connector}/edid", edid)
        write(f"{drm}/card0/dev", "226:0\n")
        self.assertEqual(scramble.connected_displays(root), [("eDP-1", 1920, 1080, 344, 194)])
        self.assertEqual(scramble.connected_displays(root + "/missing"), [])

    def test_tile_targets_follow_both_scale_rules(self):
        config = config_from("[scramble]\ndisplay = hybrid\n")
        # 1920x1200 at 200 ppi: the EDID rule gives scale 2, the simpledrm guess scale 1
        targets = scramble.tile_targets(config, [("eDP-1", 1920, 1200, 244, 152)])
        self.assertEqual([(target["scale"], target["width"], target["height"]) for target in targets],
                         [(1, 1920, 1200), (2, 960, 600)])
        self.assertAlmostEqual(targets[1]["ppi"], targets[0]["ppi"] / 2, delta=0.1)
        self.assertEqual(len(scramble.tile_targets(config, [("eDP-1", 2560, 1600, 286, 179)])), 1, "both rules give 2")
        fixed = config_from("[scramble]\ndisplay = hybrid\nppi = 150\n")
        self.assertEqual(scramble.tile_targets(fixed, [("DP-1", 1920, 1080, 0, 0)])[0]["ppi"], 150.0)
        self.assertEqual(scramble.tile_targets(fixed, []),
                         [{"display": "unknown", "scale": 1, "width": 0, "height": 0, "ppi": 150.0}])
        # without a physical size or ppi the targets have no density (text digits)
        self.assertEqual(scramble.tile_targets(config, []),
                         [{"display": "unknown", "scale": 1, "width": 0, "height": 0, "ppi": None}])
        self.assertEqual([target["ppi"] for target in scramble.tile_targets(config, [("DP-1", 1920, 1080, 0, 0)])], [None])

    def test_screens_that_cannot_show_masked_digits(self):
        unavailable = scramble.masked_digits_unavailable
        config = config_from("[scramble]\ndisplay = hybrid\n")
        # 27 inch 2560x1440 and 32 inch 3840x2160 screens at the simpledrm guess of scale 2; the first at scale 1
        target = {"display": "DP-1", "scale": 2, "width": 1280, "height": 720, "ppi": 54.44}
        self.assertEqual(unavailable(config, target), "size 1 makes 20 px tiles, under 24 px")
        self.assertEqual(unavailable(config, {**target, "width": 1920, "height": 1080, "ppi": 68.9}),
                         "camera_floor 1.2 cycles per mm is finer than the screen shows (at most 0.95)")
        self.assertIsNone(unavailable(config, {**target, "scale": 1, "width": 2560, "height": 1440, "ppi": 108.89}))
        self.assertEqual(unavailable(config, {**target, "ppi": None}),
                         "it reports no physical size; set ppi in the settings for masked digits")
        self.assertEqual(unavailable(config_from("[scramble]\nsize = 0.5\n"), {**target, "ppi": 92.62}),
                         "size 0.5 makes 16 px tiles, under 24 px")
        self.assertEqual(unavailable(config_from("[scramble]\nhighpass_cutoff = 3\n"), {**target, "ppi": 141.6}),
                         "highpass_cutoff 3 cycles per mm is finer than the screen shows (at most 1.95)")

    def test_tile_summary_names_the_display_as_reported(self):
        config = config_from("[scramble]\ndisplay = hybrid\nhighpass_cutoff = 0.6\n")
        # a 2560x1600 screen of 641x401 mm: scale 1 by its size, scale 2 by the simpledrm guess
        targets = scramble.tile_targets(config, [("DP-1", 2560, 1600, 641, 401)])
        self.assertEqual([(target["width"], target["ppi"]) for target in targets], [(2560, 101.39), (1280, 50.7)])
        sets = [{**target, "cutoff": 0.6, "estimated_safety_distance_cm": distance}
                for target, distance in zip(targets * 2, (92, 0, None, 10), strict=True)]
        summary = scramble.tile_summary({"display": "hybrid", "highpass_filter": "tapered", "quantization": "round",
                                         "sets": sets}).split("; ")
        self.assertEqual(summary[0], "hybrid digit tiles (tapered filter, cut-off 0.6 cycles per mm, quantization round) "
                         "for DP-1 2560x1600 at 101.4 ppi, Plymouth scale 1, estimated safety distance 92 cm")
        self.assertTrue(summary[1].endswith("DP-1 2560x1600 at 101.4 ppi, Plymouth scale 2, estimated safety "
                                            "distance under 10 cm (too close: you may not read these digits either)"),
                        summary[1])
        self.assertTrue(summary[2].endswith("distance over 1000 cm"))
        self.assertTrue(summary[3].endswith("distance 10 cm (too close: you may not read these digits either)"))
        unknown = scramble.tile_targets(config_from("[scramble]\ndisplay = hybrid\nppi = 150\n"), [])[0]
        self.assertEqual(scramble.target_text(unknown), "unknown at 150 ppi, Plymouth scale 1")

    def test_tiles_and_manifest(self):
        font = bold_font()
        if not font:
            self.skipTest("no bold sans-serif font (fc-match Sans:bold)")
        config = config_from("[scramble]\ndisplay = hybrid\n")
        files, manifest = scramble.make_tiles(config, [("HDMI-A-1", 1920, 1080, 527, 296)], font)
        tiles = manifest["sets"][0]
        self.assertEqual((len(files), tiles["scale"], tiles["cell"], tiles["cutoff"]),
                         (101, 1, 32, scramble.CAMERA_FLOOR))
        panel = imaging_image(files[tiles["panel"]["name"]])
        self.assertEqual((panel.size, panel.getextrema()), (scramble.keypad_row(32, True), ((130, 130),) * 3),
                         "one opaque panel in the background code, as wide as the digit row and its margins")
        self.assertGreater(tiles["estimated_safety_distance_cm"], 20)
        directory = tempfile.mkdtemp(prefix="scramble-tiles-")
        self.addCleanup(shutil.rmtree, directory)
        for name, data in files.items():
            write(f"{directory}/{name}", data)
        manifest_path = f"{directory}/{scramble.TILE_MANIFEST}"
        write(manifest_path, json.dumps(manifest))
        self.assertEqual(scramble.check_tiles(directory, manifest_path)["display"], "hybrid")
        name = "qubes-scramble-tile0-3-7.png"
        write(f"{directory}/{name}", files[name][:-1] + bytes([files[name][-1] ^ 1]))
        with self.assertRaisesRegex(scramble.ScrambleError, f"tile {name} does not match its manifest"):
            scramble.check_tiles(directory, manifest_path)
        os.unlink(f"{directory}/{name}")
        with self.assertRaisesRegex(scramble.ScrambleError, f"tile {name} is not readable"):
            scramble.check_tiles(directory, manifest_path)
        files, manifest = scramble.make_tiles(config_from("[scramble]\ndisplay = highpass\nhighpass_cutoff = 1\npanel = no\n"),
                                              [("HDMI-A-1", 1920, 1080, 527, 296)], font)
        self.assertEqual((len(files), manifest["sets"][0]["cutoff"], manifest["quantization"], "panel" in manifest["sets"][0]),
                         (10, 1.0, "diffusion", False))
        quantized = {method: scramble.make_tiles(config_from(f"[scramble]\ndisplay = highpass\nhighpass_cutoff = 1\n"
                                                             f"quantization = {method}\n"),
                                                 [("HDMI-A-1", 1920, 1080, 527, 296)], font)[0]
                     for method in ("round", "compensate")}
        self.assertEqual(quantized["round"], quantized["compensate"], "compensation applies at scale 2 only")
        self.assertNotEqual(quantized["round"], files)
        # a screen too coarse for the camera floor gets a text set, without tiles
        manifest = scramble.make_tiles(config, [("DP-1", 1920, 1080, 600, 340)], font)[1]
        write(manifest_path, json.dumps(manifest))
        self.assertEqual(scramble.check_tiles(directory, manifest_path)["sets"][0]["text"],
                         "camera_floor 1.2 cycles per mm is finer than the screen shows (at most 1.12)")
        self.assertIn("qubes_scramble.set[0].text = 1; qubes_scramble.set[0].width = 1920; "
                      "qubes_scramble.set[0].height = 1080;", scramble.render_overlay(config, read(TEMPLATE), manifest))
        self.assertEqual(scramble.tile_summary(manifest), "text digits for DP-1 1920x1080 at 81 ppi, Plymouth scale 1, "
                         "which cannot show masked digits: camera_floor 1.2 cycles per mm is finer than the screen "
                         "shows (at most 1.12)")

    def test_size_scales_the_digits_but_not_the_model(self):
        font = bold_font()
        if not font:
            self.skipTest("no bold sans-serif font (fc-match Sans:bold)")
        display = [("HDMI-A-1", 1920, 1080, 527, 296)]
        base = scramble.make_tiles(config_from("[scramble]\ndisplay = hybrid\n"), display, font)[1]["sets"][0]
        config = config_from("[scramble]\ndisplay = hybrid\nsize = 1.5\n")
        manifest = scramble.make_tiles(config, display, font)[1]
        tiles = manifest["sets"][0]
        # the tile grows with the digit; cut-off and estimate stay those of the default size
        self.assertEqual((tiles["cell"], tiles["cutoff"], tiles["estimated_safety_distance_cm"]),
                         (scramble.tile_pixels(1.5, tiles["ppi"]), base["cutoff"], base["estimated_safety_distance_cm"]))
        self.assertEqual((tiles["cell"], tiles["panel"]["width"], tiles["panel"]["height"]), (50, 566, 62))
        overlay = scramble.render_overlay(config, read(TEMPLATE), manifest)
        for line in ('qubes_scramble.set[0].panel = "qubes-scramble-tile0-panel.png";',
                     'qubes_scramble.digit_font = "Sans Bold 30";'):
            self.assertIn(line, overlay)
        self.assertIn("quantization diffusion, size 1.5)", scramble.tile_summary(manifest))
        with self.assertRaisesRegex(scramble.ScrambleError, r"need a digit row of 1266x132 px, the screen leaves 1240x168 px "
                                                            r"under the key caps; use size 2\.9 or less"):
            scramble.make_tiles(config_from("[scramble]\nsize = 3\n"), [("eDP-1", 1280, 800, 286, 179)], font)
        self.assertEqual(scramble.make_tiles(config_from("[scramble]\nsize = 0.5\n"), display, font)[1]["sets"][0]["text"],
                         "size 0.5 makes 16 px tiles, under 24 px")


class FuzzTest(unittest.TestCase):
    """Seeded random inputs: parsers fail only with ScrambleError, and the boot hook edits
    vconsole.conf exactly (or not at all without Plymouth) and restores it byte for byte."""

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
            for letters, colour in zip(config.letters, config.colors, strict=True):
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

    def test_hook_edits_vconsole_exactly(self):
        pool = [b'KEYMAP="us"', b"KEYMAP=us", b"KEYMAP='us'", b'FONT="eurlatgr"', b"# comment", b"", b"  ",
                b"XKBLAYOUT=de", b"XKBLAYOUT = de", b"  XKBVARIANT=nodeadkeys", b"\tXKBOPTIONS=ctrl:nocaps", b"XKBMODEL=pc104",
                b"KEYMAP_TOGGLE=foo", b"FONT_MAP=8859-1", b"UNKNOWN=1", b"XKBLAYOUTX=keep", b"#XKBLAYOUT=keep",
                b"KEYMAP=us\r", b"x" * 300]
        managed = re.compile(rb"^[ \t]*XKB(LAYOUT|MODEL|VARIANT|OPTIONS)[ \t]*=")
        root = tempfile.mkdtemp(prefix="scramble-fuzz-")
        self.addCleanup(shutil.rmtree, root)
        write_image_conf(root)
        for _ in range(150):
            original = b"\n".join(self.random.choices(pool, k=self.random.randint(0, 7))) + self.random.choice([b"", b"\n"])
            disabled, failing = self.random.random() < 0.3, self.random.random() < 0.2
            write(root + "/etc/vconsole.conf", original)
            write(root + scramble.THEME_FILE, STOCK_THEME + ("[script-env-vars]\nx=1\n" if failing else ""))
            write(root + "/proc/cmdline", "plymouth.enable=0\n" if disabled else "quiet\n")
            result = run_hook(root)
            if disabled or failing:
                self.assertEqual(result.returncode == 0, disabled, result.stderr)
                self.assertEqual(read_bytes(root + "/etc/vconsole.conf"), original, "vconsole.conf stays as it was")
                self.assertEqual(scramble.etc_run_files(root).keys(), BASE_FILES)
                continue
            self.assertEqual(result.returncode, 0, result.stderr)
            kept = [line for line in original.split(b"\n")[:-1 if original.endswith(b"\n") else None]
                    if not managed.match(line)] if original else []
            expected = b"".join(line + b"\n" for line in kept + [line.encode() for line in XKB_LINES])
            self.assertEqual(read_bytes(root + "/etc/vconsole.conf"), expected, repr(original))
            self.assertEqual(run_hook(root, "restore").returncode, 0)
            self.assertEqual(read_bytes(root + "/etc/vconsole.conf"), original, "restore is byte-exact")
            self.assertEqual(scramble.etc_run_files(root).keys(), BASE_FILES)


if __name__ == "__main__":
    unittest.main()

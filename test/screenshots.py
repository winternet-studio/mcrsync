# -*- coding: utf-8 -*-
"""Regenerates the README screenshots (screenshots/*.svg): drives the real
UI through tmux in the Docker rig with demo data and renders each captured
screen to SVG. See test/README.md."""

import html
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "mcrsync.py")
OUT = sys.argv[1] if len(sys.argv) > 1 else "/screenshots"
COLS, ROWS = 120, 30
KEYFILE = "/root/.ssh/id_test"
PORT = 2222
NAS_HOST = "backup-nas"
SETTINGS = os.path.expanduser("~/.mcrsync.yaml")
RCLONE_CONF = "/tmp/screens-rclone.conf"
LOCAL = "/volume1"
USB = "/volumeUSB1"


def sh(cmd):
    return subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT).decode()


# ----------------------------------------------------------------------
# ANSI capture -> SVG
# ----------------------------------------------------------------------

class Palette(object):
    BG = "#1d1f21"
    FG = "#d0d0d0"
    BASE = ["#2e3436", "#cc4b4b", "#7fb24c", "#d7a83a", "#4f7fd0", "#b072c0", "#45a5a8",
            "#d3d7cf", "#6f7476", "#ef6b6b", "#9ed36a", "#f0c95c", "#7aa2ee", "#cf96dd",
            "#6fd0d2", "#f4f4f2"]

    @classmethod
    def xterm(cls, n):
        if n < 16:
            return cls.BASE[n]
        if n < 232:
            n -= 16
            steps = [0, 95, 135, 175, 215, 255]
            return "#{0:02x}{1:02x}{2:02x}".format(steps[n // 36], steps[n // 6 % 6], steps[n % 6])
        level = 8 + (n - 232) * 10
        return "#{0:02x}{0:02x}{0:02x}".format(level)


class Cell(object):
    __slots__ = ("ch", "fg", "bg", "bold", "dim", "underline", "reverse")

    def __init__(self, ch=" ", style=None):
        self.ch = ch
        s = style or {}
        self.fg = s.get("fg")
        self.bg = s.get("bg")
        self.bold = s.get("bold", False)
        self.dim = s.get("dim", False)
        self.underline = s.get("underline", False)
        self.reverse = s.get("reverse", False)

    def colors(self):
        fg = self.fg or Palette.FG
        bg = self.bg or Palette.BG
        if self.reverse:
            fg, bg = bg, fg
        return fg, (None if bg == Palette.BG else bg)

    def text_key(self):
        return self.colors()[0], self.bold, self.dim, self.underline


class AnsiScreen(object):
    """Parses `tmux capture-pane -e -p` output into rows of cells."""

    SGR = re.compile(r"\x1b\[([0-9;:]*)m")

    def __init__(self, text, cols, rows):
        self.rows = []
        for line in text.split("\n")[:rows]:
            style = {}
            row = []
            pos = 0
            for m in self.SGR.finditer(line):
                row += [Cell(ch, style) for ch in line[pos:m.start()]]
                style = self.apply(style, m.group(1))
                pos = m.end()
            row += [Cell(ch, style) for ch in line[pos:]]
            self.rows.append((row + [Cell() for _ in range(cols)])[:cols])
        while len(self.rows) < rows:
            self.rows.append([Cell() for _ in range(cols)])

    @staticmethod
    def apply(style, params):
        style = dict(style)
        codes = [int(p) if p else 0 for p in re.split("[;:]", params)] if params else [0]
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == 0:
                # tmux 2.3 resets with 0 when an attribute goes away but
                # doesn't repeat colors that stay the same
                style = dict((k, v) for k, v in style.items() if k in ("fg", "bg"))
            elif c in (1, 2, 4, 7):
                style[{1: "bold", 2: "dim", 4: "underline", 7: "reverse"}[c]] = True
            elif c == 22:
                style.pop("bold", None)
                style.pop("dim", None)
            elif c in (24, 27):
                style.pop({24: "underline", 27: "reverse"}[c], None)
            elif 30 <= c <= 37 or 90 <= c <= 97:
                style["fg"] = Palette.BASE[c - 30 if c < 90 else c - 82]
            elif 40 <= c <= 47 or 100 <= c <= 107:
                style["bg"] = Palette.BASE[c - 40 if c < 100 else c - 92]
            elif c in (39, 49):
                style.pop("fg" if c == 39 else "bg", None)
            elif c in (38, 48) and i + 1 < len(codes):
                key = "fg" if c == 38 else "bg"
                if codes[i + 1] == 5 and i + 2 < len(codes):
                    style[key] = Palette.xterm(codes[i + 2])
                    i += 2
                elif codes[i + 1] == 2 and i + 4 < len(codes):
                    style[key] = "#{0:02x}{1:02x}{2:02x}".format(*codes[i + 2:i + 5])
                    i += 4
            i += 1
        return style


class SvgRenderer(object):
    CW, CH, FONT = 8.4, 18, 14
    PAD, BAR = 14, 30
    # box-drawing character -> (up, down, left, right)
    LINES = {u"─": "0011", u"│": "1100", u"┌": "0101", u"┐": "0110", u"└": "1001",
             u"┘": "1010", u"├": "1101", u"┤": "1110", u"┬": "0111", u"┴": "1011",
             u"┼": "1111"}

    def __init__(self, screen, title):
        self.screen = screen
        self.title = title

    def render(self):
        cols = len(self.screen.rows[0])
        width = cols * self.CW + 2 * self.PAD
        height = len(self.screen.rows) * self.CH + 2 * self.PAD + self.BAR
        out = ['<svg xmlns="http://www.w3.org/2000/svg" width="{0:.0f}" height="{1:.0f}" '
               'viewBox="0 0 {0:.0f} {1:.0f}">'.format(width, height),
               '<rect width="100%" height="100%" rx="8" fill="{0}"/>'.format(Palette.BG),
               '<rect width="100%" height="{0}" rx="8" fill="#303235"/>'.format(self.BAR),
               '<rect y="{0}" width="100%" height="8" fill="{1}"/>'.format(self.BAR - 8, "#303235")]
        for i, color in enumerate(("#ff5f57", "#febc2e", "#28c840")):
            out.append('<circle cx="{0}" cy="{1}" r="6" fill="{2}"/>'.format(
                20 + i * 20, self.BAR // 2, color))
        out.append('<text x="{0}" y="{1}" fill="#a0a0a0" font-family="sans-serif" '
                   'font-size="13" text-anchor="middle">{2}</text>'.format(
                       width / 2, self.BAR // 2 + 4, html.escape(self.title)))
        out.append('<g transform="translate({0},{1})" font-family="ui-monospace, '
                   'SFMono-Regular, Menlo, Consolas, \'DejaVu Sans Mono\', monospace" '
                   'font-size="{2}">'.format(self.PAD, self.PAD + self.BAR, self.FONT))
        for y, row in enumerate(self.screen.rows):
            out += self.backgrounds(y, row)
        for y, row in enumerate(self.screen.rows):
            out += self.texts(y, row)
        out.append("</g></svg>")
        return "\n".join(out) + "\n"

    def backgrounds(self, y, row):
        out = []
        x = 0
        while x < len(row):
            bg = row[x].colors()[1]
            start = x
            while x < len(row) and row[x].colors()[1] == bg:
                x += 1
            if bg:
                out.append('<rect x="{0:.1f}" y="{1}" width="{2:.1f}" height="{3}" fill="{4}"/>'
                           .format(start * self.CW, y * self.CH, (x - start) * self.CW,
                                   self.CH, bg))
        return out

    def texts(self, y, row):
        out = []
        x = 0
        while x < len(row):
            cell = row[x]
            if cell.ch in self.LINES:
                out.append(self.box_line(x, y, cell))
                x += 1
                continue
            key = cell.text_key()
            start = x
            while (x < len(row) and row[x].ch not in self.LINES
                   and row[x].text_key() == key):
                x += 1
            text = "".join(c.ch for c in row[start:x])
            if not text.strip() and not cell.underline:
                continue
            fg, bold, dim, underline = key
            attrs = ' fill="{0}"'.format(fg)
            if bold:
                attrs += ' font-weight="bold"'
            if dim:
                attrs += ' fill-opacity="0.55"'
            if underline:
                attrs += ' text-decoration="underline"'
            out.append('<text x="{0:.1f}" y="{1:.1f}" textLength="{2:.1f}" '
                       'lengthAdjust="spacingAndGlyphs" xml:space="preserve"{3}>{4}</text>'
                       .format(start * self.CW, y * self.CH + self.CH * 0.75,
                               (x - start) * self.CW, attrs, html.escape(text)))
        return out

    def box_line(self, x, y, cell):
        up, down, left, right = (d == "1" for d in self.LINES[cell.ch])
        cx = x * self.CW + self.CW / 2
        cy = y * self.CH + self.CH / 2
        d = []
        if up or down:
            d.append("M{0:.1f} {1:.1f}V{2:.1f}".format(
                cx, cy if not up else y * self.CH, cy if not down else (y + 1) * self.CH))
        if left or right:
            d.append("M{0:.1f} {1:.1f}H{2:.1f}".format(
                cx if not left else x * self.CW, cy, cx if not right else (x + 1) * self.CW))
        fg, bold, dim, _ = cell.text_key()
        return '<path d="{0}" stroke="{1}" stroke-width="{2}"{3}/>'.format(
            "".join(d), fg, 1.6 if bold else 1.2, ' stroke-opacity="0.55"' if dim else "")


# ----------------------------------------------------------------------
# Driving the UI
# ----------------------------------------------------------------------

class Tmux(object):
    SESSION = "mcrsync-screens"
    CONF = "/tmp/screens.tmux.conf"

    def start(self, cwd, env=""):
        with open(self.CONF, "w") as f:
            f.write("set -g status off\n")
        self.stop()
        subprocess.check_call(["tmux", "-f", self.CONF, "new-session", "-d", "-s", self.SESSION,
                               "-x", str(COLS), "-y", str(ROWS), "-c", cwd,
                               "{0} python3 {1}; sleep 600".format(env, APP)])
        self.wait_for("SOURCE [")

    def stop(self):
        subprocess.call(["tmux", "kill-server"], stderr=subprocess.DEVNULL)
        time.sleep(0.5)

    def keys(self, *keys, **kw):
        cmd = ["tmux", "send-keys", "-t", self.SESSION]
        if kw.get("literal"):
            cmd += ["-l", "--"]
        subprocess.check_call(cmd + list(keys))
        time.sleep(kw.get("delay", 0.6))

    def type(self, text):
        self.keys(text, literal=True)

    def screen(self, ansi=False):
        cmd = ["tmux", "capture-pane", "-p", "-t", self.SESSION]
        if ansi:
            cmd.insert(2, "-e")
        return subprocess.check_output(cmd).decode("utf-8", "replace")

    def wait_for(self, text, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            if text in self.screen():
                return
            time.sleep(0.3)
        raise RuntimeError("timed out waiting for {0!r}; screen:\n{1}".format(
            text, self.screen()))

    def select(self, label, pane=0):
        """Moves the cursor of the active pane (0 left, 1 right) to label."""
        self.keys("Home", delay=0.3)
        for _ in range(40):
            screen = AnsiScreen(self.screen(ansi=True), COLS, ROWS)
            half = COLS // 2
            for row in screen.rows:
                text = "".join(c.ch if c.reverse else "\0" for c in row[pane * half:(pane + 1) * half])
                if any(part.strip() == label for part in text.split("\0")):
                    return
            self.keys("Down", delay=0.3)
        raise RuntimeError("{0!r} not found; screen:\n{1}".format(label, self.screen()))

    def save(self, name, title):
        svg = SvgRenderer(AnsiScreen(self.screen(ansi=True), COLS, ROWS), title).render()
        path = os.path.join(OUT, name + ".svg")
        with open(path, "w", encoding="utf-8") as f:
            f.write(svg)
        print("wrote " + path)


class DemoData(object):
    """A NAS-like local folder, an SSH backup host and an S3 bucket."""

    def __init__(self):
        self.nas = "keyuser@" + NAS_HOST

    def build(self):
        shutil.rmtree(LOCAL, ignore_errors=True)
        small = {
            "photos/2023/Iceland": ["IMG_{0:04d}.jpg".format(i) for i in range(1, 25)],
            "photos/2024/Lisbon": ["IMG_{0:04d}.jpg".format(i) for i in range(1, 40)],
            "photos/2024/Family": ["DSC{0:05d}.jpg".format(i) for i in range(1, 30)],
            "documents/taxes": ["2022.pdf", "2023.pdf", "2024.pdf"],
            "documents/manuals": ["router.pdf", "washing-machine.pdf"],
            "music/Jazz": ["track{0:02d}.flac".format(i) for i in range(1, 12)],
            "projects/website": ["index.html", "style.css", "app.js"],
        }
        for folder, files in small.items():
            os.makedirs(os.path.join(LOCAL, folder))
            for name in files:
                with open(os.path.join(LOCAL, folder, name), "wb") as f:
                    f.write(os.urandom(3000))
        os.makedirs(os.path.join(LOCAL, "video", "Home movies"))
        for i in range(1, 5):
            path = os.path.join(LOCAL, "video", "Home movies", "clip{0:02d}.mp4".format(i))
            with open(path, "wb") as f:
                f.write(os.urandom(6000000))
        with open(os.path.join(LOCAL, "notes.txt"), "w") as f:
            f.write("x")
        shutil.rmtree(USB, ignore_errors=True)
        for folder in ("backup/photos/2022", "backup/music"):
            os.makedirs(os.path.join(USB, folder))
        self.ssh_host()
        self.rclone()

    def ssh_host(self):
        ip = sh("getent hosts remote-debian").split()[0]
        with open("/etc/hosts", "a") as f:
            f.write("{0} {1}\n".format(ip, NAS_HOST))
        with open(os.path.expanduser("~/.ssh/known_hosts"), "w") as f:
            f.write(sh("ssh-keyscan -p {0} {1} 2>/dev/null".format(PORT, NAS_HOST)))
        self.remote("rm -rf backup; mkdir -p backup/photos/2022 backup/documents backup/music "
                    "&& touch backup/documents/old-taxes.pdf")

    def remote(self, cmd):
        return sh("ssh -i {0} -p {1} {2} '{3}'".format(KEYFILE, PORT, self.nas, cmd))

    def rclone(self):
        endpoint = os.environ.get("MCRSYNC_TEST_RCLONE", "").split()[0].split("=", 1)[1]
        with open(RCLONE_CONF, "w") as f:
            f.write("[cloud]\ntype = s3\nprovider = Other\naccess_key_id = test\n"
                    "secret_access_key = testsecret\nendpoint = {0}\n".format(endpoint))
        env = "RCLONE_CONFIG=" + RCLONE_CONF
        sh("{0} rclone purge cloud:archive 2>/dev/null; true".format(env))
        sh("{0} rclone copy {1}/photos/2023 cloud:archive/photos/2023".format(env, LOCAL))
        sh("{0} rclone copy {1}/documents cloud:archive/documents".format(env, LOCAL))

    @staticmethod
    def settings(remote=None, rclone=None):
        lines = []
        if remote:
            lines += ["last_remote:", "  user: keyuser", "  host: " + NAS_HOST,
                      "  port: {0}".format(PORT), '  base_path: "~/backup"',
                      '  keyfile: "{0}"'.format(KEYFILE)]
        if rclone:
            lines += ["last_rclone:", '  remote: "cloud"', '  path: "archive"']
        with open(SETTINGS, "w") as f:
            f.write("\n".join(lines) + "\n")


class Scenes(object):
    def __init__(self):
        self.tmux = Tmux()
        self.data = DemoData()

    def run(self):
        self.data.build()
        try:
            self.nav()
            self.ssh()
            self.rclone()
            self.progress_and_log()
        finally:
            self.tmux.stop()

    def nav(self):
        t = self.tmux
        self.data.settings()
        t.start(LOCAL)
        t.keys("Tab")
        t.keys("Left")
        t.select("/" + os.path.basename(USB), pane=1)
        t.keys("Right")
        t.select("/backup", pane=1)
        t.keys("Right")
        t.wait_for(USB + "/backup")
        t.keys("Tab")
        t.select("/documents")
        t.keys("c")
        t.select("/photos")
        t.keys("c")
        time.sleep(2)           # background sizing
        t.save("nav", "mcrsync - source and target panes")

    def connect_ssh_target(self):
        t = self.tmux
        t.keys("Tab")
        t.keys("o")
        t.wait_for("Connect this pane to:")
        t.keys("Enter")
        t.wait_for("Connect TARGET pane via SSH")
        for _ in range(5):
            t.keys("Enter", delay=0.3)
        t.wait_for("TARGET [ssh keyuser@")

    def ssh(self):
        t = self.tmux
        self.data.settings(remote=True)
        t.start(LOCAL)
        self.connect_ssh_target()
        t.keys("Tab")
        t.select("/documents")
        t.keys("c")
        t.select("/photos")
        t.keys("c")
        time.sleep(2)           # background sizing
        t.save("ssh", "mcrsync - SSH remote pane")

    def rclone(self):
        t = self.tmux
        self.data.settings(rclone=True)
        t.start(os.path.join(LOCAL, "photos"), "RCLONE_CONFIG=" + RCLONE_CONF)
        t.keys("Tab")
        t.keys("o")
        t.wait_for("Connect this pane to:")
        t.keys("Down")
        t.save("connect", "mcrsync - connect a pane (O)")
        t.keys("Enter")
        t.wait_for("Remote from your rclone config")
        t.keys("Enter")
        t.wait_for("Start path")
        t.keys("Enter")
        t.wait_for("TARGET [rclone cloud]")
        t.select("/photos", pane=1)
        t.keys("Right")
        t.wait_for("/2023")
        t.keys("Tab")
        t.select("/2024")
        t.keys("c")
        time.sleep(2)
        t.save("rclone", "mcrsync - rclone (S3) pane")
        t.keys("F8")
        t.wait_for("RUN OPTIONS")
        for _ in range(12):        # down to the last row, --delete
            t.keys("Down", delay=0.2)
        t.keys("Space")
        t.wait_for("rclone sync")
        t.save("run-options-rclone", "mcrsync - rclone run options (F8)")
        t.keys("Escape")

    def progress_and_log(self):
        t = self.tmux
        self.data.settings(remote=True)
        t.start(LOCAL)
        self.connect_ssh_target()
        t.keys("Tab")
        for label in ("/video", "/music", "/documents"):
            t.select(label)
            t.keys("c")
        t.keys("F8")
        t.wait_for("RUN OPTIONS")
        t.keys("e")
        t.keys("C-u")
        t.type("-a -u --stats --itemize-changes --bwlimit=1500")
        t.keys("Enter")
        t.save("run-options-rsync", "mcrsync - rsync run options (F8)")
        t.keys("Enter")
        t.wait_for("| copying |")
        t.keys("t")
        time.sleep(6)
        t.save("progress", "mcrsync - live progress")
        t.keys("g")
        t.wait_for("RUN LOG")
        time.sleep(3)
        t.save("run-log", "mcrsync - run log")


if __name__ == "__main__":
    if not os.environ.get("MCRSYNC_TEST_HOSTS"):
        sys.exit("Run this inside the Docker rig, see test/README.md")
    Scenes().run()

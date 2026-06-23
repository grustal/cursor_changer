"""
Cursor Changer
==============
A small Windows desktop app for previewing and applying custom mouse cursors.

Two modes:
- Single Cursor: drop one image, preview it, optionally auto-split it into
  resting + clicking poses, or generate one with AI, then apply it.
- Cursor Pack: drop a .zip cursor pack (like the ones from rw-designer.com)
  or individual .cur / .ani / image files one at a time (e.g. pointer_arrow.cur,
  then pointer_wait.cur, then pointer_text.cur...). Each file is matched to
  the right Windows cursor role automatically, by reading a bundled .crs
  scheme file if present, or by guessing from the filename otherwise.

Run on Windows with:
    pip install -r requirements.txt
    python cursor_changer.py

Package as a standalone .exe with PyInstaller (see build_exe.bat).
"""

import os
import io
import json
import base64
import struct
import ctypes
import shutil
import zipfile
import tempfile
import threading
import configparser
import urllib.request
import urllib.error
import tkinter as tk
from tkinter import filedialog, messagebox

from PIL import Image, ImageTk

try:
    import winreg
except ImportError:  # not on Windows (e.g. during development on another OS)
    winreg = None

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False

try:
    RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:  # older Pillow
    RESAMPLE = Image.LANCZOS


APP_NAME = "Cursor Changer"
CURSOR_SIZE = 32  # canvas size (px) used to build generated .cur files
STORE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "CursorChanger"
)
CUR_FILENAME = "custom_cursor.cur"
CONFIG_PATH = os.path.join(STORE_DIR, "config.json")
OPENAI_IMAGE_URL = "https://api.openai.com/v1/images/generations"

# Registry value name -> friendly label shown in the UI
CURSOR_TYPES = [
    ("Arrow", "Normal Select"),
    ("IBeam", "Text Select"),
    ("Wait", "Busy"),
    ("Crosshair", "Precision Select"),
    ("Hand", "Link Select"),
    ("SizeAll", "Move"),
    ("SizeWE", "Horizontal Resize"),
    ("SizeNS", "Vertical Resize"),
    ("SizeNWSE", "Diagonal Resize \u2198"),
    ("SizeNESW", "Diagonal Resize \u2199"),
    ("NWPen", "Handwriting"),
    ("AppStarting", "Working in Background"),
    ("No", "Unavailable"),
    ("Help", "Help Select"),
]
CURSOR_ROLE_SET = {n for n, _ in CURSOR_TYPES}

CURSOR_FILE_EXTS = {".cur", ".ani"}
IMAGE_EXTS = {".png", ".ico", ".bmp", ".jpg", ".jpeg", ".gif"}

SPI_SETCURSORS = 0x0057
SPIF_UPDATEINIFILE = 0x01
SPIF_SENDCHANGE = 0x02

# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

BG = "#0e1015"
SURFACE = "#161922"
SURFACE_2 = "#1f2330"
SURFACE_3 = "#272c3b"
BORDER = "#2c3140"
ACCENT = "#7c5cff"
ACCENT_HOVER = "#9277ff"
TEXT_PRIMARY = "#f3f4f8"
TEXT_SECONDARY = "#8c93a6"
TEXT_FAINT = "#5b6173"
ERROR_COLOR = "#ff6b6b"

FONT_FAMILY = "Segoe UI"


def is_windows():
    return os.name == "nt"


# ---------------------------------------------------------------------------
# Local config (API key storage)
# ---------------------------------------------------------------------------

def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    os.makedirs(STORE_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f)


# ---------------------------------------------------------------------------
# AI cursor generation (OpenAI Images API, using your own API key)
# ---------------------------------------------------------------------------

def generate_ai_image(prompt: str, api_key: str) -> Image.Image:
    payload = json.dumps({
        "model": "gpt-image-1",
        "prompt": prompt,
        "size": "1024x1024",
        "background": "transparent",
        "n": 1,
    }).encode("utf-8")
    req = urllib.request.Request(
        OPENAI_IMAGE_URL,
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"API error {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error: {e.reason}") from e

    b64 = data["data"][0]["b64_json"]
    img_bytes = base64.b64decode(b64)
    return Image.open(io.BytesIO(img_bytes)).convert("RGBA")


# ---------------------------------------------------------------------------
# Image -> .cur conversion
# ---------------------------------------------------------------------------

def fit_to_canvas(img: Image.Image, size: int, scale_x: float = 1.0, scale_y: float = 1.0) -> Image.Image:
    """Fit an image into a size x size transparent canvas, centered.

    scale_x / scale_y let the fitted image be stretched or squashed
    independently along each axis (capped so it never exceeds the canvas).
    """
    img = img.convert("RGBA")
    base_ratio = min(size / img.width, size / img.height)
    new_w = max(1, min(size, int(img.width * base_ratio * scale_x)))
    new_h = max(1, min(size, int(img.height * base_ratio * scale_y)))
    resized = img.resize((new_w, new_h), RESAMPLE)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    offset = ((size - new_w) // 2, (size - new_h) // 2)
    canvas.paste(resized, offset, resized)
    return canvas


def build_cur_bytes(img: Image.Image, hotspot=(16, 16), size=CURSOR_SIZE,
                     scale_x: float = 1.0, scale_y: float = 1.0) -> bytes:
    canvas = fit_to_canvas(img, size, scale_x, scale_y)

    png_buf = io.BytesIO()
    canvas.save(png_buf, format="PNG")
    png_data = png_buf.getvalue()

    hx, hy = hotspot
    hx = max(0, min(size - 1, int(hx)))
    hy = max(0, min(size - 1, int(hy)))

    icondir = struct.pack("<HHH", 0, 2, 1)
    width_byte = 0 if size >= 256 else size
    height_byte = 0 if size >= 256 else size
    image_offset = 6 + 16
    entry = struct.pack(
        "<BBBBHHII",
        width_byte, height_byte, 0, 0,
        hx, hy,
        len(png_data), image_offset,
    )
    return icondir + entry + png_data


def save_cur_file(img: Image.Image, hotspot, dest_path: str,
                   scale_x: float = 1.0, scale_y: float = 1.0) -> str:
    data = build_cur_bytes(img, hotspot, scale_x=scale_x, scale_y=scale_y)
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "wb") as f:
        f.write(data)
    return dest_path


# ---------------------------------------------------------------------------
# Auto-detection of "2 cursors in 1 image" (resting + clicking poses)
# ---------------------------------------------------------------------------

ALPHA_THRESHOLD = 10
BG_COLOR_THRESHOLD = 26
MIN_SEGMENT_FRACTION = 0.12
DETECT_MAX_DIM = 600


def _is_background_pixel(rgba, bg_color):
    r, g, b, a = rgba
    if a <= ALPHA_THRESHOLD:
        return True
    if bg_color is None:
        return False
    br, bgc, bb = bg_color
    dist2 = (r - br) ** 2 + (g - bgc) ** 2 + (b - bb) ** 2
    return dist2 <= BG_COLOR_THRESHOLD ** 2


def _sample_background_color(img):
    w, h = img.size
    px = img.load()
    corners = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
    rs, gs, bs = [], [], []
    for cx, cy in corners:
        r, g, b, a = px[cx, cy]
        if a > ALPHA_THRESHOLD:
            rs.append(r)
            gs.append(g)
            bs.append(b)
    if not rs:
        return None
    return (sum(rs) // len(rs), sum(gs) // len(gs), sum(bs) // len(bs))


def _axis_segments(img, bg_color, axis):
    w, h = img.size
    px = img.load()
    length = w if axis == "x" else h
    other = h if axis == "x" else w

    is_gap = []
    for i in range(length):
        gap = True
        for j in range(other):
            xy = (i, j) if axis == "x" else (j, i)
            if not _is_background_pixel(px[xy], bg_color):
                gap = False
                break
        is_gap.append(gap)

    segments = []
    start = None
    for i, gap in enumerate(is_gap + [True]):
        if not gap and start is None:
            start = i
        elif gap and start is not None:
            segments.append((start, i))
            start = None

    min_size = max(4, int(length * MIN_SEGMENT_FRACTION))
    return [s for s in segments if (s[1] - s[0]) >= min_size]


def _trim(img, bg_color):
    w, h = img.size
    px = img.load()
    min_x, min_y, max_x, max_y = w, h, -1, -1
    for y in range(h):
        for x in range(w):
            if not _is_background_pixel(px[x, y], bg_color):
                if x < min_x:
                    min_x = x
                if x > max_x:
                    max_x = x
                if y < min_y:
                    min_y = y
                if y > max_y:
                    max_y = y
    if max_x < min_x or max_y < min_y:
        return img
    pad = 1
    box = (max(0, min_x - pad), max(0, min_y - pad), min(w, max_x + 1 + pad), min(h, max_y + 1 + pad))
    return img.crop(box)


def detect_cursor_segments(img: Image.Image):
    rgba = img.convert("RGBA")
    w, h = rgba.size
    max_dim = max(w, h)
    if max_dim > DETECT_MAX_DIM:
        scale = DETECT_MAX_DIM / max_dim
        work = rgba.resize((max(1, int(w * scale)), max(1, int(h * scale))), RESAMPLE)
    else:
        work = rgba

    bg_color = _sample_background_color(work)
    h_segments = _axis_segments(work, bg_color, axis="x")
    if len(h_segments) == 2:
        ranges, axis = h_segments, "x"
    else:
        v_segments = _axis_segments(work, bg_color, axis="y")
        if len(v_segments) == 2:
            ranges, axis = v_segments, "y"
        else:
            return [_trim(work, bg_color)]

    ww, wh = work.size
    pieces = []
    for start, end in ranges:
        box = (start, 0, end, wh) if axis == "x" else (0, start, ww, end)
        pieces.append(_trim(work.crop(box), bg_color))
    return pieces


# ---------------------------------------------------------------------------
# Cursor pack support (zip files, .crs scheme files, filename guessing)
# ---------------------------------------------------------------------------

def extract_zip(path):
    tmp_dir = tempfile.mkdtemp(prefix="cursorpack_")
    with zipfile.ZipFile(path) as zf:
        zf.extractall(tmp_dir)
    return tmp_dir


def find_scheme_file(root_dir):
    for dirpath, _, filenames in os.walk(root_dir):
        for fn in filenames:
            if fn.lower().endswith(".crs"):
                return os.path.join(dirpath, fn)
    return None


def parse_crs_scheme(scheme_path):
    """Parse a RealWorld Cursor Editor .crs scheme file -> {reg_name: abs_path}."""
    cp = configparser.ConfigParser()
    with open(scheme_path, "r", encoding="utf-8-sig", errors="ignore") as f:
        cp.read_string(f.read())
    mapping = {}
    for section in cp.sections():
        role = section.strip()
        if role not in CURSOR_ROLE_SET:
            continue
        if cp.has_option(section, "Path"):
            rel = cp.get(section, "Path").strip().strip('"')
            abs_path = os.path.normpath(os.path.join(os.path.dirname(scheme_path), rel))
            if os.path.isfile(abs_path):
                mapping[role] = abs_path
    return mapping


def _core_name(filename):
    stem = os.path.splitext(os.path.basename(filename))[0].lower()
    for prefix in ("pointer_", "cursor_", "mouse_"):
        if stem.startswith(prefix):
            stem = stem[len(prefix):]
            break
    return stem


def guess_role_for_filename(filename):
    """Best-effort guess of which Windows cursor role a filename refers to."""
    core = _core_name(filename)
    tokens = set(core.split("_"))

    if "top_left_diagonal" in core or "nwse" in tokens:
        return "SizeNWSE"
    if "top_right_diagonal" in core or "nesw" in tokens:
        return "SizeNESW"
    if "horizontal_double_arrow" in core or "sizewe" in tokens or "ew_resize" in core:
        return "SizeWE"
    if "vertical_double_arrow" in core or "sizens" in tokens or "ns_resize" in core:
        return "SizeNS"
    if tokens & {"grab", "grabbing", "move", "pan", "sizeall"} or "all_scroll" in core:
        return "SizeAll"
    if tokens & {"hand", "link"}:
        return "Hand"
    if tokens & {"ibeam", "beam"} or tokens == {"text"}:
        return "IBeam"
    if tokens & {"handwriting", "pen", "nwpen"}:
        return "NWPen"
    if tokens & {"wait", "loading", "busy", "hourglass", "spinner"}:
        return "Wait"
    if tokens & {"appstarting", "background", "working"}:
        return "AppStarting"
    if tokens & {"crosshair", "cross", "precision"}:
        return "Crosshair"
    if tokens & {"no", "nodrop", "forbidden", "unavailable", "notallowed", "stop"}:
        return "No"
    if tokens & {"help"}:
        return "Help"
    if tokens & {"arrow", "normal", "default", "select"} or core in ("pointer", ""):
        return "Arrow"
    return None


def build_pack_from_zip(zip_path):
    """Extract a zip and build a {reg_name: file_path} mapping.

    Prefers a bundled .crs scheme file (used by rw-designer.com cursor sets)
    for accurate role assignment, then fills any remaining roles by
    guessing from filenames.
    """
    tmp_dir = extract_zip(zip_path)
    mapping = {}

    scheme_path = find_scheme_file(tmp_dir)
    if scheme_path:
        try:
            mapping = parse_crs_scheme(scheme_path)
        except Exception:
            mapping = {}

    all_files = []
    for dirpath, _, filenames in os.walk(tmp_dir):
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext in CURSOR_FILE_EXTS or ext in IMAGE_EXTS:
                all_files.append(os.path.join(dirpath, fn))

    used_paths = set(mapping.values())
    for f in all_files:
        if f in used_paths:
            continue
        role = guess_role_for_filename(f)
        if role and role not in mapping:
            mapping[role] = f
            used_paths.add(f)

    return tmp_dir, mapping


def make_pack_item(path):
    """Build a preview/apply record for a single cursor-pack file."""
    ext = os.path.splitext(path)[1].lower()
    if ext in CURSOR_FILE_EXTS:
        try:
            preview = Image.open(path).convert("RGBA")
        except Exception:
            preview = None
        return {"path": path, "kind": "file", "pil": preview, "hotspot": None,
                "source_name": os.path.basename(path)}
    else:
        try:
            img = Image.open(path)
            img.load()
        except Exception:
            return None
        return {"path": path, "kind": "image", "pil": img.convert("RGBA"),
                "hotspot": (CURSOR_SIZE // 2, CURSOR_SIZE // 2),
                "source_name": os.path.basename(path)}


# ---------------------------------------------------------------------------
# Windows cursor application
# ---------------------------------------------------------------------------

def apply_cursor_mapping(role_to_path: dict):
    """Set one or more cursor registry values at once and refresh."""
    if not is_windows() or winreg is None:
        raise RuntimeError("Cursor changes can only be applied on Windows.")
    key_path = r"Control Panel\Cursors"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        for reg_name, path in role_to_path.items():
            winreg.SetValueEx(key, reg_name, 0, winreg.REG_SZ, path)
    ctypes.windll.user32.SystemParametersInfoW(
        SPI_SETCURSORS, 0, None, SPIF_UPDATEINIFILE | SPIF_SENDCHANGE
    )


def apply_cursors(cur_path: str, selected_types):
    apply_cursor_mapping({reg_name: cur_path for reg_name, _ in selected_types})


def reset_cursors_to_default(selected_types=None):
    if not is_windows() or winreg is None:
        raise RuntimeError("Cursor changes can only be applied on Windows.")
    key_path = r"Control Panel\Cursors"
    names = [n for n, _ in (selected_types or CURSOR_TYPES)]
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        for reg_name in names:
            try:
                winreg.SetValueEx(key, reg_name, 0, winreg.REG_SZ, "")
            except OSError:
                pass
    ctypes.windll.user32.SystemParametersInfoW(
        SPI_SETCURSORS, 0, None, SPIF_UPDATEINIFILE | SPIF_SENDCHANGE
    )


# ---------------------------------------------------------------------------
# Small custom widgets
# ---------------------------------------------------------------------------

def round_rect(canvas, x1, y1, x2, y2, r=10, **kwargs):
    points = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


class ModernButton(tk.Canvas):
    STYLES = {
        "accent": dict(normal=ACCENT, hover=ACCENT_HOVER, text="#ffffff", disabled=SURFACE_3),
        "ghost": dict(normal=SURFACE_2, hover=SURFACE_3, text=TEXT_PRIMARY, disabled=SURFACE),
    }

    def __init__(self, parent, text, command=None, width=128, height=38, style="accent"):
        super().__init__(parent, width=width, height=height, bg=parent["bg"],
                          highlightthickness=0, bd=0, cursor="hand2")
        self.command = command
        self.w, self.h = width, height
        self.text = text
        self.colors = self.STYLES[style]
        self.enabled = True
        self._render(self.colors["normal"])
        self.bind("<Enter>", lambda e: self._render(self.colors["hover"]) if self.enabled else None)
        self.bind("<Leave>", lambda e: self._render(self.colors["normal"]) if self.enabled else None)
        self.bind("<Button-1>", self._click)

    def _render(self, fill):
        self.delete("all")
        if not self.enabled:
            fill = self.colors["disabled"]
        round_rect(self, 1, 1, self.w - 1, self.h - 1, r=9, fill=fill, outline="")
        fg = self.colors["text"] if self.enabled else TEXT_FAINT
        self.create_text(self.w / 2, self.h / 2, text=self.text, fill=fg,
                          font=(FONT_FAMILY, 10, "bold"))

    def _click(self, _e):
        if self.enabled and self.command:
            self.command()

    def set_enabled(self, enabled: bool):
        self.enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self._render(self.colors["normal"])

    def set_text(self, text):
        self.text = text
        self._render(self.colors["normal"])

    def set_style(self, style):
        self.colors = self.STYLES[style]
        self._render(self.colors["normal"])


class ModernCheckbox(tk.Canvas):
    BOX = 16

    def __init__(self, parent, text, variable, width=170, height=24):
        super().__init__(parent, width=width, height=height, bg=parent["bg"],
                          highlightthickness=0, bd=0, cursor="hand2")
        self.variable = variable
        self.text = text
        self.w, self.h = width, height
        self.enabled = True
        self.render()
        self.bind("<Button-1>", self._toggle)

    def render(self):
        self.delete("all")
        checked = self.variable.get()
        y0 = (self.h - self.BOX) / 2
        box_fill = ACCENT if checked else SURFACE_2
        box_outline = ACCENT if checked else BORDER
        round_rect(self, 0, y0, self.BOX, y0 + self.BOX, r=4, fill=box_fill, outline=box_outline)
        if checked:
            self.create_line(3, y0 + 8, 6.5, y0 + 12, 13, y0 + 4,
                              fill="#ffffff", width=2, capstyle="round", joinstyle="round")
        fg = TEXT_PRIMARY if self.enabled else TEXT_FAINT
        self.create_text(self.BOX + 9, self.h / 2, text=self.text, anchor="w",
                          fill=fg, font=(FONT_FAMILY, 9))

    def _toggle(self, _e):
        if not self.enabled:
            return
        self.variable.set(not self.variable.get())
        self.render()

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self.render()


class ScrollableFrame(tk.Frame):
    """A vertically scrollable container (Tkinter has no built-in one)."""

    def __init__(self, parent, bg, height=320):
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, height=height)
        self.scrollbar = tk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas_window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(self.canvas_window, width=e.width))
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._on_mousewheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


class MenuBar(tk.Frame):
    """A flat, dark dropdown menu bar that matches the app theme."""

    def __init__(self, parent, menus):
        super().__init__(parent, bg=SURFACE)
        self._popup = None
        for label, items in menus:
            lbl = tk.Label(self, text=label, bg=SURFACE, fg=TEXT_SECONDARY,
                            font=(FONT_FAMILY, 9), padx=14, pady=8, cursor="hand2")
            lbl.pack(side="left")
            lbl.bind("<Enter>", lambda e, w=lbl: w.config(fg=TEXT_PRIMARY, bg=SURFACE_2))
            lbl.bind("<Leave>", lambda e, w=lbl: w.config(fg=TEXT_SECONDARY, bg=SURFACE))
            lbl.bind("<Button-1>", lambda e, w=lbl, it=items: self._toggle(w, it))
        tk.Frame(parent, bg=BORDER, height=1).pack(fill="x")

    def _toggle(self, anchor, items):
        if self._popup is not None:
            self._close()
            return
        x = anchor.winfo_rootx()
        y = anchor.winfo_rooty() + anchor.winfo_height()
        popup = tk.Toplevel(self)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.configure(bg=BORDER)
        inner = tk.Frame(popup, bg=SURFACE_2)
        inner.pack(padx=1, pady=1)
        for item_label, cmd in items:
            if item_label is None:
                tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", padx=8, pady=4)
                continue
            row = tk.Label(inner, text=item_label, bg=SURFACE_2, fg=TEXT_PRIMARY,
                            font=(FONT_FAMILY, 9), anchor="w", padx=16, pady=7, cursor="hand2")
            row.pack(fill="x")

            def on_click(_e, c=cmd):
                self._close()
                if c:
                    c()

            row.bind("<Button-1>", on_click)
            row.bind("<Enter>", lambda e, r=row: r.config(bg=ACCENT, fg="#ffffff"))
            row.bind("<Leave>", lambda e, r=row: r.config(bg=SURFACE_2, fg=TEXT_PRIMARY))
        popup.geometry(f"+{x}+{y}")
        popup.bind("<FocusOut>", lambda e: self._close())
        popup.focus_set()
        self._popup = popup

    def _close(self):
        if self._popup is not None:
            self._popup.destroy()
            self._popup = None


def render_pack_thumb(canvas, item, size=40):
    canvas.config(width=size, height=size)
    canvas.delete("all")
    if item is None:
        round_rect(canvas, 1, 1, size - 1, size - 1, r=8, fill=SURFACE_2, outline=BORDER, width=1, dash=(3, 2))
        canvas.create_text(size / 2, size / 2, text="+", fill=TEXT_FAINT, font=(FONT_FAMILY, 14))
        return
    pil = item.get("pil")
    if pil is None:
        round_rect(canvas, 1, 1, size - 1, size - 1, r=8, fill=SURFACE_2, outline=BORDER, width=1)
        canvas.create_text(size / 2, size / 2, text="\u25b6", fill=TEXT_SECONDARY, font=(FONT_FAMILY, 12))
        return
    img = pil.convert("RGBA")
    ratio = min((size - 6) / img.width, (size - 6) / img.height)
    new_w, new_h = max(1, int(img.width * ratio)), max(1, int(img.height * ratio))
    resized = img.resize((new_w, new_h), RESAMPLE)
    canvas_img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    off = ((size - new_w) // 2, (size - new_h) // 2)
    canvas_img.paste(resized, off, resized)
    tkimg = ImageTk.PhotoImage(canvas_img)
    canvas.image = tkimg  # keep a reference
    round_rect(canvas, 1, 1, size - 1, size - 1, r=8, fill=SURFACE_2, outline=BORDER, width=1)
    canvas.create_image(0, 0, anchor="nw", image=tkimg)


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class CursorChangerApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("680x920")
        self.root.minsize(680, 700)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.app_mode = "single"  # "single" or "pack"

        # single-cursor state
        self.image_path = None
        self.original_image = None
        self.detected_pieces = []
        self.pieces = []
        self.hotspots = []
        self.mode = "single"  # "single" or "split" pose mode, within single-cursor tab
        self.preview_canvases = []
        self.ai_panel_visible = False
        self.scale_x = 1.0
        self.scale_y = 1.0

        # pack state
        self.pack_items = {}          # reg_name -> item dict
        self.pack_include_vars = {}   # reg_name -> BooleanVar
        self.pack_row_widgets = {}    # reg_name -> widget refs
        self._pack_temp_dirs = []

        self.type_vars = {reg_name: tk.BooleanVar(value=(reg_name == "Arrow")) for reg_name, _ in CURSOR_TYPES}

        self._build_menu()
        self._build_ui()

    # -- menu -----------------------------------------------------------
    def _build_menu(self):
        menus = [
            ("File", [
                ("Open Image...", self.browse_file),
                ("Open Cursor Pack / Files...", self.pack_browse_files),
                (None, None),
                ("Exit", self.root.quit),
            ]),
            ("Cursor", [
                ("Reset to Windows Default", self.on_reset),
                ("Open Cursor Folder", self.open_store_dir),
            ]),
            ("AI", [
                ("Generate with AI", self.toggle_ai_panel),
                ("Set OpenAI API Key...", self.open_api_key_dialog),
            ]),
            ("Help", [
                ("About", self.show_about),
            ]),
        ]
        MenuBar(self.root, menus).pack(fill="x")

    # -- main layout ------------------------------------------------------
    def _build_ui(self):
        main = tk.Frame(self.root, bg=BG, padx=24, pady=20)
        main.pack(fill="both", expand=True)

        header = tk.Frame(main, bg=BG)
        header.pack(fill="x")
        tk.Label(header, text=APP_NAME, bg=BG, fg=TEXT_PRIMARY,
                  font=(FONT_FAMILY, 18, "bold")).pack(anchor="w")
        tk.Label(header, text="Drop an image, preview it, then apply it as your cursor.",
                  bg=BG, fg=TEXT_SECONDARY, font=(FONT_FAMILY, 10)).pack(anchor="w", pady=(2, 0))
        tk.Frame(main, bg=BORDER, height=1).pack(fill="x", pady=(16, 14))

        # Mode tabs
        tabs = tk.Frame(main, bg=BG)
        tabs.pack(fill="x", pady=(0, 14))
        self.tab_single_btn = ModernButton(tabs, "Single Cursor", command=lambda: self.set_app_mode("single"),
                                            style="accent", width=160, height=36)
        self.tab_single_btn.pack(side="left")
        self.tab_pack_btn = ModernButton(tabs, "Cursor Pack", command=lambda: self.set_app_mode("pack"),
                                          style="ghost", width=160, height=36)
        self.tab_pack_btn.pack(side="left", padx=(8, 0))

        # ---- Single-cursor section -------------------------------------
        self.single_section = tk.Frame(main, bg=BG)
        self.single_section.pack(fill="x")

        card = tk.Frame(self.single_section, bg=SURFACE)
        card.pack(fill="x")
        card_inner = tk.Frame(card, bg=SURFACE, padx=18, pady=18)
        card_inner.pack(fill="x")

        self.preview_holder = tk.Frame(card_inner, bg=SURFACE)
        self.preview_holder.pack()

        hint_text = (
            "Drag & drop an image here, or use File \u2192 Open Image.\n"
            "Two-pose images (resting + clicking) are split automatically."
            if DND_AVAILABLE
            else "Drag & drop isn't available here \u2014 use File \u2192 Open Image instead."
        )
        tk.Label(card_inner, text=hint_text, bg=SURFACE, fg=TEXT_SECONDARY,
                  font=(FONT_FAMILY, 9), wraplength=480, justify="left").pack(anchor="w", pady=(14, 2))
        tk.Label(card_inner, text="The click point (hotspot) defaults to the center of the cursor.",
                  bg=SURFACE, fg=TEXT_FAINT, font=(FONT_FAMILY, 9)).pack(anchor="w")

        if DND_AVAILABLE:
            self.preview_holder.drop_target_register(DND_FILES)
            self.preview_holder.dnd_bind("<<Drop>>", self.on_drop)

        self._rebuild_preview()

        self.ai_toggle_btn = ModernButton(card_inner, "\u2728 Generate with AI", command=self.toggle_ai_panel,
                                           style="ghost", width=200, height=34)
        self.ai_toggle_btn.pack(anchor="w", pady=(14, 0))

        self.ai_panel = tk.Frame(card_inner, bg=SURFACE_2, padx=14, pady=14)
        self._build_ai_panel(self.ai_panel)

        # Cursor type selection card (single mode only)
        self.apply_to_wrap = tk.Frame(main, bg=BG)
        self.apply_to_wrap.pack(fill="x")
        tk.Label(self.apply_to_wrap, text="APPLY TO", bg=BG, fg=TEXT_FAINT,
                  font=(FONT_FAMILY, 8, "bold")).pack(anchor="w", pady=(20, 8))
        self.types_card = tk.Frame(self.apply_to_wrap, bg=SURFACE, padx=18, pady=14)
        self.types_card.pack(fill="x")
        self.apply_holder = tk.Frame(self.types_card, bg=SURFACE)
        self.apply_holder.pack(fill="x")
        self._rebuild_apply_section()

        # ---- Cursor-pack section ----------------------------------------
        self.pack_section = tk.Frame(main, bg=BG)
        pack_card = tk.Frame(self.pack_section, bg=SURFACE, padx=18, pady=18)
        pack_card.pack(fill="both", expand=True)
        self._build_pack_section(pack_card)
        # not packed yet -- shown when the Cursor Pack tab is selected

        # Buttons
        btn_row = tk.Frame(main, bg=BG)
        btn_row.pack(fill="x", pady=(22, 0))
        self.use_btn = ModernButton(btn_row, "Use", command=self.on_use, style="accent", width=130)
        self.use_btn.pack(side="right")
        self.cancel_btn = ModernButton(btn_row, "Cancel", command=self.on_cancel, style="ghost", width=110)
        self.cancel_btn.pack(side="right", padx=(0, 10))
        self.reset_btn = ModernButton(btn_row, "\u21bb  Return to Normal Cursor",
                                       command=self.on_reset, style="ghost", width=220)
        self.reset_btn.pack(side="left")
        self.use_btn.set_enabled(False)

        # Status row
        status_row = tk.Frame(main, bg=BG)
        status_row.pack(fill="x", pady=(18, 0))
        self.status_dot = tk.Canvas(status_row, width=10, height=10, bg=BG, highlightthickness=0)
        self.status_dot.create_oval(1, 1, 9, 9, fill=TEXT_FAINT, outline="")
        self.status_dot.pack(side="left", padx=(0, 8))
        self.status_var = tk.StringVar(value="No file loaded.")
        tk.Label(status_row, textvariable=self.status_var, bg=BG, fg=TEXT_SECONDARY,
                  font=(FONT_FAMILY, 9)).pack(side="left")

        if not is_windows():
            tk.Label(main, text="Note: not running on Windows, so Use will preview only.",
                      bg=BG, fg=TEXT_FAINT, font=(FONT_FAMILY, 9)).pack(anchor="w", pady=(6, 0))

    def _set_status_dot(self, color):
        self.status_dot.delete("all")
        self.status_dot.create_oval(1, 1, 9, 9, fill=color, outline="")

    # -- mode tabs ------------------------------------------------------
    def set_app_mode(self, mode):
        self.app_mode = mode
        if mode == "single":
            self.tab_single_btn.set_style("accent")
            self.tab_pack_btn.set_style("ghost")
            self.pack_section.pack_forget()
            self.single_section.pack(fill="x")
            self.apply_to_wrap.pack(fill="x")
            self.use_btn.set_enabled(self.original_image is not None)
        else:
            self.tab_pack_btn.set_style("accent")
            self.tab_single_btn.set_style("ghost")
            self.single_section.pack_forget()
            self.apply_to_wrap.pack_forget()
            self.pack_section.pack(fill="both", expand=True)
            self.use_btn.set_enabled(len(self.pack_items) > 0)

    # -- AI panel -------------------------------------------------------
    def _build_ai_panel(self, parent):
        tk.Label(parent, text="Describe the cursor you want", bg=SURFACE_2, fg=TEXT_PRIMARY,
                  font=(FONT_FAMILY, 9, "bold")).pack(anchor="w")
        tk.Label(parent, text="Uses your own OpenAI API key, stored locally on this PC.",
                  bg=SURFACE_2, fg=TEXT_FAINT, font=(FONT_FAMILY, 8)).pack(anchor="w", pady=(2, 8))

        prompt_row = tk.Frame(parent, bg=SURFACE_2)
        prompt_row.pack(fill="x", pady=(0, 10))
        self.ai_prompt_var = tk.StringVar()
        entry = tk.Entry(prompt_row, textvariable=self.ai_prompt_var, bg=SURFACE_3, fg=TEXT_PRIMARY,
                          insertbackground=TEXT_PRIMARY, relief="flat",
                          highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
                          font=(FONT_FAMILY, 10))
        entry.pack(side="left", fill="x", expand=True, ipady=6)
        ModernButton(prompt_row, "\u2699", command=self.open_api_key_dialog,
                     style="ghost", width=38, height=32).pack(side="left", padx=(8, 0))

        btns_row = tk.Frame(parent, bg=SURFACE_2)
        btns_row.pack(fill="x")
        self.ai_generate_btn = ModernButton(btns_row, "Generate Cursor",
                                             command=lambda: self.on_generate_click("resting"),
                                             style="accent", width=160, height=34)
        self.ai_generate_btn.pack(side="left")
        self.ai_generate_click_btn = ModernButton(btns_row, "+ Add Clicking Pose",
                                                   command=lambda: self.on_generate_click("clicking"),
                                                   style="ghost", width=180, height=34)
        self.ai_generate_click_btn.pack(side="left", padx=(8, 0))
        self.ai_generate_click_btn.set_enabled(False)

        self.ai_status_var = tk.StringVar(value="")
        self.ai_status_label = tk.Label(parent, textvariable=self.ai_status_var, bg=SURFACE_2,
                                         fg=TEXT_SECONDARY, font=(FONT_FAMILY, 9))
        self.ai_status_label.pack(anchor="w", pady=(10, 0))

    def toggle_ai_panel(self):
        self.set_app_mode("single")
        self.ai_panel_visible = not self.ai_panel_visible
        if self.ai_panel_visible:
            self.ai_panel.pack(fill="x", pady=(14, 0))
            self.ai_toggle_btn.set_text("\u2715  Close AI Generator")
        else:
            self.ai_panel.pack_forget()
            self.ai_toggle_btn.set_text("\u2728 Generate with AI")

    def _set_ai_status(self, text, color):
        self.ai_status_var.set(text)
        self.ai_status_label.config(fg=color)

    def open_api_key_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("OpenAI API Key")
        dlg.configure(bg=SURFACE)
        dlg.geometry("440x220")
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()

        pad = tk.Frame(dlg, bg=SURFACE, padx=20, pady=20)
        pad.pack(fill="both", expand=True)
        tk.Label(pad, text="OpenAI API Key", bg=SURFACE, fg=TEXT_PRIMARY,
                  font=(FONT_FAMILY, 12, "bold")).pack(anchor="w")
        tk.Label(pad, text="Stored locally on this PC only, used to call OpenAI's image "
                            "generation API. Get a key at platform.openai.com.",
                  bg=SURFACE, fg=TEXT_SECONDARY, font=(FONT_FAMILY, 9),
                  wraplength=390, justify="left").pack(anchor="w", pady=(4, 12))

        key_var = tk.StringVar(value=load_config().get("openai_api_key", ""))
        entry = tk.Entry(pad, textvariable=key_var, show="\u2022", bg=SURFACE_2, fg=TEXT_PRIMARY,
                          insertbackground=TEXT_PRIMARY, relief="flat",
                          highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
                          font=(FONT_FAMILY, 10))
        entry.pack(fill="x", ipady=6)
        entry.focus_set()

        btn_row = tk.Frame(pad, bg=SURFACE)
        btn_row.pack(fill="x", pady=(18, 0))

        def do_save():
            cfg = load_config()
            cfg["openai_api_key"] = key_var.get().strip()
            save_config(cfg)
            dlg.destroy()

        ModernButton(btn_row, "Save", command=do_save, style="accent", width=100, height=34).pack(side="right")
        ModernButton(btn_row, "Cancel", command=dlg.destroy, style="ghost", width=100, height=34).pack(
            side="right", padx=(0, 8))

    def on_generate_click(self, role):
        cfg = load_config()
        api_key = cfg.get("openai_api_key", "").strip()
        if not api_key:
            messagebox.showinfo(APP_NAME, "Add your OpenAI API key first.")
            self.open_api_key_dialog()
            return
        prompt = self.ai_prompt_var.get().strip()
        if not prompt:
            messagebox.showwarning(APP_NAME, "Describe what you want the cursor to look like.")
            return

        if role == "clicking":
            full_prompt = (
                f"A single small simple Windows mouse cursor icon, the 'clicking' or "
                f"pressed-down variant of: {prompt}. Centered, isolated on a fully "
                "transparent background, clean flat vector icon style matching a normal "
                "pointer cursor but visually showing a pressed/active state. No shadow, "
                "no extra elements, no text, no background."
            )
        else:
            full_prompt = (
                f"A single small simple Windows mouse cursor icon design: {prompt}. "
                "Centered, isolated on a fully transparent background, clean flat vector "
                "icon style, no shadow, no extra elements, no text, no background."
            )

        self._set_ai_status("Generating\u2026 this can take up to a minute.", TEXT_SECONDARY)
        self.ai_generate_btn.set_enabled(False)
        self.ai_generate_click_btn.set_enabled(False)

        def worker():
            try:
                img = generate_ai_image(full_prompt, api_key)
                self.root.after(0, lambda: self._on_ai_image_ready(img, role))
            except Exception as e:
                err = str(e)
                self.root.after(0, lambda: self._on_ai_image_error(err))

        threading.Thread(target=worker, daemon=True).start()

    def _on_ai_image_ready(self, img, role):
        self.ai_generate_btn.set_enabled(True)
        self._set_ai_status("Done.", ACCENT)
        img = img.convert("RGBA")

        if role == "resting":
            self.image_path = "AI-generated"
            self.original_image = img
            self.detected_pieces = [img]
            self.scale_x = 1.0
            self.scale_y = 1.0
            self._set_mode("single")
            self.use_btn.set_enabled(True)
            self.ai_generate_click_btn.set_enabled(True)
            self._set_status_dot(TEXT_SECONDARY)
            self.status_var.set("Loaded: AI-generated cursor")
        else:
            if not self.pieces:
                self.ai_generate_click_btn.set_enabled(True)
                return
            resting_piece = self.pieces[0]
            old_hotspot = self.hotspots[0] if self.hotspots else (CURSOR_SIZE // 2, CURSOR_SIZE // 2)
            self.original_image = resting_piece
            self.detected_pieces = [resting_piece, img]
            self.pieces = [resting_piece, img]
            self.hotspots = [old_hotspot, (CURSOR_SIZE // 2, CURSOR_SIZE // 2)]
            self.mode = "split"
            self._rebuild_preview()
            self._rebuild_apply_section()
            self.ai_generate_click_btn.set_enabled(True)
            self.status_var.set("Loaded: AI-generated resting + clicking poses")

    def _on_ai_image_error(self, message):
        self.ai_generate_btn.set_enabled(True)
        self.ai_generate_click_btn.set_enabled(self.original_image is not None)
        self._set_ai_status("Generation failed.", ERROR_COLOR)
        messagebox.showerror(APP_NAME, f"Couldn't generate the image:\n{message}")

    # -- single-cursor mode management ---------------------------------
    def _set_mode(self, mode):
        self.mode = mode
        if mode == "split" and len(self.detected_pieces) == 2:
            self.pieces = list(self.detected_pieces)
        else:
            self.pieces = [self.original_image] if self.original_image is not None else []
        self.hotspots = [(CURSOR_SIZE // 2, CURSOR_SIZE // 2) for _ in self.pieces]
        self._rebuild_preview()
        self._rebuild_apply_section()

    def on_toggle_mode(self):
        self._set_mode("single" if self.mode == "split" else "split")

    def on_swap_pieces(self):
        if len(self.pieces) == 2:
            self.pieces = [self.pieces[1], self.pieces[0]]
            self.hotspots = [self.hotspots[1], self.hotspots[0]]
            self._rebuild_preview()

    # -- preview area -------------------------------------------------------
    def _rebuild_preview(self):
        for w in self.preview_holder.winfo_children():
            w.destroy()

        if self.original_image is None:
            tk.Label(self.preview_holder, text="No image loaded yet", bg=SURFACE, fg=TEXT_FAINT,
                      font=(FONT_FAMILY, 10)).pack(pady=(20, 20))
            return

        info = tk.Frame(self.preview_holder, bg=SURFACE_2, padx=16, pady=14)
        info.pack(fill="x")

        name = os.path.basename(self.image_path) if isinstance(self.image_path, str) else "AI-generated image"
        tk.Label(info, text=name, bg=SURFACE_2, fg=TEXT_PRIMARY, font=(FONT_FAMILY, 10, "bold")).pack(anchor="w")

        if self.mode == "split" and len(self.pieces) == 2:
            tk.Label(info, text="Detected 2 cursor poses: Resting (Normal Select) + Clicking (Link Select)",
                      bg=SURFACE_2, fg=TEXT_SECONDARY, font=(FONT_FAMILY, 9), wraplength=460, justify="left"
                      ).pack(anchor="w", pady=(4, 8))
            ModernButton(info, "\u21c4 Swap resting / clicking", command=self.on_swap_pieces,
                         style="ghost", width=220, height=30).pack(anchor="w")
        else:
            w, h = self.original_image.size
            tk.Label(info, text=f"{w}\u00d7{h} pixels \u2014 centered automatically on a 32\u00d732 cursor",
                      bg=SURFACE_2, fg=TEXT_SECONDARY, font=(FONT_FAMILY, 9)).pack(anchor="w", pady=(4, 0))

        if len(self.detected_pieces) == 2:
            toggle_text = ("Use as one single cursor instead" if self.mode == "split"
                            else "Split back into resting + clicking poses")
            ModernButton(info, toggle_text, command=self.on_toggle_mode,
                         style="ghost", width=300, height=30).pack(anchor="w", pady=(8, 0))

        self._build_size_controls(self.preview_holder)

    def _build_size_controls(self, parent):
        row = tk.Frame(parent, bg=SURFACE)
        row.pack(pady=(14, 0))

        def make_axis_control(label, axis):
            col = tk.Frame(row, bg=SURFACE)
            col.pack(side="left", padx=14)
            tk.Label(col, text=label, bg=SURFACE, fg=TEXT_SECONDARY,
                      font=(FONT_FAMILY, 8, "bold")).pack()
            ctrl_row = tk.Frame(col, bg=SURFACE)
            ctrl_row.pack(pady=(4, 0))
            ModernButton(ctrl_row, "\u2212", command=lambda: self.adjust_scale(axis, -0.1),
                         style="ghost", width=30, height=28).pack(side="left")
            value = self.scale_x if axis == "x" else self.scale_y
            lbl = tk.Label(ctrl_row, text=f"{int(round(value * 100))}%", bg=SURFACE, fg=TEXT_PRIMARY,
                            font=(FONT_FAMILY, 9, "bold"), width=5)
            lbl.pack(side="left", padx=4)
            ModernButton(ctrl_row, "+", command=lambda: self.adjust_scale(axis, 0.1),
                         style="ghost", width=30, height=28).pack(side="left")

        make_axis_control("WIDTH", "x")
        make_axis_control("HEIGHT", "y")

    def adjust_scale(self, axis, delta):
        if axis == "x":
            self.scale_x = max(0.3, min(2.0, round(self.scale_x + delta, 2)))
        else:
            self.scale_y = max(0.3, min(2.0, round(self.scale_y + delta, 2)))
        self._rebuild_preview()

    # -- apply-to section (single mode) ----------------------------------
    def _rebuild_apply_section(self):
        for w in self.apply_holder.winfo_children():
            w.destroy()

        visible_types = [(n, f) for n, f in CURSOR_TYPES if not (self.mode == "split" and n == "Hand")]
        for i, (reg_name, friendly) in enumerate(visible_types):
            var = self.type_vars[reg_name]
            cb = ModernCheckbox(self.apply_holder, friendly, var, width=170)
            cb.grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 6), pady=4)

        if self.mode == "split":
            note_row = (len(visible_types) + 2) // 3
            tk.Label(self.apply_holder, text="Clicking pose will always be applied to: Link Select (Hand)",
                      bg=SURFACE, fg=TEXT_FAINT, font=(FONT_FAMILY, 9)).grid(
                row=note_row, column=0, columnspan=3, sticky="w", pady=(8, 0))

    # -- single-image file loading ----------------------------------------
    def browse_file(self):
        path = filedialog.askopenfilename(
            title="Choose cursor image",
            filetypes=[("Images", "*.png *.ico *.cur *.bmp *.jpg *.jpeg *.gif"), ("All files", "*.*")],
        )
        if path:
            self.load_image(path)

    def on_drop(self, event):
        path = event.data.strip("{}")
        self.load_image(path)

    def load_image(self, path):
        try:
            img = Image.open(path)
            img.load()
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Couldn't open that file as an image:\n{e}")
            return

        self.image_path = path
        self.original_image = img.convert("RGBA")
        self.scale_x = 1.0
        self.scale_y = 1.0

        try:
            self.detected_pieces = detect_cursor_segments(self.original_image)
        except Exception:
            self.detected_pieces = [self.original_image]

        initial_mode = "split" if len(self.detected_pieces) == 2 else "single"
        self._set_mode(initial_mode)

        self.use_btn.set_enabled(True)
        self.ai_generate_click_btn.set_enabled(True)
        self._set_status_dot(TEXT_SECONDARY)
        if initial_mode == "split":
            self.status_var.set(f"Loaded: {os.path.basename(path)} \u2014 detected 2 cursor poses (resting + clicking)")
        else:
            self.status_var.set(f"Loaded: {os.path.basename(path)}  ({img.width}\u00d7{img.height})")

    # -- cursor-pack mode --------------------------------------------------
    def _build_pack_section(self, parent):
        tk.Label(parent, text="Cursor Pack", bg=SURFACE, fg=TEXT_PRIMARY,
                  font=(FONT_FAMILY, 13, "bold")).pack(anchor="w")
        tk.Label(parent, text="Drop a .zip cursor pack, or individual .cur / .ani / image files one at a "
                              "time \u2014 files like pointer_wait.cur or pointer_loading.cur are matched "
                              "to the right cursor automatically.",
                  bg=SURFACE, fg=TEXT_SECONDARY, font=(FONT_FAMILY, 9), wraplength=560, justify="left"
                  ).pack(anchor="w", pady=(4, 12))

        self.pack_drop_zone = tk.Canvas(parent, bg=SURFACE_2, highlightthickness=0, height=90, cursor="hand2")
        self.pack_drop_zone.pack(fill="x")
        self.pack_drop_zone.bind("<Configure>", self._redraw_pack_drop_zone)
        self.pack_drop_zone.bind("<Button-1>", lambda e: self.pack_browse_files())

        if DND_AVAILABLE:
            self.pack_drop_zone.drop_target_register(DND_FILES)
            self.pack_drop_zone.dnd_bind("<<Drop>>", self.on_pack_drop)

        self.pack_status_var = tk.StringVar(value="No cursor files added yet.")
        tk.Label(parent, textvariable=self.pack_status_var, bg=SURFACE, fg=TEXT_FAINT,
                  font=(FONT_FAMILY, 9)).pack(anchor="w", pady=(8, 12))

        self.pack_table = ScrollableFrame(parent, bg=SURFACE, height=360)
        self.pack_table.pack(fill="both", expand=True)

        for reg_name, friendly in CURSOR_TYPES:
            self._build_pack_row(self.pack_table.inner, reg_name, friendly)

    def _redraw_pack_drop_zone(self, event):
        c = self.pack_drop_zone
        c.delete("all")
        w, h = max(event.width, 10), max(event.height, 10)
        round_rect(c, 2, 2, w - 2, h - 2, r=12, fill=SURFACE_2, outline=BORDER, width=2, dash=(6, 4))
        c.create_text(w / 2, h / 2 - 9, text="\u2295  Drop .zip / .cur / .ani / images here",
                       fill=TEXT_SECONDARY, font=(FONT_FAMILY, 10))
        c.create_text(w / 2, h / 2 + 13, text="or click to browse", fill=TEXT_FAINT, font=(FONT_FAMILY, 8))

    def _build_pack_row(self, parent, reg_name, friendly):
        row = tk.Frame(parent, bg=SURFACE, pady=4)
        row.pack(fill="x", padx=4)

        thumb = tk.Canvas(row, width=40, height=40, bg=SURFACE, highlightthickness=0)
        thumb.pack(side="left")
        render_pack_thumb(thumb, None)

        text_col = tk.Frame(row, bg=SURFACE)
        text_col.pack(side="left", fill="x", expand=True, padx=(10, 6))
        tk.Label(text_col, text=friendly, bg=SURFACE, fg=TEXT_PRIMARY, font=(FONT_FAMILY, 9, "bold")
                  ).pack(anchor="w")
        status_lbl = tk.Label(text_col, text="Not set", bg=SURFACE, fg=TEXT_FAINT, font=(FONT_FAMILY, 8))
        status_lbl.pack(anchor="w")

        include_var = tk.BooleanVar(value=False)
        cb = ModernCheckbox(row, "Use", include_var, width=58)
        cb.set_enabled(False)
        cb.pack(side="left", padx=(0, 6))

        remove_btn = ModernButton(row, "\u2715", command=lambda rn=reg_name: self.remove_pack_item(rn),
                                   style="ghost", width=32, height=28)
        remove_btn.pack(side="left")

        self.pack_include_vars[reg_name] = include_var
        self.pack_row_widgets[reg_name] = {"thumb": thumb, "status": status_lbl, "checkbox": cb}

    def _refresh_pack_row(self, reg_name):
        widgets = self.pack_row_widgets.get(reg_name)
        if not widgets:
            return
        item = self.pack_items.get(reg_name)
        render_pack_thumb(widgets["thumb"], item)
        if item:
            widgets["status"].config(text=item["source_name"], fg=TEXT_SECONDARY)
            self.pack_include_vars[reg_name].set(True)
            widgets["checkbox"].set_enabled(True)
        else:
            widgets["status"].config(text="Not set", fg=TEXT_FAINT)
            self.pack_include_vars[reg_name].set(False)
            widgets["checkbox"].set_enabled(False)

    def remove_pack_item(self, reg_name):
        if reg_name in self.pack_items:
            del self.pack_items[reg_name]
        self._refresh_pack_row(reg_name)
        self.pack_status_var.set(f"{len(self.pack_items)} of {len(CURSOR_TYPES)} cursor roles set.")
        self.use_btn.set_enabled(len(self.pack_items) > 0)

    def pack_browse_files(self):
        self.set_app_mode("pack")
        paths = filedialog.askopenfilenames(
            title="Choose cursor files or a zip",
            filetypes=[("Cursor pack files", "*.zip *.cur *.ani *.png *.ico *.bmp *.jpg *.jpeg *.gif"),
                       ("All files", "*.*")],
        )
        for p in paths:
            self._handle_pack_path(p)

    def on_pack_drop(self, event):
        raw = event.data
        try:
            paths = self.root.tk.splitlist(raw)
        except Exception:
            paths = [raw.strip("{}")]
        for p in paths:
            p = p.strip("{}")
            self._handle_pack_path(p)

    def _handle_pack_path(self, path):
        ext = os.path.splitext(path)[1].lower()
        if ext == ".zip":
            self.handle_pack_zip(path)
        elif ext in CURSOR_FILE_EXTS or ext in IMAGE_EXTS:
            self.assign_pack_file(path)
        else:
            messagebox.showinfo(APP_NAME, f"Skipped unsupported file: {os.path.basename(path)}")

    def handle_pack_zip(self, zip_path):
        try:
            tmp_dir, mapping = build_pack_from_zip(zip_path)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Couldn't read that zip file:\n{e}")
            return
        if not mapping:
            messagebox.showinfo(APP_NAME, "No matching cursor files were found in that zip.")
            return
        self._pack_temp_dirs.append(tmp_dir)
        added = 0
        for role, path in mapping.items():
            item = make_pack_item(path)
            if item:
                self.pack_items[role] = item
                self._refresh_pack_row(role)
                added += 1
        self.pack_status_var.set(f"Loaded {added} cursor(s) from {os.path.basename(zip_path)}.")
        self.use_btn.set_enabled(len(self.pack_items) > 0)

    def assign_pack_file(self, path, role=None):
        if role is None:
            role = guess_role_for_filename(path)
        if role is None:
            self.prompt_role_for_file(path, lambda r: self._do_assign_pack_file(path, r))
            return
        self._do_assign_pack_file(path, role)

    def _do_assign_pack_file(self, path, role):
        item = make_pack_item(path)
        if item is None:
            messagebox.showerror(APP_NAME, f"Couldn't open:\n{os.path.basename(path)}")
            return
        self.pack_items[role] = item
        self._refresh_pack_row(role)
        friendly = dict(CURSOR_TYPES).get(role, role)
        self.pack_status_var.set(f"{len(self.pack_items)} of {len(CURSOR_TYPES)} cursor roles set "
                                  f"(added {os.path.basename(path)} \u2192 {friendly}).")
        self.use_btn.set_enabled(len(self.pack_items) > 0)

    def prompt_role_for_file(self, path, callback):
        dlg = tk.Toplevel(self.root)
        dlg.title("Which cursor is this?")
        dlg.configure(bg=SURFACE)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()

        pad = tk.Frame(dlg, bg=SURFACE, padx=20, pady=20)
        pad.pack(fill="both", expand=True)
        tk.Label(pad, text=f"Couldn't guess a role for:\n{os.path.basename(path)}", bg=SURFACE, fg=TEXT_PRIMARY,
                  font=(FONT_FAMILY, 10, "bold"), wraplength=340, justify="left").pack(anchor="w", pady=(0, 10))
        tk.Label(pad, text="Pick the cursor role it should replace:", bg=SURFACE, fg=TEXT_SECONDARY,
                  font=(FONT_FAMILY, 9)).pack(anchor="w", pady=(0, 8))

        grid = tk.Frame(pad, bg=SURFACE)
        grid.pack()
        for i, (reg_name, friendly) in enumerate(CURSOR_TYPES):
            def choose(rn=reg_name):
                dlg.destroy()
                callback(rn)
            ModernButton(grid, friendly, command=choose, style="ghost", width=170, height=30).grid(
                row=i // 2, column=i % 2, padx=4, pady=3)

        ModernButton(pad, "Skip this file", command=dlg.destroy, style="ghost", width=150, height=30).pack(
            pady=(14, 0))

    # -- actions ----------------------------------------------------------
    def on_use(self):
        if self.app_mode == "pack":
            self.on_use_pack()
        else:
            self.on_use_single()

    def on_use_single(self):
        if self.original_image is None or not self.pieces:
            return

        if not is_windows():
            messagebox.showinfo(
                APP_NAME,
                "This app changes the system cursor only on Windows. "
                "You're running it elsewhere, so nothing on the OS was changed.",
            )
            return

        try:
            if self.mode == "split" and len(self.pieces) == 2:
                resting_selected = [(n, f) for n, f in CURSOR_TYPES
                                     if n != "Hand" and self.type_vars[n].get()]
                if not resting_selected:
                    messagebox.showwarning(APP_NAME, "Select at least one cursor type for the resting pose.")
                    return
                resting_path = os.path.join(STORE_DIR, "resting_cursor.cur")
                click_path = os.path.join(STORE_DIR, "clicking_cursor.cur")
                save_cur_file(self.pieces[0], self.hotspots[0], resting_path,
                               scale_x=self.scale_x, scale_y=self.scale_y)
                save_cur_file(self.pieces[1], self.hotspots[1], click_path,
                               scale_x=self.scale_x, scale_y=self.scale_y)
                apply_cursors(resting_path, resting_selected)
                apply_cursors(click_path, [("Hand", "Link Select")])
            else:
                selected = [(n, f) for n, f in CURSOR_TYPES if self.type_vars[n].get()]
                if not selected:
                    messagebox.showwarning(APP_NAME, "Select at least one cursor type to apply to.")
                    return
                dest = os.path.join(STORE_DIR, CUR_FILENAME)
                save_cur_file(self.pieces[0], self.hotspots[0], dest,
                               scale_x=self.scale_x, scale_y=self.scale_y)
                apply_cursors(dest, selected)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Couldn't apply the cursor:\n{e}")
            return

        self._set_status_dot(ACCENT)
        self.status_var.set("Cursor applied.")
        messagebox.showinfo(APP_NAME, "Your Windows cursor has been updated.")

    def on_use_pack(self):
        if not self.pack_items:
            messagebox.showwarning(APP_NAME, "Add at least one cursor file first.")
            return
        if not is_windows():
            messagebox.showinfo(APP_NAME, "This app changes the system cursor only on Windows. "
                                           "You're running it elsewhere, so nothing on the OS was changed.")
            return

        role_to_path = {}
        try:
            pack_dir = os.path.join(STORE_DIR, "pack")
            os.makedirs(pack_dir, exist_ok=True)
            for role, item in self.pack_items.items():
                var = self.pack_include_vars.get(role)
                if var is not None and not var.get():
                    continue
                if item["kind"] == "file":
                    ext = os.path.splitext(item["path"])[1] or ".cur"
                    dest = os.path.join(pack_dir, f"{role}{ext}")
                    shutil.copyfile(item["path"], dest)
                else:
                    dest = os.path.join(pack_dir, f"{role}.cur")
                    save_cur_file(item["pil"], item["hotspot"], dest)
                role_to_path[role] = dest

            if not role_to_path:
                messagebox.showwarning(APP_NAME, "Nothing selected to apply.")
                return
            apply_cursor_mapping(role_to_path)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Couldn't apply the cursor pack:\n{e}")
            return

        self._set_status_dot(ACCENT)
        self.pack_status_var.set(f"Applied {len(role_to_path)} cursor(s).")
        self.status_var.set(f"Applied {len(role_to_path)} cursor(s) from your pack.")
        messagebox.showinfo(APP_NAME, f"Applied {len(role_to_path)} cursor role(s) to Windows.")

    def on_cancel(self):
        # Single-cursor state
        self.image_path = None
        self.original_image = None
        self.detected_pieces = []
        self.pieces = []
        self.hotspots = []
        self.mode = "single"
        self.scale_x = 1.0
        self.scale_y = 1.0
        self._rebuild_preview()
        self._rebuild_apply_section()
        self.ai_generate_click_btn.set_enabled(False)

        # Pack state
        self.pack_items = {}
        for reg_name, _ in CURSOR_TYPES:
            self._refresh_pack_row(reg_name)
        self.pack_status_var.set("No cursor files added yet.")

        self.use_btn.set_enabled(False)
        self._set_status_dot(TEXT_FAINT)
        self.status_var.set("Cancelled. No changes were made.")

    def on_reset(self):
        if not is_windows():
            messagebox.showinfo(APP_NAME, "Not running on Windows; nothing to reset.")
            return
        if messagebox.askyesno(APP_NAME, "Reset all cursors to the Windows default scheme?"):
            try:
                reset_cursors_to_default()
                messagebox.showinfo(APP_NAME, "Cursors reset to default.")
            except Exception as e:
                messagebox.showerror(APP_NAME, f"Couldn't reset cursors:\n{e}")

    def open_store_dir(self):
        os.makedirs(STORE_DIR, exist_ok=True)
        if is_windows():
            os.startfile(STORE_DIR)  # noqa: S606
        else:
            messagebox.showinfo(APP_NAME, STORE_DIR)

    def show_about(self):
        messagebox.showinfo(
            APP_NAME,
            f"{APP_NAME}\n\nDrag, preview, and apply custom Windows cursors.\n"
            "Single Cursor: one image, with auto-split for resting + clicking poses, "
            "or generate one with AI.\n"
            "Cursor Pack: drop a .zip cursor set or individual .cur/.ani/image files "
            "one at a time \u2014 matched to the right role automatically.\n\n"
            "Use applies the change; Cancel discards it.",
        )

    def _on_close(self):
        for d in self._pack_temp_dirs:
            shutil.rmtree(d, ignore_errors=True)
        self.root.destroy()


def main():
    if DND_AVAILABLE:
        root = TkinterDnD.Tk()
    else:
        root = tk.Tk()
    CursorChangerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

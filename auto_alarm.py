"""Auto Alarm (widget version) - plays a sound file for a set time.
Setup:  python -m pip install pygame-ce
Run:    python auto_alarm_widget.py
"""
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
import ctypes
from ctypes import wintypes
import tkinter as tk
from tkinter import filedialog
from datetime import datetime, timedelta

import pygame

TRAY_OK = os.name == "nt"  # the tray uses Windows calls only

CFG = os.path.join(os.path.expanduser("~"), ".auto_alarm.json")
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]  # matches datetime.weekday()
VERSION = "1.8"
DEFAULTS = {"sound": "", "dur": 30, "vol": 80, "loop": True, "alarms": [],
            "opacity": 92, "top": True, "tray": True, "popup": True, "pos": None,
            "h24": True}

BG, CARD, LINE = "#12151c", "#1b2029", "#2a303b"
TX, MU = "#e8ebf0", "#8b94a3"
ACC, OK, BAD = "#4f8cff", "#3ddc97", "#ff5d5d"
FONT = "Segoe UI"


def shade(c, amt):
    r, g, b = (int(c[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02x%02x%02x" % tuple(min(255, v + amt) for v in (r, g, b))


def fmt_time(hhmm, h24):
    """Show an 'HH:MM' alarm time as 24-hour or 12-hour text."""
    h, m = map(int, hhmm.split(":"))
    if h24:
        return f"{h:02d}:{m:02d}"
    return f"{h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def flat(parent, text, cmd, bg=CARD, fg=TX, font=(FONT, 9), padx=10, pady=5):
    """A flat, clickable label that acts as a button."""
    l = tk.Label(parent, text=text, bg=bg, fg=fg, font=font, padx=padx, pady=pady, cursor="hand2")
    l.base = bg
    l.bind("<Button-1>", lambda e: cmd())
    l.bind("<Enter>", lambda e: l.config(bg=shade(l.base, 18)))
    l.bind("<Leave>", lambda e: l.config(bg=l.base))
    return l


def paint(l, bg, fg):
    l.base = bg
    l.config(bg=bg, fg=fg)


class Slider(tk.Canvas):
    """A simple horizontal slider that draws clearly on a dark theme."""

    def __init__(self, parent, var, lo, hi, cmd, width=260, bg=CARD):
        super().__init__(parent, width=width, height=24, bg=bg, highlightthickness=0, cursor="hand2")
        self.var, self.lo, self.hi, self.cmd, self.w, self.pad = var, lo, hi, cmd, width, 10
        self.bind("<Button-1>", self.on_mouse)
        self.bind("<B1-Motion>", self.on_mouse)
        self.draw()

    def draw(self):
        self.delete("all")
        span = self.w - 2 * self.pad
        frac = (self.var.get() - self.lo) / (self.hi - self.lo)
        x = self.pad + frac * span
        self.create_line(self.pad, 12, self.w - self.pad, 12, fill=LINE, width=4, capstyle="round")
        self.create_line(self.pad, 12, x, 12, fill=ACC, width=4, capstyle="round")
        self.create_oval(x - 8, 4, x + 8, 20, fill="white", outline=ACC, width=2)

    def on_mouse(self, e):
        span = self.w - 2 * self.pad
        frac = min(1, max(0, (e.x - self.pad) / span))
        self.var.set(round(self.lo + frac * (self.hi - self.lo)))
        self.draw()
        self.cmd()


def build_icon_bits(size=32):
    """Draw a small blue clock icon as raw pixels (no image library needed)."""
    and_bits, xor_bits = bytearray(), bytearray()
    blue, white = (0x4F, 0x8C, 0xFF), (255, 255, 255)
    for y in range(size):
        row = bytearray(size // 8)
        for x in range(size):
            dx, dy = x - 15.5, y - 15.5
            d = (dx * dx + dy * dy) ** 0.5
            if d > 15:
                row[x // 8] |= 0x80 >> (x % 8)  # transparent pixel
                xor_bits += bytes(4)
                continue
            r, g, b = blue if d > 11 else white
            if d <= 11:
                minute = abs(dx) <= 1.3 and -9 <= dy <= 1
                hour = 0 <= dx <= 7 and dy >= 0 and abs(dy - dx * 0.55) <= 1.4
                if minute or hour:
                    r, g, b = blue
            xor_bits += bytes((b, g, r, 255))
        and_bits += row
    return bytes(and_bits), bytes(xor_bits)


class NID(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON), ("szTip", ctypes.c_wchar * 128),
                ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", ctypes.c_wchar * 256), ("uVersion", wintypes.UINT),
                ("szInfoTitle", ctypes.c_wchar * 64), ("dwInfoFlags", wintypes.DWORD)]


class Tray:
    """A system tray icon built on Windows calls only (no extra packages)."""
    MSG = 0x0400 + 20  # our private callback message

    def __init__(self, tip, on_show, on_quit, on_stop=None):
        self.tip, self.on_show, self.on_quit, self.on_stop = tip, on_show, on_quit, on_stop
        self.hwnd = None
        self.nid = None
        self.ok = False
        self.last_tip = 0
        self.ready = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        self.ready.wait(3)
        return self.ok

    def _run(self):
        try:
            W = wintypes
            u, s, k = ctypes.windll.user32, ctypes.windll.shell32, ctypes.windll.kernel32
            self.u, self.s = u, s
            LRES = ctypes.c_ssize_t
            WNDPROC = ctypes.WINFUNCTYPE(LRES, W.HWND, W.UINT, W.WPARAM, W.LPARAM)

            class WNDCLASS(ctypes.Structure):
                _fields_ = [("style", W.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                            ("cbWndExtra", ctypes.c_int), ("hInstance", W.HINSTANCE),
                            ("hIcon", W.HICON), ("hCursor", W.HICON), ("hbrBackground", W.HBRUSH),
                            ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR)]

            k.GetModuleHandleW.argtypes, k.GetModuleHandleW.restype = [W.LPCWSTR], W.HMODULE
            u.DefWindowProcW.argtypes, u.DefWindowProcW.restype = [W.HWND, W.UINT, W.WPARAM, W.LPARAM], LRES
            u.RegisterClassW.argtypes, u.RegisterClassW.restype = [ctypes.POINTER(WNDCLASS)], W.ATOM
            u.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, ctypes.c_int, ctypes.c_int,
                                          ctypes.c_int, ctypes.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID]
            u.CreateWindowExW.restype = W.HWND
            u.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
            u.PostQuitMessage.argtypes = [ctypes.c_int]
            u.GetMessageW.argtypes, u.GetMessageW.restype = [ctypes.POINTER(W.MSG), W.HWND, W.UINT, W.UINT], ctypes.c_int
            u.TranslateMessage.argtypes = [ctypes.POINTER(W.MSG)]
            u.DispatchMessageW.argtypes, u.DispatchMessageW.restype = [ctypes.POINTER(W.MSG)], LRES
            u.CreateIcon.argtypes = [W.HINSTANCE, ctypes.c_int, ctypes.c_int, ctypes.c_ubyte, ctypes.c_ubyte,
                                     ctypes.c_char_p, ctypes.c_char_p]
            u.CreateIcon.restype = W.HICON
            u.LoadIconW.argtypes, u.LoadIconW.restype = [W.HINSTANCE, ctypes.c_void_p], W.HICON
            u.CreatePopupMenu.restype = W.HMENU
            u.AppendMenuW.argtypes = [W.HMENU, W.UINT, ctypes.c_size_t, W.LPCWSTR]
            u.TrackPopupMenu.argtypes = [W.HMENU, W.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.HWND, W.LPVOID]
            u.DestroyMenu.argtypes = [W.HMENU]
            u.SetForegroundWindow.argtypes = [W.HWND]
            u.GetCursorPos.argtypes = [ctypes.POINTER(W.POINT)]
            s.Shell_NotifyIconW.argtypes, s.Shell_NotifyIconW.restype = [W.DWORD, ctypes.POINTER(NID)], W.BOOL

            hinst = k.GetModuleHandleW(None)
            self._proc = WNDPROC(self._wndproc)  # keep a reference so it is not freed
            wc = WNDCLASS()
            wc.lpfnWndProc, wc.hInstance, wc.lpszClassName = self._proc, hinst, "AutoAlarmTray"
            u.RegisterClassW(ctypes.byref(wc))
            self.hwnd = u.CreateWindowExW(0, "AutoAlarmTray", "AutoAlarmTray", 0, 0, 0, 0, 0,
                                          None, None, hinst, None)

            and_bits, xor_bits = build_icon_bits()
            icon = u.CreateIcon(hinst, 32, 32, 1, 32, and_bits, xor_bits) or u.LoadIconW(None, 32512)

            nid = NID()
            nid.cbSize = ctypes.sizeof(NID)
            nid.hWnd, nid.uID, nid.uFlags = self.hwnd, 1, 1 | 2 | 4
            nid.uCallbackMessage, nid.hIcon, nid.szTip = self.MSG, icon, self.tip[:120]
            self.nid = nid
            self.ok = bool(s.Shell_NotifyIconW(0, ctypes.byref(nid)))  # NIM_ADD
            self.ready.set()
            if not self.ok:
                return
            msg = W.MSG()
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                u.TranslateMessage(ctypes.byref(msg))
                u.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            self.ok = False
        finally:
            self.ready.set()

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == self.MSG:
                ev = lp & 0xFFFF
                if ev == 0x0202:      # left click
                    self.on_show()
                elif ev == 0x0205:    # right click
                    self._menu(hwnd)
                return 0
            if msg == 0x0002:         # window destroyed: end the message loop
                self.u.PostQuitMessage(0)
                return 0
        except Exception:
            pass
        return self.u.DefWindowProcW(hwnd, msg, wp, lp)

    def _menu(self, hwnd):
        u = self.u
        pt = wintypes.POINT()
        u.GetCursorPos(ctypes.byref(pt))
        u.SetForegroundWindow(hwnd)
        menu = u.CreatePopupMenu()
        u.AppendMenuW(menu, 0, 1, "Show")
        u.AppendMenuW(menu, 0, 3, "Stop sound")
        u.AppendMenuW(menu, 0, 2, "Quit")
        choice = u.TrackPopupMenu(menu, 0x0100 | 0x0002, pt.x, pt.y, 0, hwnd, None)  # return the choice
        u.PostMessageW(hwnd, 0, 0, 0)
        u.DestroyMenu(menu)
        if choice == 1:
            self.on_show()
        elif choice == 2:
            self.on_quit()
        elif choice == 3 and self.on_stop:
            self.on_stop()

    def set_tip(self, text):
        if not self.ok or time.time() - self.last_tip < 5:
            return
        self.last_tip = time.time()
        self.nid.szTip = text[:120]
        self.s.Shell_NotifyIconW(1, ctypes.byref(self.nid))  # NIM_MODIFY

    def stop(self):
        try:
            if self.nid is not None:
                self.s.Shell_NotifyIconW(2, ctypes.byref(self.nid))  # NIM_DELETE
            if self.hwnd:
                self.u.PostMessageW(self.hwnd, 0x0010, 0, 0)  # WM_CLOSE
        except Exception:
            pass


def close_other_instances():
    """Close every other running copy of this .exe, so only the newest one stays.
    Works only in the PyInstaller build; running the .py file skips it."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    exe = os.path.basename(sys.executable)
    cmd = ["taskkill", "/F", "/IM", exe]
    for pid in (os.getpid(), os.getppid()):  # keep this process and its PyInstaller launcher
        cmd += ["/FI", f"PID ne {pid}"]
    try:
        quiet = subprocess.DEVNULL
        subprocess.run(cmd, stdin=quiet, stdout=quiet, stderr=quiet,
                       creationflags=0x08000000, timeout=10)  # no console window
        time.sleep(0.5)
    except Exception:
        pass


def win_api():
    """user32 with correct argument types (needed for 64-bit Windows)."""
    u, W = ctypes.windll.user32, wintypes
    u.GetParent.argtypes, u.GetParent.restype = [W.HWND], W.HWND
    u.GetWindowLongW.argtypes, u.GetWindowLongW.restype = [W.HWND, ctypes.c_int], ctypes.c_long
    u.SetWindowLongW.argtypes, u.SetWindowLongW.restype = [W.HWND, ctypes.c_int, ctypes.c_long], ctypes.c_long
    u.ShowWindow.argtypes = [W.HWND, ctypes.c_int]
    u.SendMessageW.argtypes, u.SendMessageW.restype = [W.HWND, W.UINT, W.WPARAM, W.LPARAM], ctypes.c_ssize_t
    u.CreateIcon.argtypes = [W.HINSTANCE, ctypes.c_int, ctypes.c_int, ctypes.c_ubyte, ctypes.c_ubyte,
                             ctypes.c_char_p, ctypes.c_char_p]
    u.CreateIcon.restype = W.HICON
    return u


def keep_awake():
    """Ask Windows not to sleep while the app runs."""
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
    except Exception:
        pass


def to_int(var, default):
    try:
        return int(var.get())
    except (tk.TclError, ValueError):
        return default


class App:
    def __init__(self, root):
        self.root = root
        root.title("Auto Alarm")
        self.cfg = self.load()
        self.next = {}
        self.last = {}
        self.ring_end = 0
        self.banner_label = ""
        self.tray = None
        self.hidden = False
        self.hwnd = None
        self.wins = {}        # pop-out overlays: "set" and "add"
        self.current = None   # name of the overlay that is open now
        self.titles = {}      # overlay title labels
        self.editing = None   # the alarm being edited (None = adding a new one)
        self.cmds = queue.Queue()  # messages from the tray thread

        c = self.cfg
        self.dur = tk.StringVar(value=str(c["dur"]))
        self.vol = tk.IntVar(value=c["vol"])
        self.opa = tk.IntVar(value=c["opacity"])
        self.loop = tk.BooleanVar(value=c["loop"])
        self.top = tk.BooleanVar(value=c["top"])
        self.tray_on = tk.BooleanVar(value=c["tray"] and TRAY_OK)
        self.popup = tk.BooleanVar(value=c["popup"])
        self.h24 = tk.BooleanVar(value=c["h24"])
        self.pm = False       # AM/PM choice in the Add form (12-hour mode)
        self.dur.trace_add("write", self.save)

        try:
            pygame.mixer.init()
            self.audio_ok = True
        except pygame.error:
            self.audio_ok = False

        # let typing work in borderless windows
        root.bind_class("Spinbox", "<Button-1>", lambda e: e.widget.focus_force(), add="+")

        self.build()
        self.build_settings()
        self.build_add()
        for a in self.cfg["alarms"]:
            self.arm(a)
        self.refresh_list()
        keep_awake()
        self.tick()
        root.after(200, self.taskbar_button)
        root.protocol("WM_DELETE_WINDOW", self.close)

    # ---------- storage ----------
    def load(self):
        cfg = dict(DEFAULTS)
        try:
            with open(CFG, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (OSError, ValueError):
            pass
        return cfg

    def save(self, *_):
        self.cfg["dur"] = max(1, to_int(self.dur, 30))
        self.cfg["vol"] = int(self.vol.get())
        self.cfg["loop"] = bool(self.loop.get())
        self.cfg["opacity"] = int(self.opa.get())
        self.cfg["top"] = bool(self.top.get())
        self.cfg["tray"] = bool(self.tray_on.get())
        self.cfg["popup"] = bool(self.popup.get())
        self.cfg["h24"] = bool(self.h24.get())
        try:
            if not self.hidden:
                self.cfg["pos"] = [self.root.winfo_x(), self.root.winfo_y()]
            with open(CFG, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=2)
        except (OSError, tk.TclError):
            pass

    # ---------- main window ----------
    def build(self):
        r = self.root
        r.overrideredirect(True)
        r.configure(bg=LINE)
        r.attributes("-topmost", self.cfg["top"])
        r.attributes("-alpha", self.cfg["opacity"] / 100)
        pos = self.cfg["pos"]
        if pos:
            r.geometry(f"+{pos[0]}+{pos[1]}")
        else:
            r.geometry(f"+{r.winfo_screenwidth() - 340}+60")

        body = tk.Frame(r, bg=BG)
        body.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Frame(body, width=300, height=1, bg=BG).pack()

        bar = tk.Frame(body, bg=BG)
        bar.pack(fill="x", padx=8, pady=(6, 0))
        title = tk.Label(bar, text="⏰  Auto Alarm", bg=BG, fg=MU, font=(FONT, 9))
        title.pack(side="left")
        flat(bar, "✕", self.close, bg=BG, fg=MU, padx=8, pady=2).pack(side="right")
        flat(bar, "–", self.minimize, bg=BG, fg=MU, padx=8, pady=2).pack(side="right")
        for w in (bar, title):
            w.bind("<ButtonPress-1>", self.drag_start)
            w.bind("<B1-Motion>", self.drag_move)
            w.bind("<ButtonRelease-1>", self.save)

        clockrow = tk.Frame(body, bg=BG)
        clockrow.pack(pady=(2, 0))
        self.clock = tk.Label(clockrow, bg=BG, fg=TX, font=(FONT, 34, "bold"))
        self.clock.pack(side="left")
        self.ampm = tk.Label(clockrow, bg=BG, fg=MU, font=(FONT, 12, "bold"))  # shown in 12-hour mode
        self.nextlbl = tk.Label(body, bg=BG, fg=MU, font=(FONT, 9))
        self.nextlbl.pack(pady=(0, 8))

        self.banner = tk.Frame(body, bg=BAD)
        self.banner_txt = tk.Label(self.banner, bg=BAD, fg="white", font=(FONT, 9, "bold"))
        self.banner_txt.pack(side="left", padx=10, pady=8)
        flat(self.banner, "Stop", self.stop, bg="white", fg="#111", font=(FONT, 9, "bold"),
             pady=3).pack(side="right", padx=8)

        self.listwrap = tk.Frame(body, bg=BG)
        self.listwrap.pack(fill="x", padx=12)

        acts = tk.Frame(body, bg=BG)
        acts.pack(fill="x", padx=12, pady=8)
        flat(acts, "＋  Add alarm", self.toggle_add, bg=ACC, fg="white",
             font=(FONT, 9, "bold"), pady=6).pack(side="left", fill="x", expand=True)
        flat(acts, "⚙  Settings", self.toggle_settings, bg=CARD,
             pady=6).pack(side="left", padx=(8, 0))

        self.msg = tk.Label(body, bg=BG, fg=BAD, font=(FONT, 8), wraplength=270)
        self.msg.pack(pady=(2, 8))

    def build_add(self):
        t, p = self.make_overlay("add", "＋  Add alarm")
        self.addwin = t
        self.add_mode = "time"
        row = tk.Frame(p, bg=CARD)
        row.pack(fill="x", padx=10, pady=(10, 6))
        self.mchips = {}
        for key, text in (("time", "At a time"), ("every", "Auto repeat")):
            c = flat(row, text, lambda k=key: self.set_mode(k), pady=4)
            c.pack(side="left", padx=(0, 6))
            self.mchips[key] = c

        self.timebox = tk.Frame(p, bg=CARD)
        t = tk.Frame(self.timebox, bg=CARD)
        t.pack(anchor="w", padx=10)
        self.hh = tk.StringVar(value="07")
        self.mm = tk.StringVar(value="00")
        for var, hi in ((self.hh, 23), (self.mm, 59)):
            sb = tk.Spinbox(t, from_=0, to=hi, width=3, format="%02.0f", textvariable=var, wrap=True,
                            font=(FONT, 16, "bold"), bg=BG, fg=TX, buttonbackground=LINE,
                            insertbackground=TX, relief="flat", justify="center")
            sb.pack(side="left", padx=(0, 4))
            if var is self.hh:
                self.hh_box = sb
                tk.Label(t, text=":", bg=CARD, fg=TX, font=(FONT, 16, "bold")).pack(side="left", padx=(0, 4))
        self.ampm_btn = flat(t, "AM", self.flip_pm, bg=LINE, fg=TX, font=(FONT, 10, "bold"), padx=8, pady=6)
        dr = tk.Frame(self.timebox, bg=CARD)
        dr.pack(anchor="w", padx=10, pady=8)
        self.dayon = [True] * 7
        self.chips = []
        for i, letter in enumerate("MTWTFSS"):
            c = flat(dr, letter, lambda i=i: self.chip(i), bg=ACC, fg="white",
                     font=(FONT, 9, "bold"), padx=7, pady=3)
            c.pack(side="left", padx=2)
            self.chips.append(c)

        self.everybox = tk.Frame(p, bg=CARD)
        e = tk.Frame(self.everybox, bg=CARD)
        e.pack(anchor="w", padx=10, pady=4)
        tk.Label(e, text="Every", bg=CARD, fg=MU, font=(FONT, 9)).pack(side="left")
        self.every = tk.StringVar(value="30")
        tk.Spinbox(e, from_=0.5, to=1440, increment=0.5, width=5, textvariable=self.every, font=(FONT, 14, "bold"),
                   bg=BG, fg=TX, buttonbackground=LINE, insertbackground=TX, relief="flat",
                   justify="center").pack(side="left", padx=6)
        tk.Label(e, text="minutes", bg=CARD, fg=MU, font=(FONT, 9)).pack(side="left")

        self.savebtn = flat(p, "Save alarm", self.add, bg=OK, fg="#0b1a13", font=(FONT, 9, "bold"), pady=6)
        self.savebtn.pack(fill="x", padx=10, pady=(4, 4))
        self.addmsg = tk.Label(p, bg=CARD, fg=BAD, font=(FONT, 8))
        self.addmsg.pack(pady=(0, 8))
        self.apply_clock_mode()
        self.set_form_time(7, 0)
        self.set_mode("time")
        return p

    # ---------- 12-hour / 24-hour form helpers ----------
    def apply_clock_mode(self):
        """Set the hour box range and show or hide the AM/PM button."""
        if self.h24.get():
            self.hh_box.config(from_=0, to=23)
            self.ampm_btn.pack_forget()
        else:
            self.hh_box.config(from_=1, to=12)
            self.ampm_btn.pack(side="left", padx=(4, 0))

    def set_form_time(self, hour24, minute):
        """Fill the time boxes from a 24-hour value, in the current format."""
        if self.h24.get():
            self.hh.set(f"{hour24:02d}")
        else:
            self.hh.set(f"{hour24 % 12 or 12:02d}")
            self.pm = hour24 >= 12
            self.ampm_btn.config(text="PM" if self.pm else "AM")
        self.mm.set(f"{minute:02d}")

    def form_hour(self, h24):
        """Read the hour box as a 24-hour value, given which format it is in."""
        h = to_int(self.hh, 7)
        if h24:
            return h % 24
        return h % 12 + (12 if self.pm else 0)

    def flip_pm(self):
        self.pm = not self.pm
        self.ampm_btn.config(text="PM" if self.pm else "AM")

    def on_h24(self):
        """The 24-hour checkbox: keep the form's time, change its format."""
        hour24 = self.form_hour(not self.h24.get())  # read it in the old format
        minute = to_int(self.mm, 0) % 60
        self.apply_clock_mode()
        self.set_form_time(hour24, minute)
        self.save()
        self.refresh_list()
        if self.current == "add":
            self.place_overlay()

    def set_mode(self, key):
        self.add_mode = key
        for k, c in self.mchips.items():
            paint(c, ACC if k == key else LINE, "white" if k == key else MU)
        self.timebox.pack_forget()
        self.everybox.pack_forget()
        (self.timebox if key == "time" else self.everybox).pack(fill="x", before=self.savebtn)
        if self.current == "add":
            self.place_overlay()

    def chip(self, i):
        self.dayon[i] = not self.dayon[i]
        on = self.dayon[i]
        paint(self.chips[i], ACC if on else BG, "white" if on else MU)

    def toggle_add(self):
        """The ＋ Add alarm button: open a blank form, or close the panel."""
        if self.current == "add" and self.editing is None:
            self.toggle_overlay("add")
            return
        self.reset_form()
        if self.current != "add":
            self.toggle_overlay("add")

    def paint_chips(self):
        for i, c in enumerate(self.chips):
            on = self.dayon[i]
            paint(c, ACC if on else BG, "white" if on else MU)

    def reset_form(self):
        """Back to 'add' mode. After an edit, also clear the form to its defaults."""
        was_editing = self.editing is not None
        self.editing = None
        self.titles["add"].config(text="＋  Add alarm")
        self.savebtn.config(text="Save alarm")
        self.addmsg.config(text="")
        if was_editing:
            self.set_form_time(7, 0)
            self.every.set("30")
            self.dayon[:] = [True] * 7
            self.paint_chips()
            self.set_mode("time")

    def edit(self, a):
        """Open the form filled with an existing alarm."""
        self.editing = a
        self.addmsg.config(text="")
        self.titles["add"].config(text="✎  Edit alarm")
        self.savebtn.config(text="Save changes")
        if a["mode"] == "time":
            h, m = a["time"].split(":")
            self.set_form_time(int(h), int(m))
            self.dayon[:] = [d in a["days"] for d in range(7)]
            self.paint_chips()
        else:
            self.every.set(f"{a['every']:g}")
        if self.current != "add":
            self.toggle_overlay("add")
        self.set_mode(a["mode"])

    def fit(self):
        """Resize the window to fit its content (removes leftover blank space)."""
        try:
            self.root.update_idletasks()
            self.root.geometry(f"{self.root.winfo_reqwidth()}x{self.root.winfo_reqheight()}")
            if self.current:
                self.place_overlay()
        except (tk.TclError, AttributeError):
            pass

    # ---------- pop-out overlays (Add alarm, Settings) ----------
    def make_overlay(self, name, title):
        t = tk.Toplevel(self.root)
        t.withdraw()
        t.overrideredirect(True)
        t.configure(bg=LINE)
        t.attributes("-topmost", self.cfg["top"])
        t.attributes("-alpha", self.cfg["opacity"] / 100)
        self.wins[name] = t
        b = tk.Frame(t, bg=CARD)
        b.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Frame(b, width=280, height=1, bg=CARD).pack()
        bar = tk.Frame(b, bg=CARD)
        bar.pack(fill="x", padx=8, pady=(6, 0))
        lbl = tk.Label(bar, text=title, bg=CARD, fg=TX, font=(FONT, 10, "bold"))
        lbl.pack(side="left", padx=2)
        self.titles[name] = lbl
        flat(bar, "✕", lambda: self.toggle_overlay(name), bg=CARD, fg=MU, padx=8, pady=2).pack(side="right")
        return t, b

    def build_settings(self):
        t, b = self.make_overlay("set", "⚙  Settings")
        self.settings = t

        row = tk.Frame(b, bg=CARD)
        row.pack(fill="x", padx=12, pady=(10, 2))
        flat(row, "Choose sound…", self.choose, bg=ACC, fg="white", pady=4).pack(side="left")
        flat(row, "Test", lambda: self.ring("test"), bg=LINE, pady=4).pack(side="left", padx=6)
        self.fname = tk.Label(b, bg=CARD, fg=MU, font=(FONT, 8), wraplength=250, justify="left")
        self.fname.pack(anchor="w", padx=12)
        self.set_fname()

        dr = tk.Frame(b, bg=CARD)
        dr.pack(fill="x", padx=12, pady=(10, 0))
        tk.Label(dr, text="Play for (seconds)", bg=CARD, fg=TX, font=(FONT, 9)).pack(side="left")
        tk.Spinbox(dr, from_=1, to=3600, width=5, textvariable=self.dur, command=self.save,
                   bg=BG, fg=TX, buttonbackground=LINE, insertbackground=TX, relief="flat",
                   justify="center").pack(side="right")

        self.slider_row(b, "Volume", self.vol, 0, 100, self.on_vol, "%")
        self.slider_row(b, "Opacity (lower = clearer)", self.opa, 30, 100, self.on_opacity, "%")

        tk.Frame(b, height=6, bg=CARD).pack()
        checks = [(self.h24, "Use 24-hour clock", self.on_h24),
                  (self.loop, "Loop sound until time is up", self.save),
                  (self.top, "Keep on top of other windows", self.on_top),
                  (self.popup, "Show the widget when an alarm rings", self.save),
                  (self.tray_on, "Hide to tray when I close (✕)", self.save)]
        for var, text, cmd in checks:
            state = "disabled" if (var is self.tray_on and not TRAY_OK) else "normal"
            tk.Checkbutton(b, text=text, variable=var, command=cmd, bg=CARD, fg=TX, state=state,
                           selectcolor=BG, activebackground=CARD, activeforeground=TX,
                           disabledforeground=MU, highlightthickness=0, bd=0,
                           font=(FONT, 9)).pack(anchor="w", padx=10)
        if not TRAY_OK:
            tk.Label(b, text="Tray icon works on Windows only.", bg=CARD, fg=MU,
                     font=(FONT, 8)).pack(anchor="w", padx=12)

        flat(b, "Quit app", self.quit_app, bg=LINE, fg=BAD, font=(FONT, 9, "bold"),
             pady=6).pack(fill="x", padx=12, pady=(12, 4))
        tk.Label(b, text=f"Version {VERSION}", bg=CARD, fg=MU, font=(FONT, 8)).pack(pady=(0, 8))

    def slider_row(self, parent, label, var, lo, hi, cmd, suffix):
        top = tk.Frame(parent, bg=CARD)
        top.pack(fill="x", padx=12, pady=(10, 0))
        tk.Label(top, text=label, bg=CARD, fg=TX, font=(FONT, 9)).pack(side="left")
        val = tk.Label(top, text=f"{var.get()}{suffix}", bg=CARD, fg=MU, font=(FONT, 9))
        val.pack(side="right")

        def changed():
            val.config(text=f"{var.get()}{suffix}")
            cmd()
        Slider(parent, var, lo, hi, changed).pack(padx=8)

    def place_overlay(self, t=None):
        t = t or self.wins.get(self.current)
        if t is None:
            return
        r = self.root
        t.update_idletasks()
        w, h = t.winfo_reqwidth(), t.winfo_reqheight()
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        x = r.winfo_x() + r.winfo_width() + 8
        if x + w > sw:
            x = r.winfo_x() - w - 8
        y = min(r.winfo_y(), max(0, sh - h - 48))
        t.geometry(f"{w}x{h}+{max(0, x)}+{y}")

    def toggle_settings(self):
        self.toggle_overlay("set")

    def toggle_overlay(self, name):
        """Open one overlay; opening another closes the first."""
        was = self.current
        self.close_settings()
        if was == name:
            return
        self.current = name
        t = self.wins[name]
        self.place_overlay(t)
        t.deiconify()
        t.lift()

    def close_settings(self):
        """Close whichever overlay is open."""
        if self.current:
            was = self.current
            self.wins[was].withdraw()
            self.current = None
            if was == "add":
                self.reset_form()

    def set_fname(self):
        p = self.cfg["sound"]
        self.fname.config(text=os.path.basename(p) if p else "No sound chosen yet.")

    def choose(self):
        p = filedialog.askopenfilename(
            parent=self.settings, title="Choose a sound",
            filetypes=[("Audio", "*.mp3 *.wav *.ogg *.flac"), ("All files", "*.*")])
        if p:
            self.cfg["sound"] = p
            self.set_fname()
            self.save()

    def on_opacity(self):
        a = self.opa.get() / 100
        for w in [self.root, *self.wins.values()]:
            w.attributes("-alpha", a)
        self.save()

    def on_top(self):
        for w in [self.root, *self.wins.values()]:
            w.attributes("-topmost", self.top.get())
        self.save()

    def on_vol(self):
        if self.audio_ok:
            pygame.mixer.music.set_volume(self.vol.get() / 100)
        self.save()

    # ---------- window control and tray ----------
    def drag_start(self, e):
        self.dx, self.dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def drag_move(self, e):
        self.root.geometry(f"+{e.x_root - self.dx}+{e.y_root - self.dy}")
        if self.current:
            self.place_overlay()

    def taskbar_button(self):
        """Give the borderless window its own taskbar button and icon (Windows only)."""
        if os.name != "nt":
            return
        try:
            u = win_api()
            hwnd = u.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            ex = u.GetWindowLongW(hwnd, -20)
            u.SetWindowLongW(hwnd, -20, (ex & ~0x80) | 0x40000)  # app window, not tool window
            st = u.GetWindowLongW(hwnd, -16)
            u.SetWindowLongW(hwnd, -16, st | 0x20000)            # allow minimize from the taskbar
            and_bits, xor_bits = build_icon_bits()
            icon = u.CreateIcon(None, 32, 32, 1, 32, and_bits, xor_bits)
            if icon:
                u.SendMessageW(hwnd, 0x0080, 0, icon)            # small icon
                u.SendMessageW(hwnd, 0x0080, 1, icon)            # large icon
            self.hwnd = hwnd
            self.root.withdraw()                                 # re-show so Windows adds the button
            self.root.after(50, self.root.deiconify)
        except Exception:
            self.hwnd = None

    def minimize(self):
        self.close_settings()
        try:
            if self.hwnd:
                win_api().ShowWindow(self.hwnd, 6)  # SW_MINIMIZE: goes to the taskbar
            else:
                self.root.iconify()
        except Exception:
            pass

    def close(self):
        """The ✕ button: hide to tray if the option is on, otherwise quit."""
        if self.tray_on.get() and TRAY_OK:
            self.hide_to_tray()
        else:
            self.quit_app()

    def hide_to_tray(self):
        self.save()
        if self.tray is None:
            tray = Tray("Auto Alarm", lambda: self.cmds.put("show"), lambda: self.cmds.put("quit"),
                        lambda: self.cmds.put("stop"))
            if not tray.start():
                self.msg.config(text="Could not create the tray icon.")
                return  # keep the window visible so it is not lost
            self.tray = tray
        self.close_settings()
        self.hidden = True
        self.root.withdraw()

    def show_main(self):
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
            self.tray = None
        self.hidden = False
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", self.top.get())

    def quit_app(self):
        self.save()
        if self.audio_ok:
            pygame.mixer.music.stop()
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()

    # ---------- alarms ----------
    def arm(self, a):
        if a["mode"] == "every":
            self.next[a["id"]] = time.time() + a["every"] * 60

    def add(self):
        old = self.editing
        a = {"id": old["id"] if old else int(time.time() * 1000), "on": old["on"] if old else True}
        if self.add_mode == "time":
            a["mode"] = "time"
            a["time"] = f"{self.form_hour(self.h24.get()):02d}:{to_int(self.mm, 0) % 60:02d}"
            a["days"] = [i for i, v in enumerate(self.dayon) if v]
            if not a["days"]:
                self.addmsg.config(text="Pick at least one day.")
                return
        else:
            try:
                mins = float(self.every.get().strip().replace(",", "."))
            except ValueError:
                self.addmsg.config(text="Enter minutes, like 30 or 1.5.")
                return
            mins = max(0.1, round(mins, 2))  # shortest repeat: 6 seconds
            a["mode"] = "every"
            a["every"] = int(mins) if mins == int(mins) else mins
        self.addmsg.config(text="")
        if old and old in self.cfg["alarms"]:
            self.cfg["alarms"][self.cfg["alarms"].index(old)] = a
            self.last.pop(a["id"], None)
            self.next.pop(a["id"], None)
        else:
            self.cfg["alarms"].append(a)
        self.arm(a)
        self.save()
        self.refresh_list()
        self.toggle_overlay("add")  # closes the panel

    def toggle(self, a):
        a["on"] = not a["on"]
        self.arm(a)
        self.save()
        self.refresh_list()

    def delete(self, a):
        if a is self.editing:
            self.close_settings()
        self.cfg["alarms"].remove(a)
        self.save()
        self.refresh_list()

    def refresh_list(self):
        for w in self.listwrap.winfo_children():
            w.destroy()
        if not self.cfg["alarms"]:
            tk.Label(self.listwrap, text="No alarms yet.", bg=BG, fg=MU, font=(FONT, 9)).pack(pady=4)
        for a in self.cfg["alarms"]:
            on = a["on"]
            row = tk.Frame(self.listwrap, bg=CARD)
            row.pack(fill="x", pady=3)
            left = tk.Frame(row, bg=CARD)
            left.pack(side="left", padx=10, pady=6)
            if a["mode"] == "time":
                big = fmt_time(a["time"], self.h24.get())
                sub = "Every day" if len(a["days"]) == 7 else " ".join(DAYS[d] for d in a["days"])
            else:
                big, sub = f"Every {a['every']} min", "Auto repeat"
            tk.Label(left, text=big, font=(FONT, 14, "bold"), bg=CARD,
                     fg=TX if on else MU).pack(anchor="w")
            tk.Label(left, text=sub, font=(FONT, 8), bg=CARD, fg=MU, wraplength=150,
                     justify="left").pack(anchor="w")
            for w in (left, *left.winfo_children()):  # click the text to edit
                w.config(cursor="hand2")
                w.bind("<Button-1>", lambda e, a=a: self.edit(a))
            flat(row, "✕", lambda a=a: self.delete(a), bg=CARD, fg=MU, padx=6).pack(side="right", padx=(0, 4))
            flat(row, "✎", lambda a=a: self.edit(a), bg=CARD, fg=MU, padx=6).pack(side="right")
            flat(row, "ON" if on else "OFF", lambda a=a: self.toggle(a),
                 bg=OK if on else LINE, fg="#0b1a13" if on else MU,
                 font=(FONT, 8, "bold"), padx=10, pady=3).pack(side="right", padx=4)
        self.fit()

    def next_text(self, now):
        best, label = None, ""
        for a in self.cfg["alarms"]:
            if not a["on"]:
                continue
            t = None
            if a["mode"] == "time":
                h, m = map(int, a["time"].split(":"))
                for d in range(8):
                    c = (now + timedelta(days=d)).replace(hour=h, minute=m, second=0, microsecond=0)
                    if c > now and c.weekday() in a["days"]:
                        t = c.timestamp()
                        break
            else:
                t = self.next.get(a["id"])
            if t and (best is None or t < best):
                best = t
                label = (fmt_time(a["time"], self.h24.get()) if a["mode"] == "time"
                         else f"every {a['every']} min")
        if best is None:
            return "No alarm set"
        s = max(0, math.ceil(best - now.timestamp()))
        h, m, sec = s // 3600, s % 3600 // 60, s % 60
        return f"Next alarm in {f'{h}h ' if h else ''}{f'{m}m ' if h or m else ''}{sec}s  ·  {label}"

    # ---------- sound ----------
    def ring(self, label):
        self.msg.config(text="")
        path = self.cfg["sound"]
        if not self.audio_ok or not os.path.isfile(path):
            self.msg.config(text="Choose a sound file first (⚙ Settings).")
            return
        try:
            pygame.mixer.music.load(path)
            pygame.mixer.music.set_volume(self.vol.get() / 100)
            pygame.mixer.music.play(-1 if self.loop.get() else 0)
        except pygame.error as e:
            self.msg.config(text=f"Cannot play file: {e}")
            return
        if self.popup.get():  # bring the widget back so you can press Stop
            if self.hidden:
                self.show_main()
            else:
                try:
                    if self.root.state() == "iconic":  # minimized to the taskbar
                        self.root.deiconify()
                        self.root.lift()
                except tk.TclError:
                    pass
        self.ring_end = time.time() + max(1, to_int(self.dur, 30))
        self.banner_label = label
        self.banner.pack(fill="x", padx=12, pady=(0, 8), before=self.listwrap)
        self.fit()

    def stop(self):
        if self.audio_ok:
            pygame.mixer.music.stop()
        self.ring_end = 0
        self.banner.pack_forget()
        self.fit()

    def tick(self):
        # messages from the tray icon
        try:
            while True:
                cmd = self.cmds.get_nowait()
                if cmd == "show":
                    self.show_main()
                elif cmd == "stop":
                    self.stop()
                elif cmd == "quit":
                    self.quit_app()
                    return
        except queue.Empty:
            pass

        try:
            if self.current and self.root.state() == "iconic":
                self.close_settings()  # no floating panels while minimized
        except tk.TclError:
            pass

        now = datetime.now()
        text = self.next_text(now)
        if self.h24.get():
            self.clock.config(text=now.strftime("%H:%M:%S"))
            self.ampm.pack_forget()
        else:
            self.clock.config(text=f"{now.hour % 12 or 12:02d}{now.strftime(':%M:%S')}")
            self.ampm.config(text="AM" if now.hour < 12 else "PM")
            self.ampm.pack(side="left", padx=(6, 0), pady=(14, 0))
        self.nextlbl.config(text=text)
        if self.tray is not None:
            self.tray.set_tip("Auto Alarm · " + text)

        key = now.strftime("%Y%m%d%H%M")
        for a in self.cfg["alarms"]:
            if not a["on"]:
                continue
            if a["mode"] == "time":
                if (a["time"] == now.strftime("%H:%M") and now.weekday() in a["days"]
                        and self.last.get(a["id"]) != key):
                    self.last[a["id"]] = key
                    self.ring(fmt_time(a["time"], self.h24.get()))
            elif self.next.get(a["id"]) and time.time() >= self.next[a["id"]]:
                self.arm(a)
                self.ring(f"every {a['every']} min")

        if self.ring_end:
            left = math.ceil(self.ring_end - time.time())
            if left <= 0 or (not self.loop.get() and self.audio_ok and not pygame.mixer.music.get_busy()):
                self.stop()
            else:
                self.banner_txt.config(text=f"Ringing · {left}s left")
        self.root.after(500, self.tick)


if __name__ == "__main__":
    close_other_instances()
    root = tk.Tk()
    App(root)
    root.mainloop()
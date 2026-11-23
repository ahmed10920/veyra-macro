"""Standalone Tkinter Calibrator & Overlay Toast helper for Veyra."""
import ctypes
import os
import queue
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox

HERE = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
LOCAL_DEPS = os.path.join(HERE, 'deps')
if os.path.isdir(LOCAL_DEPS) and LOCAL_DEPS not in sys.path:
    sys.path.insert(0, LOCAL_DEPS)
if os.path.isdir(r'D:\mmacro_deps') and r'D:\mmacro_deps' not in sys.path:
    sys.path.insert(0, r'D:\mmacro_deps')

from pynput import mouse
import game
import eden

# Black & White (BW) theme — monochrome
BG = '#000000'
SIDEBAR_BG = '#0c0c0c'
PANEL = '#121212'
PANEL_ALT = '#161616'
SUNK = '#080808'
EDGE = '#222222'
TEXT = '#ffffff'
SUBTEXT = '#bbbbbb'
DIM = '#777777'
ACCENT = '#ffffff'
ACCENT_HOVER = '#cccccc'
ACCENT_BG = '#181818'
GOOD = '#cccccc'
GOOD_BG = '#0e0e0e'
WARN = '#cccccc'
BAD = '#aaaaaa'

UI, UIB = 'Segoe UI', 'Segoe UI Semibold'
SMALL_BUTTON = dict(bg=PANEL_ALT, fg=TEXT, activebackground=ACCENT, activeforeground=BG, relief='flat',
                    font=(UIB, 9), pady=5, cursor='hand2', borderwidth=0,
                    highlightthickness=1, highlightbackground=EDGE)


def dpi_aware():
    for aware in (lambda: ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)),
                  lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0,
                  lambda: ctypes.windll.user32.SetProcessDPIAware()):
        try:
            if aware():
                return
        except (AttributeError, OSError):
            pass


def dark_titlebar(window):
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id()) or window.winfo_id()
        val = ctypes.c_int(1)
        for attr in (20, 19):
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd), attr, ctypes.byref(val), ctypes.sizeof(val)
            )
    except Exception:
        pass


class CalibrateWindow(tk.Tk):
    def __init__(self, points=None):
        super().__init__()
        self.configure(bg=BG)
        dark_titlebar(self)
        try:
            self.iconbitmap(default=os.path.join(HERE, 'icon.ico'))
        except tk.TclError:
            pass
        self.only = points is not None
        sections = [(None, points)] if points else [
            ('Items (Auto SC + BR)', game.POINTS), ('Fishing', game.FISH_POINTS),
            ('Merchant collection', game.MERCHANT_POINTS), ('Skip intro', game.SKIP_POINTS),
            ('Daily rewards', game.DAILY_POINTS)]
        self.points = [p for _, group in sections for p in group]
        self.capturing = None
        self.banner = None
        self.title('Calibrate Buttons · Veyra')
        tall = 250 + 54 * len(self.points) if points else 560
        self.geometry('500x%d' % min(tall, self.winfo_screenheight() - 140))
        self.minsize(450, 320)
        self.protocol('WM_DELETE_WINDOW', self.finish)

        head = tk.Frame(self, bg=SIDEBAR_BG, highlightthickness=1, highlightbackground=EDGE)
        head.pack(fill='x')
        h_in = tk.Frame(head, bg=SIDEBAR_BG)
        h_in.pack(fill='x', padx=22, pady=14)
        tk.Label(h_in, text='Button Calibration', bg=SIDEBAR_BG, fg=TEXT, font=(UIB, 13)).pack(anchor='w')
        tk.Label(h_in, text='Veyra auto-scales all coordinates. Only override a button if a click misses on your screen.',
                 bg=SIDEBAR_BG, fg=SUBTEXT, font=(UI, 9), wraplength=440, justify='left', anchor='w').pack(fill='x', pady=(4, 0))

        bar = tk.Frame(self, bg=SIDEBAR_BG, highlightthickness=1, highlightbackground=EDGE)
        bar.pack(side='bottom', fill='x')
        bar_in = tk.Frame(bar, bg=SIDEBAR_BG)
        bar_in.pack(fill='x', padx=22, pady=12)

        canvas = tk.Canvas(self, bg=BG, highlightthickness=0, yscrollincrement=20)
        canvas.pack(side='left', fill='both', expand=True)
        scroll = tk.Frame(canvas, bg=BG)
        win = canvas.create_window((0, 0), window=scroll, anchor='nw')
        scroll.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda e: canvas.itemconfigure(win, width=e.width))
        self.bind_all('<MouseWheel>', lambda e: canvas.yview_scroll(int(-e.delta / 40), 'units'))

        tk.Frame(scroll, bg=BG, height=12).pack()
        self.rows, self.sections = {}, []
        for heading, group in sections:
            box = tk.Frame(scroll, bg=PANEL, highlightthickness=1, highlightbackground=EDGE)
            box.pack(fill='x', padx=20, pady=(0, 10))
            inner = tk.Frame(box, bg=PANEL)
            if heading:
                top = tk.Frame(box, bg=PANEL, cursor='hand2')
                top.pack(fill='x', padx=18, pady=12)
                arrow = tk.Label(top, text='\u25b8', bg=PANEL, fg=ACCENT, font=(UIB, 10), cursor='hand2')
                arrow.pack(side='left')
                tk.Label(top, text=heading, bg=PANEL, fg=TEXT, font=(UIB, 10), cursor='hand2').pack(side='left', padx=(8, 0))
                summary = tk.Label(top, text='', bg=ACCENT_BG, fg=ACCENT, font=(UIB, 8), padx=8, pady=2, cursor='hand2')
                summary.pack(side='right')
                toggle = lambda e=None, i=inner, a=arrow: self._fold(i, a)
                for w in (top, arrow, summary) + tuple(top.winfo_children()):
                    w.bind('<Button-1>', toggle)
                self.sections.append(([n for n, _ in group], summary))
            else:
                inner.pack(fill='x', pady=(6, 6))
            for i, (name, what) in enumerate(group):
                if i:
                    tk.Frame(inner, bg=EDGE, height=1).pack(fill='x', padx=18, pady=2)
                self.rows[name] = self._row(inner, name, what)

        self.done_b = tk.Button(bar_in, text='Done', command=self.finish, bg=ACCENT, fg=BG, activebackground=ACCENT_HOVER,
                                activeforeground=BG, relief='flat', font=(UIB, 10), pady=9, cursor='hand2', borderwidth=0)
        self.done_b.pack(fill='x')
        self._refresh()

    def _fold(self, inner, arrow):
        if inner.winfo_manager():
            inner.pack_forget()
            arrow.configure(text='\u25b8')
        else:
            inner.pack(fill='x', pady=(0, 8))
            arrow.configure(text='\u25be')

    def _row(self, parent, name, what):
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill='x', padx=18, pady=6)
        tick = tk.Label(row, text='', bg=SUNK, fg=GOOD, font=(UIB, 8), width=6, pady=2, anchor='center')
        tick.pack(side='left', padx=(0, 10))
        tk.Label(row, text=what, bg=PANEL, fg=TEXT, font=(UI, 9), anchor='w').pack(side='left', fill='x', expand=True)
        quiet = dict(bg=SUNK, fg=SUBTEXT, activebackground=PANEL_ALT, activeforeground=ACCENT, relief='flat', font=(UI, 9),
                     padx=10, pady=4, cursor='hand2', borderwidth=0, highlightthickness=1, highlightbackground=EDGE)
        b = tk.Button(row, text='Set', command=lambda: self.capture(name), padx=14, **SMALL_BUTTON)
        b.pack(side='right')
        test = tk.Button(row, text='Test', command=lambda: self.show(name), **quiet)
        test.pack(side='right', padx=(0, 6))
        reset = tk.Button(row, text='Reset', command=lambda: self.reset(name), **quiet) if name in game.BUILT_IN else None
        return tick, b, test, reset

    def show(self, name):
        at = game.point_at(name)
        if at is not None:
            game.glide(*at)

    def reset(self, name):
        game.forget_point(name)
        self._refresh()

    def _refresh(self):
        stored = game.load_points()
        for name, (tick, b, test, reset) in self.rows.items():
            done = name in stored
            tick.configure(text='CUSTOM' if done else 'AUTO' if name in game.BUILT_IN else 'UNSET',
                           fg=GOOD if done else (ACCENT if name in game.BUILT_IN else WARN),
                           bg=GOOD_BG if done else (ACCENT_BG if name in game.BUILT_IN else SUNK))
            b.configure(text='Redo' if done else 'Set')
            if reset is not None:
                if done and not reset.winfo_manager():
                    reset.pack(side='right', padx=(0, 6), before=test)
                elif not done and reset.winfo_manager():
                    reset.pack_forget()
        for names, summary in self.sections:
            n = sum(1 for name in names if name in stored)
            summary.configure(text='%d custom' % n if n else 'All Auto' if all(x in game.BUILT_IN for x in names)
                              else 'Not Set', fg=GOOD if n else ACCENT)
        left = [n for n, _ in self.points if n not in stored and n not in game.BUILT_IN]
        self.done_b.configure(text='Save & Close' if not left else 'Done  (%d still to set)' % len(left))

    def capture(self, name):
        if self.capturing:
            return
        self.capturing = name
        self.withdraw()
        self.banner = self._make_banner('Click "%s" in Roblox      [Esc to cancel]' % dict(self.points)[name])
        self._got = []

        def on_click(x, y, button, pressed):
            if pressed and button == mouse.Button.left:
                self._got.append((x, y))
                return False

        self._listener = mouse.Listener(on_click=on_click)
        self._listener.start()
        self.after(60, self._poll)

    def _make_banner(self, text):
        top = tk.Toplevel(self, bg=ACCENT)
        top.overrideredirect(True)
        top.attributes('-topmost', True)
        tk.Label(top, text=text, bg=ACCENT, fg=BG, font=(UIB, 12), padx=28, pady=12).pack()
        top.update_idletasks()
        top.geometry('+%d+%d' % (max(0, (top.winfo_screenwidth() - top.winfo_reqwidth()) // 2), 40))
        top.bind('<Escape>', lambda e: self._cancel())
        self.bind_all('<Escape>', lambda e: self._cancel())
        return top

    def _poll(self):
        if not self.capturing:
            return
        if self._got:
            try:
                game.save_point(self.capturing, game.to_fraction(*self._got[0]))
            except Exception:
                pass
            self._end_capture()
        elif not self._listener.running:
            self._end_capture()
        else:
            self.after(60, self._poll)

    def _cancel(self):
        if self.capturing:
            self._end_capture()

    def _end_capture(self):
        self.capturing = None
        try:
            self._listener.stop()
        except Exception:
            pass
        if self.banner is not None:
            self.banner.destroy()
            self.banner = None
        self.unbind_all('<Escape>')
        self.deiconify()
        self.lift()
        self._refresh()

    def finish(self):
        self._cancel()
        self.destroy()


# Toast fill, rim, and text for each Settings theme. Black & White stays monochrome.
_TOAST_THEMES = {
    'dark': ('#0c1424', '#38bdf8', '#f8fafc'),
    'white': ('#ffffff', '#0284c7', '#0f172a'),
    'midnight': ('#161228', '#c4b5fd', '#f5f3ff'),
    'ember': ('#2a140c', '#fb923c', '#fff7ed'),
    'forest': ('#0c2418', '#34d399', '#ecfdf5'),
    'bw': ('#000000', '#ffffff', '#ffffff'),
}


def _toast_look(colour):
    theme = 'dark'
    try:
        saved = (game.load_settings().get('theme') or 'dark')
        if saved in _TOAST_THEMES:
            theme = saved
    except Exception:
        pass
    fill, rim, fg = _TOAST_THEMES[theme]
    if theme != 'bw' and colour == BAD:
        rim, fg = BAD, '#fff1f2'
    elif theme != 'bw' and colour == WARN:
        rim = WARN
    return fill, rim, fg


def _clip_pill(hwnd, width, height):
    """Clip a borderless window into a capsule. The system owns the region after this."""
    radius = max(8, height)
    rgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, int(width) + 1, int(height) + 1, radius, radius)
    if rgn:
        ctypes.windll.user32.SetWindowRgn(hwnd, rgn, True)


class ToastService:
    """Dedicated daemon-thread click-through overlay toast over Roblox."""

    def __init__(self):
        self._q = queue.Queue()
        self._started = False
        self._init_lock = threading.Lock()
        self._banner_q = queue.Queue()
        self._banner_started = False
        self._banner_lock = threading.Lock()

    def show(self, text, colour=TEXT, ms=2200):
        if not self._started:
            with self._init_lock:
                if not self._started:
                    self._started = True
                    threading.Thread(target=self._run, daemon=True).start()
        self._q.put((text, colour, ms))

    def banner(self, kind, ms=2000):
        """Veyra icon + STARTED (green) / STOPPED (red), topmost for `ms` then hidden."""
        if not self._banner_started:
            with self._banner_lock:
                if not self._banner_started:
                    self._banner_started = True
                    threading.Thread(target=self._banner_run, daemon=True).start()
        self._banner_q.put((kind, ms))

    def _banner_run(self):
        """Big, centered, click-through pill with the Veyra compass and a status word."""
        from PIL import Image, ImageTk

        dpi_aware()
        u32 = ctypes.windll.user32
        place = ctypes.WinDLL('user32').SetWindowPos
        place.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        TOPMOST = ctypes.c_void_p(-1 & 0xFFFFFFFFFFFFFFFF)

        root = tk.Tk()
        root.withdraw()
        top = tk.Toplevel(root, bg='#0b1220')
        top.overrideredirect(True)
        top.attributes('-topmost', True)

        big = tkfont.Font(root, family=UIB, size=19, weight='bold')
        sub = tkfont.Font(root, family=UIB, size=9)

        frame = tk.Frame(top, bg='#0b1220', padx=26, pady=14)
        frame.pack()
        icon_lbl = tk.Label(frame, bg='#0b1220')
        icon_lbl.grid(row=0, column=0, padx=(0, 13), sticky='ns')
        text_lbl = tk.Label(frame, bg='#0b1220', fg='#f8fafc', font=big, anchor='w')
        text_lbl.grid(row=0, column=1, sticky='w')
        sub_lbl = tk.Label(frame, bg='#0b1220', fg='#94a3b8', font=sub, anchor='w')
        sub_lbl.grid(row=1, column=1, sticky='w', pady=(4, 0))

        # Veyra compass icon, with pre-rendered spin frames
        spin, spin_i = [], [0]
        try:
            path = os.path.join(HERE, 'ui', 'brand_bh.png')
            img = Image.open(path).convert('RGBA')
            img.thumbnail((40, 40), Image.LANCZOS)
            spin = [ImageTk.PhotoImage(img.rotate(a, resample=Image.BICUBIC))
                    for a in range(0, 360, 30)]
            icon_lbl.configure(image=spin[0])
        except Exception:
            pass

        def animate():
            if spin and shown[0]:
                spin_i[0] = (spin_i[0] + 1) % len(spin)
                icon_lbl.configure(image=spin[spin_i[0]])
            root.after(80, animate)

        top.withdraw()
        top.update_idletasks()
        hwnd = u32.GetParent(top.winfo_id()) or top.winfo_id()
        ex = u32.GetWindowLongW(hwnd, -20)
        u32.SetWindowLongW(hwnd, -20, ex | 0x08000000 | 0x80 | 0x20)  # click-through (no layered: it hides the window)

        kinds = {
            'start': ('MACRO STARTED', '#4ade80', 'Veyra is running'),
            'stop': ('MACRO STOPPED', '#f87171', 'Veyra has stopped'),
        }
        job = [None]
        shown = [False]

        def hide():
            job[0] = None
            shown[0] = False
            u32.ShowWindow(hwnd, 0)

        def poll():
            try:
                while True:
                    kind, ms = self._banner_q.get_nowait()
                    word, color, note_text = kinds.get(kind, kinds['start'])
                    frame.configure(bg='#0b1220')
                    for lbl in (icon_lbl, text_lbl, sub_lbl):
                        lbl.configure(bg='#0b1220')
                    text_lbl.configure(text=word, fg=color)
                    sub_lbl.configure(text=note_text)
                    top.update_idletasks()
                    w, h = top.winfo_reqwidth(), top.winfo_reqheight()
                    _clip_pill(hwnd, w, h)
                    x, y, cw, ch = 0, 0, top.winfo_screenwidth(), top.winfo_screenheight()
                    try:
                        win = game.find_roblox_window()
                        if win is not None:
                            x, y, cw, ch = game.client_rect(win)
                    except Exception:
                        pass
                    # top-center over the game
                    place(hwnd, TOPMOST, x + max(0, (cw - w) // 2), y + max(18, ch // 14),
                          w, h, 0x0010 | 0x0040)
                    u32.ShowWindow(hwnd, 5)
                    top.lift()
                    top.attributes('-topmost', True)
                    shown[0] = True
                    if job[0] is not None:
                        top.after_cancel(job[0])
                    job[0] = top.after(ms, hide)
            except queue.Empty:
                pass
            root.after(40, poll)

        if spin:
            root.after(80, animate)
        root.after(40, poll)
        root.mainloop()

    def _run(self):
        dpi_aware()
        u32 = ctypes.windll.user32
        place = ctypes.WinDLL('user32').SetWindowPos
        place.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                          ctypes.c_uint]
        TOPMOST = ctypes.c_void_p(-1 & 0xFFFFFFFFFFFFFFFF)

        root = tk.Tk()
        root.withdraw()
        font = tkfont.Font(root, family=UIB, size=12)
        top = tk.Toplevel(root, bg='#0c1424')
        top.overrideredirect(True)
        top.attributes('-topmost', True)
        label = tk.Label(top, bg='#0c1424', fg='#f8fafc', font=font, padx=26, pady=11,
                         wraplength=640, justify='center')
        label.pack()
        top.withdraw()
        top.update_idletasks()
        hwnd = u32.GetParent(top.winfo_id()) or top.winfo_id()
        ex = u32.GetWindowLongW(hwnd, -20)
        u32.SetWindowLongW(hwnd, -20, ex | 0x08000000 | 0x80 | 0x20)
        job = [None]

        def hide():
            job[0] = None
            u32.ShowWindow(hwnd, 0)

        def poll():
            try:
                while True:
                    text, colour, ms = self._q.get_nowait()
                    fill, rim, fg = _toast_look(colour)
                    top.configure(bg=fill)
                    label.configure(text=text, bg=fill, fg=rim if colour in (GOOD, ACCENT) else fg)
                    top.update_idletasks()
                    w, h = top.winfo_reqwidth(), top.winfo_reqheight()
                    _clip_pill(hwnd, w, h)
                    x, y, cw = 0, 0, top.winfo_screenwidth()
                    try:
                        win = game.find_roblox_window()
                        if win is not None:
                            x, y, cw, _ = game.client_rect(win)
                    except Exception:
                        pass
                    place(hwnd, TOPMOST, x + max(0, (cw - w) // 2), y + 48, w, h, 0x0010 | 0x0040)
                    if job[0] is not None:
                        top.after_cancel(job[0])
                    job[0] = top.after(ms, hide)
            except queue.Empty:
                pass
            root.after(40, poll)

        root.after(40, poll)
        root.mainloop()


def run(mode='all'):
    dpi_aware()
    if game.find_roblox_window() is None:
        r = tk.Tk()
        r.withdraw()
        dark_titlebar(r)
        messagebox.showinfo('Veyra', 'Open Roblox first, then calibrate.')
        return
    if mode == 'play':
        pts = game.PLAY_POINT
    elif mode == 'contract':
        pts = eden.POINTS
    else:
        pts = None
    CalibrateWindow(pts).mainloop()


if __name__ == '__main__':
    run(sys.argv[1] if len(sys.argv) > 1 else 'all')

"""The control window — a small Tk "пульт", not the app itself.

It exists so the EXE has something to show and somewhere to close from. The
real interface is the browser tab. Closing the tab must not stop anything;
closing this window shuts the backend down.
"""
from __future__ import annotations

import tkinter as tk
import webbrowser
from tkinter import ttk

from . import config
from .logging import LOG

BG = "#1b1b1a"
SURFACE = "#232322"
BORDER = "#3a3936"
TEXT = "#eceae5"
MUTED = "#a3a09a"
ACCENT = "#d97757"
ACCENT_HOVER = "#e08a6d"
OK = "#5c9e6a"

W, H = 380, 250


class ControlPanel:
    def __init__(self, web, services):
        self.web = web
        self.services = services
        self.root = tk.Tk()
        self.root.title(config.APP_TITLE)
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._quit)
        self._build()
        self._center()

    # ── layout ──────────────────────────────────────────────────────────
    def _build(self) -> None:
        pad = tk.Frame(self.root, bg=BG, padx=22, pady=20)
        pad.pack(fill="both", expand=True)

        tk.Label(pad, text=config.APP_TITLE, bg=BG, fg=TEXT,
                 font=("Segoe UI", 15, "bold")).pack(anchor="w")
        tk.Label(pad, text=f"версия {config.APP_VERSION}", bg=BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 14))

        status = tk.Frame(pad, bg=BG)
        status.pack(fill="x", pady=(0, 4))
        self._dot(status)
        tk.Label(status, text="сервер работает", bg=BG, fg=OK,
                 font=("Segoe UI", 10)).pack(side="left", padx=(7, 0))

        url_box = tk.Frame(pad, bg=SURFACE, highlightbackground=BORDER,
                           highlightthickness=1)
        url_box.pack(fill="x", pady=(10, 4))
        entry = tk.Entry(url_box, bg=SURFACE, fg=MUTED, relief="flat",
                         font=("Consolas", 9), readonlybackground=SURFACE,
                         highlightthickness=0, bd=0)
        entry.insert(0, self.web.url)
        entry.configure(state="readonly")
        entry.pack(fill="x", padx=9, pady=7)
        self._entry = entry

        tk.Label(pad, text="Адрес открывается только на этом компьютере.",
                 bg=BG, fg=MUTED, font=("Segoe UI", 8)).pack(anchor="w")

        buttons = tk.Frame(pad, bg=BG)
        buttons.pack(fill="x", pady=(16, 0))
        self._button(buttons, "Открыть", self._open, accent=True).pack(side="left")
        self._button(buttons, "Копировать адрес", self._copy).pack(side="left", padx=8)
        self._button(buttons, "Завершить", self._quit).pack(side="right")

    def _dot(self, parent) -> None:
        canvas = tk.Canvas(parent, width=10, height=10, bg=BG, highlightthickness=0,
                           bd=0)
        canvas.create_oval(1, 1, 9, 9, fill=OK, outline="")
        canvas.pack(side="left", padx=(0, 0), pady=(2, 0))

    def _button(self, parent, text, command, accent=False):
        btn = tk.Button(
            parent, text=text, command=command, relief="flat", bd=0, cursor="hand2",
            font=("Segoe UI", 9), padx=13, pady=6,
            bg=ACCENT if accent else SURFACE,
            fg="#1b1b1a" if accent else TEXT,
            activebackground=ACCENT_HOVER if accent else BORDER,
            activeforeground="#1b1b1a" if accent else TEXT,
            highlightthickness=0)
        return btn

    def _center(self) -> None:
        self.root.withdraw()
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - W) // 2
        y = (self.root.winfo_screenheight() - H) // 3
        self.root.geometry(f"{W}x{H}+{x}+{y}")
        self.root.deiconify()

    # ── actions ─────────────────────────────────────────────────────────
    def _open(self) -> None:
        webbrowser.open(self.web.url)

    def _copy(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.web.url)
        LOG.info("URL copied to clipboard", module="panel")

    def _quit(self) -> None:
        self.root.quit()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


_ = ttk  # imported for consistent theming on some Windows builds

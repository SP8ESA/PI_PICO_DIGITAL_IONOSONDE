#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GUI for the automatic ionosonde (ionosonde_auto.py).

Set the sounding parameters, press Start, and the loop runs on its own:
Pico TX -> RTL-SDR recording -> correlation analysis -> the range plot shows up
in the Results tab. Every finished cycle is added to the history list, click any
entry to bring its plot back.

    python3 ionosonde_auto_gui.py
"""

import contextlib
import io
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import ttk, filedialog, messagebox, scrolledtext

import numpy as np

import ionosonde_auto as ia

try:
    from PIL import Image, ImageTk          # nicer scaling when available
except ImportError:
    Image = ImageTk = None

HERE = os.path.dirname(os.path.abspath(__file__))
MOD_VALUES = ['CARRIER', 'BARKER2', 'BARKER3', 'BARKER4', 'BARKER5',
              'BARKER7', 'BARKER11', 'BARKER13']


class AutoGUI:
    def __init__(self, root):
        self.root = root
        root.title("BPSK Ionosonde - Automatic Sounding")
        root.geometry("1400x820")
        root.minsize(900, 480)          # parameters scroll, so small screens are fine

        self.worker = None
        self.stop_evt = threading.Event()
        self.events = queue.Queue()
        self.history = []          # list of result dicts
        self._photo = None         # keep a reference or Tk drops the image

        self.vars = {
            'freqs': tk.StringVar(value="3.822"),
            'offset_khz': tk.DoubleVar(value=400.0),
            'bit_us': tk.DoubleVar(value=40.0),
            'amp': tk.IntVar(value=90),
            'mod': tk.StringVar(value="BARKER13"),
            'chips': tk.IntVar(value=2048),
            'range_km': tk.DoubleVar(value=780.0),
            'rate': tk.DoubleVar(value=2048000),
            'direct_samp': tk.IntVar(value=2),
            'ppm': tk.DoubleVar(value=0.0),
            'iq_sense': tk.StringVar(value="normal"),
            'digital_agc': tk.BooleanVar(value=True),
            'settle_ms': tk.DoubleVar(value=300),
            'lead_ms': tk.DoubleVar(value=50),
            'tail_ms': tk.DoubleVar(value=300),
            'cycles': tk.IntVar(value=0),
            'period': tk.DoubleVar(value=0),
            'out_dir': tk.StringVar(value=os.path.join(HERE, "captures")),
            'port': tk.StringVar(value=""),
            'enable_tx': tk.BooleanVar(value=True),
            'enable_rx': tk.BooleanVar(value=True),
            'keep_wav': tk.BooleanVar(value=False),
            'no_analyze': tk.BooleanVar(value=False),
            'km_min': tk.DoubleVar(value=100),
            'km_max': tk.DoubleVar(value=650),
            'coh_batch': tk.IntVar(value=256),
            'ion_start': tk.DoubleVar(value=2.0),
            'ion_stop': tk.DoubleVar(value=10.0),
            'ion_step_khz': tk.DoubleVar(value=100.0),
            'ion_repeat': tk.IntVar(value=1),
            'ion_period': tk.DoubleVar(value=0.0),
        }

        self._build()
        self.root.after(100, self._pump)

    # ---------------------------------------------------------- layout

    def _build(self):
        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(outer)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))
        right = ttk.Frame(outer)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Controls are pinned to the bottom, the parameters scroll above them -
        # the panel is taller than a laptop screen once every section is open.
        ctrl = ttk.Frame(left)
        ctrl.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))
        self._build_controls(ctrl)

        area = ttk.Frame(left)
        area.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.pcanvas = tk.Canvas(area, highlightthickness=0, borderwidth=0)
        vs = ttk.Scrollbar(area, orient=tk.VERTICAL, command=self.pcanvas.yview)
        self.pcanvas.configure(yscrollcommand=vs.set)
        vs.pack(side=tk.RIGHT, fill=tk.Y)
        self.pcanvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        params = ttk.Frame(self.pcanvas)
        self.pcanvas.create_window((0, 0), window=params, anchor="nw")
        params.bind("<Configure>", self._on_params_resize)
        for w in (self.pcanvas, params):
            w.bind("<Enter>", lambda e: self._wheel_bind(True))
            w.bind("<Leave>", lambda e: self._wheel_bind(False))

        self._build_params(params)
        self._build_results(right)

        bottom = ttk.Frame(self.root)
        bottom.pack(fill=tk.X, side=tk.BOTTOM)

        # Progress row: known-duration work (a frame being transmitted, a wait)
        # runs the bar against the clock; a sweep drives it by steps.
        # Two bars: the top one is the job running right now (a frame going out,
        # a wait), the bottom one is where we are in the whole series - a sweep of
        # soundings, or a folder being analysed.
        bar_row = ttk.Frame(bottom, padding=(4, 2))
        bar_row.pack(fill=tk.X)
        self.task_lbl = ttk.Label(bar_row, text="", width=34, anchor=tk.W)
        self.task_lbl.pack(side=tk.LEFT)
        self.pb = ttk.Progressbar(bar_row, mode="determinate", maximum=1000)
        self.pb.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        self.pb_lbl = ttk.Label(bar_row, text="", width=14, anchor=tk.E)
        self.pb_lbl.pack(side=tk.LEFT)

        ser_row = ttk.Frame(bottom, padding=(4, 0))
        ser_row.pack(fill=tk.X)
        self.series_lbl = ttk.Label(ser_row, text="", width=34, anchor=tk.W)
        self.series_lbl.pack(side=tk.LEFT)
        self.pb2 = ttk.Progressbar(ser_row, mode="determinate", maximum=1000)
        self.pb2.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        self.pb2_lbl = ttk.Label(ser_row, text="", width=14, anchor=tk.E)
        self.pb2_lbl.pack(side=tk.LEFT)

        self.status = ttk.Label(bottom, text="Idle", relief=tk.SUNKEN,
                                anchor=tk.W, padding=4)
        self.status.pack(fill=tk.X)

        self._task = None          # (t0, seconds, text) for a timed operation
        self._sweep = None         # (done, total, t0) for a stepped operation
        self._pb_busy = False
        self.root.after(100, self._tick_progress)

    def _on_params_resize(self, _evt):
        """Keep the scroll region and the column width matched to the content."""
        self.pcanvas.configure(scrollregion=self.pcanvas.bbox("all"))
        kids = self.pcanvas.winfo_children()
        if kids:                                    # column as wide as its content
            self.pcanvas.configure(width=kids[0].winfo_reqwidth())

    def _wheel_bind(self, on):
        if on:
            self.pcanvas.bind_all("<MouseWheel>", self._on_wheel)
            self.pcanvas.bind_all("<Button-4>", self._on_wheel)
            self.pcanvas.bind_all("<Button-5>", self._on_wheel)
        else:
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                self.pcanvas.unbind_all(seq)

    def _on_wheel(self, evt):
        if getattr(evt, "num", None) == 4 or getattr(evt, "delta", 0) > 0:
            self.pcanvas.yview_scroll(-2, "units")
        elif getattr(evt, "num", None) == 5 or getattr(evt, "delta", 0) < 0:
            self.pcanvas.yview_scroll(2, "units")

    def _row(self, parent, r, label, widget, hint=None):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky=tk.W, pady=2)
        widget.grid(row=r, column=1, sticky=tk.W, padx=4, pady=2)
        if hint:
            ttk.Label(parent, text=hint, foreground="gray",
                      font=('Helvetica', 8)).grid(row=r, column=2, sticky=tk.W)

    def _group(self, parent, r, title):
        """A heading inside the single settings panel."""
        pad = (10, 2) if r else (0, 2)
        ttk.Label(parent, text=title, font=('Helvetica', 9, 'bold')).grid(
            row=r, column=0, columnspan=3, sticky=tk.W, pady=pad)
        return r + 1

    def _build_params(self, parent):
        v = self.vars
        s = ttk.LabelFrame(parent, text="Settings", padding=8)
        s.pack(fill=tk.X, pady=(0, 6))
        r = 0

        r = self._group(s, r, "Transmitter")
        self._row(s, r, "Frequencies [MHz]:",
                  ttk.Entry(s, textvariable=v['freqs'], width=16)); r += 1
        self._row(s, r, "Modulation:",
                  ttk.Combobox(s, textvariable=v['mod'], values=MOD_VALUES,
                               state="readonly", width=12)); r += 1
        self._row(s, r, "Bit duration [us]:",
                  ttk.Spinbox(s, from_=10, to=500, increment=5,
                              textvariable=v['bit_us'], width=14)); r += 1
        self._row(s, r, "Chip count:",
                  ttk.Spinbox(s, from_=1, to=20000, increment=128,
                              textvariable=v['chips'], width=14)); r += 1
        self._row(s, r, "Amplitude [%]:",
                  ttk.Spinbox(s, from_=0, to=100, increment=5,
                              textvariable=v['amp'], width=14)); r += 1
        self._row(s, r, "Radar range [km]:",
                  ttk.Spinbox(s, from_=150, to=5000, increment=10,
                              textvariable=v['range_km'], width=14)); r += 1
        self.frame_lbl = ttk.Label(s, text="", foreground="#006600",
                                   font=('Consolas', 8))
        self.frame_lbl.grid(row=r, column=0, columnspan=3, sticky=tk.W); r += 1

        r = self._group(s, r, "Receiver")
        self._row(s, r, "Tune below carrier [kHz]:",
                  ttk.Spinbox(s, from_=5, to=1000, increment=5,
                              textvariable=v['offset_khz'], width=14)); r += 1
        self._row(s, r, "Sample rate [S/s]:",
                  ttk.Combobox(s, textvariable=v['rate'], width=12,
                               values=[240000, 250000, 300000, 960000, 1024000,
                                       1200000, 2048000, 2400000])); r += 1
        self._row(s, r, "Direct sampling:",
                  ttk.Combobox(s, textvariable=v['direct_samp'], state="readonly",
                               values=[0, 1, 2], width=12)); r += 1
        self._row(s, r, "Frequency corr. [ppm]:",
                  ttk.Spinbox(s, from_=-200, to=200, increment=1,
                              textvariable=v['ppm'], width=14)); r += 1
        self._row(s, r, "I/Q spectrum:",
                  ttk.Combobox(s, textvariable=v['iq_sense'], state="readonly",
                               values=["normal", "auto", "invert"], width=12)); r += 1
        self._row(s, r, "Settle / lead / tail [ms]:",
                  self._triple(s, v['settle_ms'], v['lead_ms'], v['tail_ms'])); r += 1
        ttk.Checkbutton(s, text="Digital AGC (RTL2832)",
                        variable=v['digital_agc']).grid(row=r, column=0, columnspan=3,
                                                        sticky=tk.W); r += 1

        r = self._group(s, r, "Analysis")
        self._row(s, r, "Coherent batch:",
                  ttk.Spinbox(s, from_=1, to=1024, increment=8,
                              textvariable=v['coh_batch'], width=14)); r += 1
        self._row(s, r, "Height axis [km]:",
                  self._pair(s, v['km_min'], v['km_max'])); r += 1

        r = self._group(s, r, "Run")
        self._row(s, r, "Cycles:",
                  ttk.Spinbox(s, from_=0, to=100000, increment=1,
                              textvariable=v['cycles'], width=14),
                  "0 = forever"); r += 1
        self._row(s, r, "Period [s]:",
                  ttk.Spinbox(s, from_=0, to=3600, increment=10,
                              textvariable=v['period'], width=14),
                  "0 = back to back"); r += 1
        self._row(s, r, "Serial port:",
                  ttk.Entry(s, textvariable=v['port'], width=16)); r += 1
        d = ttk.Frame(s)
        ttk.Entry(d, textvariable=v['out_dir'], width=22).pack(side=tk.LEFT)
        ttk.Button(d, text="...", width=3, command=self._pick_dir).pack(side=tk.LEFT)
        self._row(s, r, "Output folder:", d); r += 1
        en = ttk.Frame(s)
        en.grid(row=r, column=0, columnspan=3, sticky=tk.W, pady=(2, 0)); r += 1
        ttk.Checkbutton(en, text="Enable TX", variable=v['enable_tx'],
                        command=self._update_enables).pack(side=tk.LEFT)
        ttk.Checkbutton(en, text="Enable RX", variable=v['enable_rx'],
                        command=self._update_enables).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Checkbutton(en, text="Keep WAV",
                        variable=v['keep_wav']).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Checkbutton(en, text="No analysis",
                        variable=v['no_analyze']).pack(side=tk.LEFT, padx=(12, 0))
        self.enable_lbl = ttk.Label(s, text="", foreground="gray",
                                    font=('Helvetica', 8))
        self.enable_lbl.grid(row=r, column=0, columnspan=3, sticky=tk.W); r += 1
        self._update_enables()

        r = self._group(s, r, "Ionogram sweep")
        self._row(s, r, "From / to [MHz]:",
                  self._pair_mhz(s, v['ion_start'], v['ion_stop'])); r += 1
        self._row(s, r, "Step [kHz]:",
                  ttk.Spinbox(s, from_=10, to=2000, increment=10,
                              textvariable=v['ion_step_khz'], width=14)); r += 1
        self._row(s, r, "Repeat:",
                  ttk.Spinbox(s, from_=0, to=1000, increment=1,
                              textvariable=v['ion_repeat'], width=14),
                  "0 = forever"); r += 1
        self._row(s, r, "Every [min]:",
                  ttk.Spinbox(s, from_=0, to=1440, increment=5,
                              textvariable=v['ion_period'], width=14),
                  "0 = back to back"); r += 1
        self.ion_lbl = ttk.Label(s, text="", foreground="#006600",
                                 font=('Consolas', 8))
        self.ion_lbl.grid(row=r, column=0, columnspan=3, sticky=tk.W)

        for k in ('chips', 'range_km', 'amp', 'bit_us', 'mod'):
            v[k].trace('w', lambda *a: self._update_frame_label())
        for k in ('ion_start', 'ion_stop', 'ion_step_khz', 'chips', 'range_km',
                  'rate', 'ion_repeat'):
            v[k].trace('w', lambda *a: self._update_ion_label())
        self._update_frame_label()
        self._update_ion_label()

    def _pair_mhz(self, parent, a, b):
        f = ttk.Frame(parent)
        ttk.Spinbox(f, from_=0.5, to=30, increment=0.1, textvariable=a,
                    width=6).pack(side=tk.LEFT)
        ttk.Label(f, text=" - ").pack(side=tk.LEFT)
        ttk.Spinbox(f, from_=0.5, to=30, increment=0.1, textvariable=b,
                    width=6).pack(side=tk.LEFT)
        return f

    def _pair(self, parent, a, b):
        f = ttk.Frame(parent)
        ttk.Spinbox(f, from_=0, to=5000, increment=50, textvariable=a, width=6).pack(side=tk.LEFT)
        ttk.Label(f, text=" - ").pack(side=tk.LEFT)
        ttk.Spinbox(f, from_=0, to=5000, increment=50, textvariable=b, width=6).pack(side=tk.LEFT)
        return f

    def _triple(self, parent, a, b, c):
        f = ttk.Frame(parent)
        for var in (a, b, c):
            ttk.Spinbox(f, from_=0, to=5000, increment=50,
                        textvariable=var, width=5).pack(side=tk.LEFT, padx=(0, 2))
        return f

    def _build_controls(self, parent):
        box = ttk.Frame(parent)
        box.pack(fill=tk.X, pady=(4, 0))
        self.start_btn = ttk.Button(box, text="Start", command=self.start, width=12)
        self.start_btn.pack(side=tk.LEFT, padx=2)
        self.stop_btn = ttk.Button(box, text="Stop", command=self.stop,
                                   width=12, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=2)
        box2 = ttk.Frame(parent)
        box2.pack(fill=tk.X, pady=4)
        ttk.Button(box2, text="Single cycle", command=lambda: self.start(single=True),
                   width=12).pack(side=tk.LEFT, padx=2)
        ttk.Button(box2, text="Run ionogram", command=self.run_ionogram,
                   width=12).pack(side=tk.LEFT, padx=2)
        ttk.Button(parent, text="Open output folder",
                   command=self._open_dir).pack(fill=tk.X, pady=(2, 0))

    def _build_results(self, parent):
        nb = ttk.Notebook(parent)
        nb.pack(fill=tk.BOTH, expand=True)
        self.nb = nb

        res = ttk.Frame(nb, padding=4)
        nb.add(res, text="Results")
        top = ttk.Frame(res)
        top.pack(fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(top, background="#f4f4f4", highlightthickness=0)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", lambda e: self._redraw())

        side = ttk.Frame(top, width=190)
        side.pack(side=tk.LEFT, fill=tk.Y, padx=(6, 0))
        side.pack_propagate(False)
        ttk.Label(side, text="History (UTC)").pack(anchor=tk.W)
        self.hist = tk.Listbox(side, font=('Consolas', 9))
        self.hist.pack(fill=tk.BOTH, expand=True)
        self.hist.bind("<<ListboxSelect>>", self._on_pick)

        # Colour range for the ionogram - redraws from the saved matrix, so it
        # never re-runs the analysis.
        self.scale_box = ttk.LabelFrame(res, text="Ionogram colour range [dB]",
                                        padding=6)
        self.scale_box.pack(fill=tk.X, pady=(6, 0))
        self.vmin_var = tk.DoubleVar(value=0.0)
        self.vmax_var = tk.DoubleVar(value=10.0)
        row = ttk.Frame(self.scale_box)
        row.pack(fill=tk.X)
        ttk.Label(row, text="floor", width=6).pack(side=tk.LEFT)
        self.vmin_scale = ttk.Scale(row, from_=-10, to=60, variable=self.vmin_var,
                                    orient=tk.HORIZONTAL, command=self._on_scale)
        self.vmin_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        self.vmin_lbl = ttk.Label(row, text="0.0", width=6)
        self.vmin_lbl.pack(side=tk.LEFT)
        row2 = ttk.Frame(self.scale_box)
        row2.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(row2, text="ceiling", width=6).pack(side=tk.LEFT)
        self.vmax_scale = ttk.Scale(row2, from_=-10, to=60, variable=self.vmax_var,
                                    orient=tk.HORIZONTAL, command=self._on_scale)
        self.vmax_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        self.vmax_lbl = ttk.Label(row2, text="10.0", width=6)
        self.vmax_lbl.pack(side=tk.LEFT)
        ttk.Button(self.scale_box, text="Auto", width=8,
                   command=self._auto_scale).pack(side=tk.LEFT, pady=(4, 0))
        self._scale_job = None
        self._set_scale_state(False)

        self.metrics = ttk.Label(res, text="No result yet", font=('Consolas', 9),
                                 anchor=tk.W, justify=tk.LEFT)
        self.metrics.pack(fill=tk.X, pady=(4, 0))

        logf = ttk.Frame(nb, padding=4)
        nb.add(logf, text="Log")
        self.log_text = scrolledtext.ScrolledText(logf, font=('Consolas', 9),
                                                  state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        ttk.Button(logf, text="Clear", command=self._clear_log).pack(anchor=tk.W, pady=4)

    # ---------------------------------------------------------- helpers

    def _update_enables(self):
        """Say plainly what the two switches mean together."""
        tx = self.vars['enable_tx'].get()
        rx = self.vars['enable_rx'].get()
        text = {(True, True): "",                       # the normal case says nothing
                (True, False): "TX only - SDR left free",
                (False, True): "RX only - Pico not keyed",
                (False, False): "nothing enabled"}[(tx, rx)]
        self.enable_lbl.config(text=text)

    def _update_frame_label(self):
        """Frame length plus what it costs in RF: peak, energy, mean power."""
        try:
            v = self.vars
            cfg = self._budget_cfg()
            b = ia.tx_budget(cfg)
            self.frame_lbl.config(
                text=(f"S2S {cfg.s2s} us | frame {b['frame_s']:.2f} s | "
                      f"duty {b['duty']*100:.1f} %\n"
                      f"peak {b['peak_w']:.2f} W | avg {b['avg_w']*1000:.0f} mW | "
                      f"{b['energy_j']:.2f} J/frame"))
        except Exception:
            self.frame_lbl.config(text="")

    def _budget_cfg(self):
        """Minimal object with what tx_budget() and frame_seconds() need."""
        v = self.vars
        c = type("C", (), {})()
        c.amp = float(v['amp'].get())
        c.bit_us = float(v['bit_us'].get())
        c.mod = v['mod'].get()
        c.chips = int(v['chips'].get())
        c.chip_overhead_us = 250.0
        c.s2s = int(round(ia.s2s_from_range(float(v['range_km'].get()),
                                            c.chip_overhead_us)))
        return c

    def _update_ion_label(self):
        """Show what the sweep will cost before the user starts it."""
        try:
            v = self.vars
            n = len(ia.sweep_plan(v['ion_start'].get() * 1e6, v['ion_stop'].get() * 1e6,
                                  v['ion_step_khz'].get() * 1e3))
            frame = ia.frame_seconds(self._budget_cfg())
            mins = n * (frame + 1.5) / 60.0
            gb = n * 4 * v['rate'].get() * (frame + 0.4) / 1e9
            rep = v['ion_repeat'].get()
            series = (f" | x{rep} = ~{mins*rep/60:.1f} h" if rep > 1 else
                      " | forever" if rep == 0 else "")
            self.ion_lbl.config(text=f"{n} steps | ~{mins:.0f} min | ~{gb:.1f} GB"
                                     f"{series}")
        except (tk.TclError, ValueError, SystemExit, ZeroDivisionError):
            self.ion_lbl.config(text="")

    def _pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.vars['out_dir'].get())
        if d:
            self.vars['out_dir'].set(d)

    def _open_dir(self):
        d = self.vars['out_dir'].get()
        os.makedirs(d, exist_ok=True)
        subprocess.Popen(["xdg-open", d])

    def log(self, msg):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _clear_log(self):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _build_cfg(self, single=False, ionogram=False):
        """Reuse the CLI parser so GUI and command line cannot drift apart."""
        v = self.vars
        argv = ["ionosonde_auto.py",
                "--freqs", v['freqs'].get(),
                "--offset-khz", str(v['offset_khz'].get()),
                "--bit-us", str(v['bit_us'].get()),
                "--amp", str(v['amp'].get()),
                "--mod", v['mod'].get(),
                "--chips", str(v['chips'].get()),
                "--range-km", str(v['range_km'].get()),
                "--rate", str(v['rate'].get()),
                "--direct-samp", str(v['direct_samp'].get()),
                "--ppm", str(v['ppm'].get()),
                "--iq-sense", v['iq_sense'].get(),
                "--settle-ms", str(v['settle_ms'].get()),
                "--lead-ms", str(v['lead_ms'].get()),
                "--tail-ms", str(v['tail_ms'].get()),
                "--cycles", "1" if single else str(v['cycles'].get()),
                "--period", str(v['period'].get()),
                "--out-dir", v['out_dir'].get(),
                "--analyze-args",
                f"--coh_batch {v['coh_batch'].get()} "
                f"--km_min {v['km_min'].get()} --km_max {v['km_max'].get()}",
                "--ion-start", str(v['ion_start'].get()),
                "--ion-stop", str(v['ion_stop'].get()),
                "--ion-step-khz", str(v['ion_step_khz'].get()),
                "--ion-repeat", str(v['ion_repeat'].get()),
                "--ion-period", str(v['ion_period'].get()),
                "--ion-km-min", str(v['km_min'].get()),
                "--ion-km-max", str(v['km_max'].get())]
        if v['port'].get().strip():
            argv += ["--port", v['port'].get().strip()]
        if not v['enable_tx'].get():
            argv += ["--no-tx"]
        if not v['enable_rx'].get():
            argv += ["--no-rx"]
        if v['keep_wav'].get():
            argv += ["--keep-wav"]
        if v['no_analyze'].get():
            argv += ["--no-analyze"]
        # the CLI defaults this ON, so an unchecked box must say so explicitly
        argv += ["--digital-agc" if v['digital_agc'].get() else "--no-digital-agc"]
        if ionogram:
            argv += ["--ionogram"]
        old, err = sys.argv, io.StringIO()
        try:
            sys.argv = argv
            with contextlib.redirect_stderr(err):
                return ia.parse_args()
        except SystemExit:
            # argparse writes the reason to stderr and exits - turn it into a
            # message the dialog can actually show
            msg = err.getvalue().strip().splitlines()
            raise ValueError(msg[-1] if msg else "invalid parameters")
        finally:
            sys.argv = old

    # ---------------------------------------------------------- run control

    def start(self, single=False, ionogram=False):
        if self.worker and self.worker.is_alive():
            return
        try:
            cfg = self._build_cfg(single=single, ionogram=ionogram)
        except (ValueError, tk.TclError) as e:     # bad or non-numeric field
            messagebox.showerror("Bad parameters", str(e))
            return
        if ionogram:
            n = len(ia.sweep_plan(cfg.ion_start * 1e6, cfg.ion_stop * 1e6,
                                  cfg.ion_step_khz * 1e3))
            if not messagebox.askokcancel(
                    "Run ionogram",
                    f"{n} soundings from {cfg.ion_start:.3f} to {cfg.ion_stop:.3f} MHz, "
                    f"{'repeating forever' if cfg.ion_repeat == 0 else f'{cfg.ion_repeat} time(s)'}.\n\n"
                    f"{self.ion_lbl.cget('text')}\n\n"
                    "Recording runs first, analysis afterwards. Start?"):
                return
        self.stop_evt.clear()
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.nb.select(0)
        mode = ("ionogram" if ionogram else "txonly" if cfg.tx_only else "loop")
        self.worker = threading.Thread(target=self._run, args=(cfg, mode), daemon=True)
        self.worker.start()

    def run_ionogram(self):
        self.start(ionogram=True)

    def stop(self):
        self.stop_evt.set()
        self.stop_btn.config(state=tk.DISABLED)
        self.events.put(("status", "Stopping after the current cycle..."))

    def _run(self, cfg, mode):
        """Worker thread: drives ionosonde_auto directly, no subprocess."""
        put = self.events.put
        ia.log = lambda m: put(("log", m))          # route library output to the GUI
        ia.progress = lambda **kw: put(("progress", kw))
        pico = rx = None
        try:
            os.makedirs(cfg.out_dir, exist_ok=True)
            ia.validate(cfg)
            if not cfg.no_tx:
                port = ia.find_serial_port(cfg.port)
                pico = ia.PicoTx(port, echo=False, requested=cfg.port)
                put(("log", f"TX: connected to {port}"))
                pico.drain(0.5)
                pico.send("TX_STOP")
                pico.drain(0.3)

            if mode == "txonly":
                if pico is None:
                    raise SystemExit("TX only needs the Pico - uncheck "
                                     "'Receive only (no TX)'")
                put(("status", "TX only: the SDR is free for another program"))
                ia.run_tx_only(pico, cfg, stop_evt=self.stop_evt)
                return

            rx = ia.RtlRx(cfg.device, cfg.rate, cfg.direct_samp, cfg.gain,
                          cfg.agc, cfg.ppm, cfg.digital_agc)
            rx.open()

            if mode == "ionogram":
                put(("status", "Ionogram: recording the sweep..."))
                ia.run_ionogram_series(
                    rx, pico, cfg, stop_evt=self.stop_evt,
                    on_done=lambda png: put(("ionogram",
                                             (png, os.path.dirname(png or "")))))
                return

            seq = 0
            while not self.stop_evt.is_set() and (cfg.cycles == 0 or seq < cfg.cycles):
                t0 = time.time()
                freq = cfg.freq_list[seq % len(cfg.freq_list)]
                seq += 1
                put(("status", f"Cycle {seq}: sounding {freq/1e6:.4f} MHz..."))
                try:
                    meta = ia.sound_once(rx, pico, cfg, freq, seq)
                    if meta:
                        put(("result", meta))
                except Exception as e:
                    put(("log", f"cycle FAILED: {type(e).__name__}: {e}"))
                    time.sleep(1.0)
                if pico and not pico.ok:
                    put(("log", "TX serial link lost - reconnecting..."))
                    put(("log", "TX link restored" if pico.reconnect(60.0)
                         else "TX link still down"))
                if cfg.period > 0 and not self.stop_evt.is_set():
                    wait = cfg.period - (time.time() - t0)
                    if wait > 0:
                        put(("status", f"Idle {wait:.0f} s until next cycle"))
                        self.stop_evt.wait(wait)
        except SystemExit as e:
            put(("error", str(e)))
        except Exception as e:
            put(("error", f"{type(e).__name__}: {e}"))
        finally:
            if pico:
                pico.stop_auto()
                pico.close()
            if rx:
                rx.close()
            put(("done", None))

    # ---------------------------------------------------------- GUI updates

    def _pump(self):
        """Drain worker events on the Tk thread."""
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.log(payload)
                    short = payload.strip()
                    if short and not short.startswith(">>>"):
                        self.status.config(text=short[:120])
                elif kind == "status":
                    self.status.config(text=payload)
                elif kind == "result":
                    self._add_result(payload)
                elif kind == "progress":
                    self._set_progress(**payload)
                elif kind == "ionogram":
                    self._add_ionogram(*payload)
                elif kind == "error":
                    self.log("ERROR: " + payload)
                    messagebox.showerror("Sounding stopped", payload)
                elif kind == "done":
                    self._set_progress()
                    self.start_btn.config(state=tk.NORMAL)
                    self.stop_btn.config(state=tk.DISABLED)
                    self.status.config(text="Idle")
        except queue.Empty:
            pass
        self.root.after(100, self._pump)

    def _add_result(self, meta):
        self.history.append(meta)
        f = meta["tx_freq_hz"] / 1e6
        snr = meta.get("snr_db")
        tag = "ok" if meta.get("png_path") else "--"
        self.hist.insert(tk.END, f"{meta['utc'][11:19]} {f:7.3f}MHz "
                                 f"{snr if snr is not None else '?':>5} dB {tag}")
        self.hist.see(tk.END)
        self.hist.selection_clear(0, tk.END)
        self.hist.selection_set(tk.END)
        self._show(meta)

    def _add_ionogram(self, png, folder):
        meta = {"kind": "ionogram", "png_path": png, "folder": folder,
                "utc": datetime.utcnow().isoformat(timespec="seconds")}
        self.history.append(meta)
        self.hist.insert(tk.END, f"{meta['utc'][11:19]}  IONOGRAM")
        self.hist.see(tk.END)
        self.hist.selection_clear(0, tk.END)
        self.hist.selection_set(tk.END)
        self._show(meta)
        self.nb.select(0)

    # ---------------------------------------------------------- progress bar

    @staticmethod
    def _mmss(seconds):
        seconds = max(0, int(round(seconds)))
        return f"{seconds//60}:{seconds%60:02d}"

    def _set_progress(self, text="", seconds=None, done=None, total=None):
        """Called from the event pump with whatever the engine reported.

        Timed work drives the task bar, counted steps drive the series bar, and
        the two are independent - a sweep keeps its position while each frame
        runs its own clock.
        """
        if done is not None and total:
            same = self._sweep and self._sweep[3] == total
            t0 = self._sweep[2] if same else time.time()
            self._sweep = (done, total, t0, total, text)
            return
        if seconds and seconds > 0:
            self._task = (time.time(), float(seconds), text)
            self._stop_busy()
            return
        if text:                                   # open-ended job
            self._task = None
            self.task_lbl.config(text=text)
            if not self._pb_busy:
                self.pb.configure(mode="indeterminate")
                self.pb.start(60)
                self._pb_busy = True
            self.pb_lbl.config(text="")
            return
        self._task = self._sweep = None            # idle
        self._stop_busy()
        for bar, lab, val in ((self.pb, self.task_lbl, self.pb_lbl),
                              (self.pb2, self.series_lbl, self.pb2_lbl)):
            bar.configure(value=0)
            lab.config(text="")
            val.config(text="")

    def _stop_busy(self):
        if self._pb_busy:
            self.pb.stop()
            self.pb.configure(mode="determinate")
            self._pb_busy = False

    def _tick_progress(self):
        if self._task:
            t0, total, text = self._task
            done = time.time() - t0
            self.pb.configure(value=min(1.0, done / total) * 1000 if total > 0 else 0)
            self.task_lbl.config(text=text)
            self.pb_lbl.config(text=f"{self._mmss(done)} / {self._mmss(total)}")
        if self._sweep:
            done, total, t0, _tot, text = self._sweep
            self.pb2.configure(value=done / total * 1000)
            elapsed = time.time() - t0
            eta = (elapsed / done * (total - done)) if done else None
            self.series_lbl.config(text=text or f"{done}/{total}")
            self.pb2_lbl.config(text=f"ETA {self._mmss(eta)}" if eta else
                                f"{done}/{total}")
        self.root.after(100, self._tick_progress)

    def _set_scale_state(self, on):
        state = tk.NORMAL if on else tk.DISABLED
        for w in (self.vmin_scale, self.vmax_scale):
            w.configure(state=state)
        for child in self.scale_box.winfo_children():
            for w in child.winfo_children() if isinstance(child, ttk.Frame) else [child]:
                if isinstance(w, ttk.Button):
                    w.configure(state=state)

    def _npz_for(self, meta):
        png = meta.get("png_path")
        if not png:
            return None
        npz = os.path.splitext(png)[0] + ".npz"
        return npz if os.path.exists(npz) else None

    def _prepare_scale(self, meta):
        """Point the sliders at this ionogram's actual dB range."""
        npz = self._npz_for(meta)
        if not npz:
            self._set_scale_state(False)
            return
        import ionogram as ig
        try:
            _f, _km, M = ig.load_data(npz)
        except Exception:
            self._set_scale_state(False)
            return
        finite = M[np.isfinite(M)] if hasattr(M, "__array__") else M
        lo = float(np.floor(np.nanmin(finite))) if finite.size else -10.0
        hi = float(np.ceil(np.nanmax(finite))) if finite.size else 60.0
        if hi - lo < 1.0:
            hi = lo + 1.0
        for w in (self.vmin_scale, self.vmax_scale):
            w.configure(from_=lo, to=hi)
        a, b = ig.auto_levels(M)
        meta.setdefault("vmin", max(lo, a))
        meta.setdefault("vmax", min(hi, b))
        self.vmin_var.set(meta["vmin"])
        self.vmax_var.set(meta["vmax"])
        self._update_scale_labels()
        self._set_scale_state(True)

    def _update_scale_labels(self):
        self.vmin_lbl.config(text=f"{self.vmin_var.get():.1f}")
        self.vmax_lbl.config(text=f"{self.vmax_var.get():.1f}")

    def _on_scale(self, _value=None):
        self._update_scale_labels()
        if self._scale_job:
            self.root.after_cancel(self._scale_job)
        self._scale_job = self.root.after(200, self._rescale)   # debounce dragging

    def _auto_scale(self):
        meta = getattr(self, "_current", None)
        npz = self._npz_for(meta) if meta else None
        if not npz:
            return
        import ionogram as ig
        _f, _km, M = ig.load_data(npz)
        a, b = ig.auto_levels(M)
        self.vmin_var.set(a)
        self.vmax_var.set(b)
        self._on_scale()

    def _rescale(self):
        self._scale_job = None
        meta = getattr(self, "_current", None)
        if not meta or meta.get("kind") != "ionogram":
            return
        npz = self._npz_for(meta)
        if not npz:
            return
        vmin, vmax = self.vmin_var.get(), self.vmax_var.get()
        if vmax <= vmin:
            vmax = vmin + 0.5
        import ionogram as ig
        try:
            ig.replot(npz, meta["png_path"], vmin=vmin, vmax=vmax,
                      title="Ionogram",
                      subtitle=meta.get("local", "")[:19].replace("T", " "))
        except Exception as e:
            self.log(f"redraw failed: {e}")
            return
        meta["vmin"], meta["vmax"] = vmin, vmax
        self._redraw()

    def _on_pick(self, _evt):
        sel = self.hist.curselection()
        if sel:
            self._show(self.history[sel[0]])

    def _show(self, meta):
        self._current = meta
        if meta.get("kind") == "ionogram":
            self.metrics.config(text=f"Ionogram {meta['utc'][:19].replace('T', ' ')} UTC\n"
                                     f"{meta.get('png_path') or 'no plot'}")
            self._prepare_scale(meta)
            self._redraw()
            return
        self._set_scale_state(False)
        ov = meta.get("overflows", 0)
        self.metrics.config(text=(
            f"cycle {meta.get('cycle','?')}   {meta['utc'][11:19]}   "
            f"TX {meta['tx_freq_hz']/1e6:.4f} MHz -> RX {meta['rx_center_hz']/1e6:.4f} MHz "
            f"(+{meta['offset_hz']/1e3:.0f} kHz)   {meta['duration_s']:.1f} s\n"
            f"peak {meta['peak']:.0f}/32767   rms {meta['rms']:.0f}   "
            f"clipped {meta['clip_frac']*100:.2f}%   band SNR {meta.get('snr_db')} dB   "
            f"overflows {ov}   analysis "
            f"{'OK' if meta.get('analysis_ok') else ('skipped' if meta.get('analysis_ok') is None else 'FAILED')}"))
        self._redraw()

    def _redraw(self):
        meta = getattr(self, "_current", None)
        self.canvas.delete("all")
        w = max(self.canvas.winfo_width(), 10)
        h = max(self.canvas.winfo_height(), 10)
        path = meta.get("png_path") if meta else None
        if not path or not os.path.exists(path):
            msg = ("Waiting for the first result..." if not meta else
                   "No plot for this cycle (analysis failed or was skipped)")
            self.canvas.create_text(w // 2, h // 2, text=msg, fill="#888")
            return
        try:
            if Image is not None:
                img = Image.open(path)
                scale = min(w / img.width, h / img.height, 1.0)
                img = img.resize((max(1, int(img.width * scale)),
                                  max(1, int(img.height * scale))),
                                 Image.LANCZOS)
                self._photo = ImageTk.PhotoImage(img)
            else:
                photo = tk.PhotoImage(file=path)
                k = max(1, int(max(photo.width() / w, photo.height() / h) + 0.999))
                self._photo = photo.subsample(k, k)
            self.canvas.create_image(w // 2, h // 2, image=self._photo)
        except Exception as e:
            self.canvas.create_text(w // 2, h // 2, text=f"Cannot show plot: {e}",
                                    fill="#a00")

    def on_close(self):
        self.stop_evt.set()
        if self.worker and self.worker.is_alive():
            if not messagebox.askokcancel(
                    "Quit", "A sounding cycle is still running. Quit anyway?"):
                return
        self.root.destroy()


def main():
    root = tk.Tk()
    app = AutoGUI(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()

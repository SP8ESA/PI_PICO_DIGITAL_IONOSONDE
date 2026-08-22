"""GUI smoke: widget -> config mapping, progress bar, colour sliders, layout."""
import os, sys, time
import tkinter as tk
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ionosonde_auto as ia
import ionosonde_auto_gui as gui

root = tk.Tk()
app = gui.AutoGUI(root)
root.update_idletasks()

cfg = app._build_cfg()
assert cfg.freq_list and cfg.offset_hz > 0 and cfg.rate > 0
assert cfg.s2s == int(round(ia.s2s_from_range(cfg.range_km, cfg.chip_overhead_us)))
b = ia.tx_budget(cfg)
print(f"config: {cfg.freq_list[0]/1e6:.3f} MHz, offset {cfg.offset_hz/1e3:.0f} kHz, "
      f"{cfg.rate/1e6:.3f} MS/s")
print(f"budget: range {cfg.range_km:.0f} km -> S2S {cfg.s2s} us, frame "
      f"{b['frame_s']:.2f} s, peak {b['peak_w']:.2f} W, mean {b['avg_w']*1e3:.0f} mW, "
      f"energy {b['energy_j']:.2f} J")
assert abs(b['peak_w'] - 1.5 * (cfg.amp/100)**2) < 1e-9
assert abs(b['energy_j'] - b['peak_w']*b['chip_s']*cfg.chips) < 1e-9
assert abs(b['avg_w'] - b['energy_j']/b['frame_s']) < 1e-9
assert not hasattr(cfg, "bias_tee") and not hasattr(cfg, "analyzer_mode")
assert "bias_tee" not in app.vars and "analyzer_mode" not in app.vars

ic = app._build_cfg(ionogram=True)
assert ic.ionogram and ic.ion_repeat is not None
plan = ia.sweep_plan(ic.ion_start*1e6, ic.ion_stop*1e6, ic.ion_step_khz*1e3)
print(f"ionogram: {len(plan)} steps, repeat {ic.ion_repeat}, every {ic.ion_period} min")
assert app._build_cfg(single=True).cycles == 1
app.vars['enable_rx'].set(False)
c = app._build_cfg(); assert c.tx_only and not c.no_tx
app.vars['enable_rx'].set(True); app.vars['enable_tx'].set(False)
c = app._build_cfg(); assert c.no_tx and not c.tx_only
app.vars['enable_tx'].set(False); app.vars['enable_rx'].set(False)
try:
    app._build_cfg(); print("FAIL: both disabled accepted"); sys.exit(1)
except ValueError:
    pass
app.vars['enable_tx'].set(True); app.vars['enable_rx'].set(True)
print("enables: TX+RX, TX only, RX only and neither all map correctly")
assert not hasattr(app, "txonly_btn") and not hasattr(app, "selftest")

app.vars['freqs'].set("")
try:
    app._build_cfg()
    print("FAIL: empty freqs accepted"); sys.exit(1)
except ValueError as e:
    print("bad input rejected:", str(e)[:60])
app.vars['freqs'].set("3.822")


def pump(sec):
    end = time.time() + sec
    while time.time() < end:
        root.update(); time.sleep(0.02)


app._set_progress(text="transmitting 3.822 MHz", seconds=4.0)
a = app.pb.cget("value"); pump(1.0); b = app.pb.cget("value")
assert b > a and "transmitting" in app.task_lbl.cget("text")
app._set_progress(done=5, total=25, text="sweep 6/25")
pump(0.2)
assert abs(app.pb2.cget("value") - 200) < 1, "series bar not driven"
assert app.pb.cget("value") > 0, "task bar lost its own progress"
print(f"two bars: task {app.task_lbl.cget('text')!r} {app.pb_lbl.cget('text')!r} | "
      f"series {app.series_lbl.cget('text')!r} {app.pb2_lbl.cget('text')!r}")
app._set_progress(text="analysing")
assert str(app.pb.cget("mode")) == "indeterminate"
app._set_progress()
assert str(app.pb.cget("mode")) == "determinate" and app.pb.cget("value") == 0
assert app.pb2.cget("value") == 0
print("progress bar: timed, stepped, busy and idle all OK")

assert hasattr(app, "status") and app.status.winfo_manager() == "pack"
for w, h in ((1400, 820), (900, 480)):
    root.geometry(f"{w}x{h}"); root.update_idletasks(); root.update()
    y = app.start_btn.winfo_rooty() - root.winfo_rooty()
    assert 0 < y < h, f"Start off screen at {w}x{h}"
    assert app.canvas.winfo_width() > 200
print("layout: controls reachable and plot area alive at 1400x820 and 900x480")

root.destroy()
print("\nRESULT: OK")

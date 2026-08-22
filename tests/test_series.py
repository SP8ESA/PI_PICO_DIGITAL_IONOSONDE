"""Ionogram series: N maps back to back, own folder each, Stop honoured."""
import os, sys, time, shutil, tempfile, threading, glob
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ionosonde_auto as ia

FS, OFF, BIT_US, S2S_US, CHIPS = 250000, 50e3, 40.0, 4975, 60
B13 = np.array([1,1,1,1,1,-1,-1,1,1,-1,1,-1,1], float)

def frame(n):
    t = np.arange(n)/FS
    bit_n = int(round(BIT_US*1e-6*FS)); chip = np.repeat(B13, bit_n)
    s2s = int(round(S2S_US*1e-6*FS)); base = np.zeros(n)
    d = int(2*300e3/299792458.0*FS)
    for k in range(CHIPS):
        s = k*s2s
        if s+len(chip) > n: break
        base[s:s+len(chip)] += chip
        if s+d+len(chip) <= n: base[s+d:s+d+len(chip)] += 0.08*chip
    z = base*np.exp(2j*np.pi*OFF*t)
    return z + (np.random.randn(n)+1j*np.random.randn(n))*0.03

class Dev:
    def __init__(s): s.iq=None; s.pos=0
    def readStream(s, st, buffs, num, timeoutUs=0):
        n = min(num, len(s.iq)-s.pos)
        if n <= 0: s.pos, n = 0, num
        seg = s.iq[s.pos:s.pos+n]; s.pos += n
        v = np.empty(n*2, np.int16)
        v[0::2] = np.clip(seg.real*900,-32767,32767); v[1::2] = np.clip(seg.imag*900,-32767,32767)
        buffs[0][:n*2] = v; time.sleep(n/FS)
        class R: pass
        r=R(); r.ret=n; return r

class Rx:
    def __init__(s): s.dev=Dev(); s.stream=object(); s.fs=FS; s.direct_samp=2; s.center=None
    def open(s): pass
    def close(s): pass
    def activate(s): pass
    def deactivate(s): pass
    def tune(s, hz):
        s.center=hz; s.dev.iq=frame(int(CHIPS*S2S_US/1e6*FS)+int(2*FS)); s.dev.pos=0; return hz

class Pico:
    ok=True; frame_s=CHIPS*S2S_US/1e6; silent_frames=0
    def alive(s, timeout=1.5): return True
    def recover(s, timeout=45.0): return True
    def drain(s, x=0): pass
    def send(s, c): pass
    def poll(s): return []
    def wait_for(s, groups, timeout):
        if any("Completed" in g for grp in groups for g in grp):
            time.sleep(s.frame_s); return "[TX-0] Completed"
        return "[TX-0] Started at 1 us"
    def set_params(s, f, cfg): pass
    def stop_auto(s): pass
    def close(s): pass

out = os.path.join(tempfile.mkdtemp(prefix="iono_series_"), "out")
shutil.rmtree(out, ignore_errors=True)
sys.argv = ["ionosonde_auto.py", "--ionogram", "--ion-start", "3.0", "--ion-stop", "3.4",
            "--ion-step-khz", "200", "--ion-repeat", "3", "--ion-period", "0",
            "--offset-khz", str(OFF/1e3), "--rate", str(FS), "--bit-us", str(BIT_US),
            "--s2s", str(S2S_US), "--chips", str(CHIPS), "--out-dir", out,
            "--settle-ms", "100", "--tail-ms", "100", "--chip-overhead-us", "0"]
cfg = ia.parse_args(); ia.validate(cfg); os.makedirs(out, exist_ok=True)

done = []
t0 = time.time()
made = ia.run_ionogram_series(Rx(), Pico(), cfg, stop_evt=threading.Event(),
                              on_done=lambda p: done.append(p))
print(f"\nzrobione: {made} jonogramy w {time.time()-t0:.1f} s")
maps = sorted(glob.glob(os.path.join(out, "ionograms", "ionogram_*.png")))
sweeps = sorted(glob.glob(os.path.join(out, "sweeps", "sweep_*")))
print("mapy w jednym folderze:", [os.path.basename(p) for p in maps])
print("katalogi sweepów:", [os.path.basename(f) for f in sweeps])
ok = (made == 3 and len(done) == 3 and len(maps) == 3 and len(sweeps) == 3
      and all(os.path.dirname(p) == os.path.join(out, "ionograms") for p in done))

# Stop w trakcie serii
ev = threading.Event()
sys.argv[sys.argv.index("--ion-repeat")+1] = "0"       # forever
cfg2 = ia.parse_args()
t = threading.Thread(target=lambda: ia.run_ionogram_series(Rx(), Pico(), cfg2, stop_evt=ev))
t.start(); time.sleep(6); ev.set(); t.join(timeout=60)
print("Stop w serii:", "zadziałał" if not t.is_alive() else "NIE zadziałał")
ok = ok and not t.is_alive()
print("\nRESULT:", "OK" if ok else "FAILED")
sys.exit(0 if ok else 1)

"""Planted echoes must survive the new template, at the right range and level."""
import json, os, sys, tempfile, wave
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ionogram as ig

FS, OFF, BIT_US, S2S_US, CHIPS = 250000, 50e3, 40.0, 4975, 400
B13 = np.array([1,1,1,1,1,-1,-1,1,1,-1,1,-1,1], float)
OUT = os.path.join(tempfile.mkdtemp(prefix="iono_test_"), "echo")
os.makedirs(OUT, exist_ok=True)

def make(km_echo, echo_amp, noise=0.05, interferer=True):
    n = int((CHIPS * S2S_US * 1e-6 + 1.0) * FS)
    bit_n = int(round(BIT_US * 1e-6 * FS)); chip = np.repeat(B13, bit_n)
    s2s_n = int(round(S2S_US * 1e-6 * FS))
    base = np.zeros(n)
    d = int(2 * km_echo * 1000 / 299792458.0 * FS)
    for k in range(CHIPS):
        s = k * s2s_n
        if s + len(chip) > n: break
        base[s:s+len(chip)] += chip
        if echo_amp and s + d + len(chip) <= n:
            base[s+d:s+d+len(chip)] += echo_amp * chip
    t = np.arange(n) / FS
    z = base * np.exp(2j*np.pi*OFF*t)
    z += (np.random.randn(n) + 1j*np.random.randn(n)) * noise
    if interferer:          # a loud spike, exactly what used to hijack the probe
        j = int(0.8 * n)
        z[j:j+40] += 50.0 * np.exp(2j*np.pi*OFF*t[j:j+40])
    return z

for km_echo, amp in ((300, 0.05), (300, 0.02), (160, 0.05), (500, 0.05)):
    z = make(km_echo, amp)
    p = os.path.join(OUT, f"baseband_1000000Hz_e{km_echo}_{amp}.wav")
    v = np.empty(len(z)*2, np.int16)
    v[0::2] = np.clip(z.real*900, -32767, 32767); v[1::2] = np.clip(z.imag*900, -32767, 32767)
    with wave.open(p, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(FS); w.writeframes(v.tobytes())
    meta = {"tx_freq_hz": 1.05e6, "rx_center_hz": 1.0e6, "offset_hz": OFF,
            "tx": {"bit_us": BIT_US, "chips": CHIPS, "s2s_us": S2S_US, "mod": "BARKER13"}}
    json.dump(meta, open(p[:-4] + ".json", "w"))
    km, db, i = ig.profile_from_wav(p, ig.parse_analysis_args("--coh_batch 64"), meta)
    sel = (km > 100) & (km < 700)
    j = int(np.nanargmax(np.where(sel, db, -np.inf)))
    err = km[j] - km_echo
    ok = abs(err) < 5 and db[j] > 6
    print(f"echo {km_echo:>3} km amp {amp:.2f} -> found {km[j]:>5.0f} km "
          f"({err:+.0f} km), {db[j]:+5.1f} dB, {i['n_found']}/{i['n_expected']} pulses  "
          f"{'OK' if ok else 'FAIL'}")

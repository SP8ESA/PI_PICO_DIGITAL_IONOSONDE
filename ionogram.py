#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ionogram support: turn a swept set of recordings into a frequency vs virtual
height map.

The DSP is not reimplemented here - every profile is produced by the very same
chain the single frequency plots use (corr/radar_corr_autoprobe.py), just
returned as numbers instead of being drawn:

    auto-probe -> spectrum -> band-pass -> matched filter -> pulse detection
    -> coherent integration -> smoothing -> dB over the profile median

so a column of the ionogram is exactly the curve you would get from one
`*_coh.png`, laid on its side and coloured.
"""

import argparse
import os
import shlex
import sys
import time
import wave
from datetime import datetime

import numpy as np
import matplotlib
matplotlib.use("Agg")                      # never open a window, GUI owns the screen
import matplotlib.pyplot as plt
from matplotlib.colors import Normalize
from scipy.signal import butter, fftconvolve, hilbert, sosfiltfilt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "corr"))
import radar_corr_autoprobe as rc          # noqa: E402  (path set above)

C_MPS = 299_792_458.0


# =========================================================
#  Analysis parameters, shared with the single-shot path
# =========================================================

def parse_analysis_args(arg_string):
    """Analysis knobs. Unknown options are ignored, so old strings still work."""
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--coh_batch", type=int, default=256)
    ap.add_argument("--max_lag_ms", type=float, default=None)
    ap.add_argument("--smooth_scale", type=float, default=1.0)
    ap.add_argument("--km_min", type=float, default=100.0)
    ap.add_argument("--km_max", type=float, default=650.0)
    ap.add_argument("--c_mps", type=float, default=C_MPS)
    # Experimental and OFF by default: on real captures it removed part of the
    # sounding's own spectrum (the code's line at the bit rate) and made profiles
    # worse, without touching the fringes it was meant to cure.
    ap.add_argument("--notch", dest="notch", action="store_true", default=False)
    known, _ = ap.parse_known_args(shlex.split(arg_string or ""))
    return known


# =========================================================
#  Band isolation + pulse grid
# =========================================================

def read_iq(path):
    """Stereo WAV -> complex baseband, float32 pairs (keeps big captures small)."""
    with wave.open(path, "rb") as w:
        fs = w.getframerate()
        n = w.getnframes()
        raw = np.frombuffer(w.readframes(n), dtype="<i2")
    v = raw.reshape(-1, 2).astype(np.float32)
    z = v[:, 0] + 1j * v[:, 1]
    return float(fs), (z - z.mean()).astype(np.complex64)


def isolate_band(z, fs, offset_hz, bw_hz):
    """Shift the sounding band to zero, low-pass it and decimate.

    The capture can be 2 MHz wide while the sounding occupies ~50 kHz, and the
    rest of the band is full of far stronger broadcasters. Everything after this
    step sees only our own signal, which is what makes the pulses detectable at
    all - and the decimation makes the analysis several times faster.
    """
    t = np.arange(len(z), dtype=np.float32) / fs
    y = z * np.exp(-2j * np.pi * np.float32(offset_hz) * t)
    keep = max(bw_hz * 0.75, 30e3)                 # half-width to keep
    decim = int(max(1, np.floor(fs / (4.0 * keep))))
    sos = butter(8, min(keep / (fs / 2), 0.95), "low", output="sos")
    y = sosfiltfilt(sos, y)
    if decim > 1:
        y = y[::decim]
    return y.astype(np.complex64), fs / decim, decim


def notch_carriers(y, fs, thresh_db=12.0, max_lines=8, notch_hz=400.0,
                   guard_hz=1000.0, log=None):
    """Remove narrowband carriers sitting inside the sounding passband.

    A CW interferer offset by df from our carrier beats against the code in the
    matched filter and paints regular fringes across the whole range profile -
    the striped columns in an ionogram. Our own signal is broadband (~50 kHz), so
    a line that stands far above the local spectrum can be notched out at a cost
    of a fraction of a percent of the sounding energy.

    The FFT is deliberately coarse (308 Hz/bin at 157 kS/s): the pulse train has
    a comb spectrum with ~192 Hz teeth, and a finer analysis would resolve those
    teeth and notch our own signal.
    """
    from scipy.ndimage import median_filter
    NF = 512
    nb = min(400, len(y) // NF)
    if nb < 4:
        return y, []
    Y = y[:nb * NF].reshape(nb, NF)
    psd = (np.abs(np.fft.fft(Y * np.hanning(NF).astype(np.float32), axis=1)) ** 2
           ).mean(axis=0)
    f = np.fft.fftfreq(NF, 1 / fs)
    base = median_filter(psd, size=9, mode="wrap")
    over = 10 * np.log10((psd + 1e-30) / (base + 1e-30))
    cand = np.where((over > thresh_db) & (np.abs(f) > guard_hz))[0]
    if len(cand) == 0:
        return y, []
    lines = []
    for k in cand[np.argsort(over[cand])[::-1]]:
        if all(abs(f[k] - f0) > 2 * notch_hz for f0, _ in lines):
            lines.append((float(f[k]), float(over[k])))
        if len(lines) >= max_lines:
            break

    t = np.arange(len(y), dtype=np.float32) / fs
    sos = butter(4, notch_hz / (fs / 2), "high", output="sos")
    out = y
    for f0, _db in lines:                       # shift the line to DC, notch, shift back
        mix = np.exp(-2j * np.pi * np.float32(f0) * t)
        out = (sosfiltfilt(sos, out * mix) * np.conj(mix)).astype(np.complex64)
    if log:
        log("   notched " + ", ".join(f"{f0/1e3:+.1f} kHz ({db:.0f} dB)"
                                      for f0, db in lines))
    return out, lines


def estimate_period(env, fs, hint_s=None, lo_s=1e-3, hi_s=50e-3):
    """Pulse repetition interval from the envelope autocorrelation, sub-sample."""
    e = (env - env.mean()).astype(np.float32)
    N = 1 << int(np.ceil(np.log2(2 * len(e))))
    E = np.fft.rfft(e, N)
    ac = np.fft.irfft(E * np.conj(E), N)
    ac = ac[:int(hi_s * fs) + 2] / (ac[0] + np.finfo(np.float32).eps)
    lo = int(lo_s * fs)
    if hint_s:                                     # search around the configured S2S
        w = max(int(0.25 * hint_s * fs), 4)
        c = int(hint_s * fs)
        lo2, hi2 = max(lo, c - w), min(len(ac) - 2, c + w)
        k = lo2 + int(np.argmax(ac[lo2:hi2])) if hi2 > lo2 else lo
    else:
        k = lo + int(np.argmax(ac[lo:len(ac) - 2]))
    y0, y1, y2 = ac[k - 1], ac[k], ac[k + 1]       # parabolic refinement
    den = y0 - 2 * y1 + y2
    d = 0.5 * (y0 - y2) / den if abs(den) > 1e-12 else 0.0
    return (k + float(np.clip(d, -1, 1))) / fs, float(ac[k])


BARKER = {2: [1, -1], 3: [1, 1, -1], 4: [1, 1, -1, 1], 5: [1, 1, 1, -1, 1],
          7: [1, 1, 1, -1, -1, 1, -1],
          11: [1, 1, 1, -1, -1, -1, 1, -1, -1, 1, -1],
          13: [1, 1, 1, 1, 1, -1, -1, 1, 1, -1, 1, -1, 1]}


def reference_chip(fs, bit_us, mod="BARKER13"):
    """The chip we transmit, at baseband - a proper matched filter template.

    Cutting the template out of the recording (the strongest sample anywhere)
    picks whatever interference spike happens to be loudest, which mismatches the
    filter and smears energy across the whole profile.
    """
    seq = BARKER.get(_seq_len(mod), BARKER[13])
    bit_n = max(1, int(round(bit_us * 1e-6 * fs)))
    return np.repeat(np.asarray(seq, dtype=np.float32), bit_n).astype(np.complex64)


def refine_template(y, starts, m, mf_vals=None, use_frac=0.25):
    """Measured template: phase-align the strongest pulses and average them.

    This captures the real pulse shape - DAC steps, filter ringing, the receiver
    response - so the matched filter is matched to what actually arrives.
    """
    starts = np.asarray(starts)
    if mf_vals is not None and len(starts) > 8:
        keep = starts[np.argsort(mf_vals)[::-1][:max(8, int(len(starts) * use_frac))]]
    else:
        keep = starts
    segs = [y[s:s + m] for s in keep if 0 <= s and s + m <= len(y)]
    if len(segs) < 4:
        return None
    segs = np.stack(segs).astype(np.complex128)
    ref = segs[np.argmax(np.abs(segs).max(axis=1))]
    inner = segs @ np.conj(ref)                       # phase of each vs the reference
    ph = np.exp(-1j * np.angle(inner))
    avg = (segs * ph[:, None]).mean(axis=0)
    n = np.linalg.norm(avg)
    return (avg / n).astype(np.complex64) if n > 0 else None


def track_pulse_train(mf_abs, fs, period_s, n_expected, search_s, floor_ratio=0.25):
    """Follow the pulse train instead of assuming a rigid grid.

    The firmware schedules every chip relative to the *current* time
    (bpsk_tx.c: t_start = now + 150 us), so the interval carries the per-chip
    software overhead and the error accumulates as a random walk - a fixed grid
    drifts off the train within a few hundred chips. Here each pulse is predicted
    from the previous one plus a slowly adapted interval, and searched for in a
    narrow window. If nothing stands above the floor the tracker coasts on the
    prediction, so a faded chip keeps its slot instead of snapping onto noise.
    """
    n = len(mf_abs)
    win = max(2, int(search_s * fs))
    thr = floor_ratio * float(np.median(mf_abs[mf_abs > np.percentile(mf_abs, 99)]))
    alpha = 0.05                                   # interval tracking rate
    lo_T, hi_T = period_s * fs * 0.97, period_s * fs * 1.03

    def walk(start_k, step_sign, T0):
        """Return positions from start_k (exclusive) in one direction."""
        out, T, cur = [], T0, float(start_k)
        while True:
            pred = cur + step_sign * T
            a, b = int(round(pred)) - win, int(round(pred)) + win
            if a < 0 or b >= n:
                break
            k = a + int(np.argmax(mf_abs[a:b]))
            if mf_abs[k] >= thr:
                meas = abs(k - cur)
                T = float(np.clip((1 - alpha) * T + alpha * meas, lo_T, hi_T))
                cur = float(k)
            else:
                cur = pred                          # coast through the fade
            out.append(int(round(cur)))
        return out

    # Anchor on the phase of the whole train, not on the single loudest sample.
    # One interference spike can be louder than any pulse; anchoring there puts
    # every slot between pulses, and the tracker then coasts through the entire
    # frame and returns a full count of nothing. Folding the matched filter at
    # the measured period averages hundreds of pulses, so the phase is decided by
    # the train itself.
    L = int(round(period_s * fs))
    nper = int(min(512, n // max(L, 1)))
    if nper >= 8:
        fold = mf_abs[:nper * L].reshape(nper, L).mean(axis=0)
        phase = int(np.argmax(fold))
        cands = phase + np.arange(n // L) * L
        cands = cands[(cands > win) & (cands < n - win)]
        if len(cands):
            local = np.array([mf_abs[c - win:c + win].max() for c in cands])
            k0 = int(cands[int(np.argmax(local))])
        else:
            k0 = int(np.argmax(mf_abs))
    else:
        k0 = int(np.argmax(mf_abs))
    T0 = period_s * fs
    back = walk(k0, -1, T0)[::-1]
    fwd = walk(k0, +1, T0)
    idx = np.array(back + [k0] + fwd, dtype=int)
    idx = idx[(idx >= 0) & (idx < n)]

    vals = mf_abs[idx]
    n_train = int(np.sum(vals >= thr))
    n_want = int(n_expected) if n_expected else len(idx)
    if n_want >= len(idx):
        sel = idx
    else:                                           # the run carrying the frame
        csum = np.concatenate(([0.0], np.cumsum(vals.astype(np.float64))))
        j0 = int(np.argmax(csum[n_want:] - csum[:-n_want]))
        sel = idx[j0:j0 + n_want]
    coasted = int(np.sum(mf_abs[sel] < thr))
    return sel, coasted, n_train


def pulse_grid(mf_abs, fs, period_s, n_expected, search_frac=0.2):
    """Place exactly n_expected pulse starts on the measured grid.

    Individual pulses fade, so a plain amplitude threshold finds a different
    number every time. The train is strictly periodic, so instead we lay the
    known grid over the matched filter output, snap each slot to its local
    maximum and keep weak slots - coherent integration wants them. The grid is
    anchored on the FIRST slot that actually carries a pulse, not on the
    earliest slot that fits, so the recorded lead-in does not consume a slot and
    push the last chip out of the record.
    """
    L = period_s * fs
    win = max(2, int(search_frac * L))
    k_ref = int(np.argmax(mf_abs))                 # strongest pulse anchors the phase

    i_lo = int(np.ceil((win - k_ref) / L))
    i_hi = int(np.floor((len(mf_abs) - win - 1 - k_ref) / L))
    if i_hi < i_lo:
        return np.array([], dtype=int), 0, 0

    idx = np.empty(i_hi - i_lo + 1, dtype=int)
    for n, i in enumerate(range(i_lo, i_hi + 1)):
        c = int(round(k_ref + i * L))
        a = c - win
        idx[n] = a + int(np.argmax(mf_abs[a:c + win]))
    vals = mf_abs[idx]

    # Which run of n_expected consecutive slots is the transmitted frame? The one
    # carrying the most matched-filter energy. Picking the window instead of
    # thresholding the first pulse keeps the count exact even when the start of
    # the train is deep in a fade.
    typical = float(np.median(vals[vals >= np.percentile(vals, 75)]))
    thr = max(0.15 * typical, 3.0 * float(np.median(mf_abs)))
    n_train = int(np.sum(vals >= thr))

    n_want = int(n_expected) if n_expected else len(idx)
    if n_want >= len(idx):
        sel = idx                                   # record shorter than the frame
    else:
        csum = np.concatenate(([0.0], np.cumsum(vals.astype(np.float64))))
        window = csum[n_want:] - csum[:-n_want]
        j0 = int(np.argmax(window))
        sel = idx[j0:j0 + n_want]
    centres = np.round(k_ref + np.arange(i_lo, i_hi + 1) * L).astype(int)
    pos = int(np.searchsorted(idx, sel[0])) if len(sel) else 0
    snapped = int(np.sum(sel != centres[pos:pos + len(sel)]))
    return sel, snapped, n_train


# =========================================================
#  One recording -> one range profile
# =========================================================

def profile_from_wav(path, ap, meta=None):
    """Band-isolated analysis: works even when the capture is 2 MHz wide and the
    sounding is nowhere near the strongest thing in it.

    Returns (km, db, info); info['n_found'] vs info['n_expected'] tells you
    whether every transmitted chip was accounted for.
    """
    meta = meta or {}
    tx = meta.get("tx", {})
    offset_hz = float(meta.get("offset_hz", getattr(ap, "offset_hz", 0.0) or 0.0))
    bit_us = float(tx.get("bit_us", 40.0))
    n_expected = int(tx.get("chips", 0) or 0)
    s2s_us = float(tx.get("s2s_us", 0) or 0)
    bw_hz = 2.0 / (bit_us * 1e-6)

    fs0, z = read_iq(path)
    if offset_hz <= 0:
        raise RuntimeError("capture metadata has no tuning offset - cannot isolate "
                           "the sounding band")
    y, fs, decim = isolate_band(z, fs0, offset_hz, bw_hz)
    del z
    notched = []
    if getattr(ap, "notch", False):
        y, notched = notch_carriers(y, fs)

    env = np.abs(y)
    med = float(np.median(env)) + np.finfo(np.float32).eps
    crest = float(env.max() / med)

    # period first: it is the most robust thing in the recording
    hint = (s2s_us * 1e-6) if s2s_us else None
    period_s, ac_r = estimate_period(env, fs, hint_s=hint)
    if not np.isfinite(period_s) or period_s <= 0:
        raise RuntimeError("could not measure the pulse repetition interval")

    # Template: start from the chip we know we transmit, then refine it into the
    # measured pulse shape once we know where the pulses are.
    chip_us = bit_us * _seq_len(tx.get("mod", "BARKER13"))
    m = int(round(chip_us * 1e-6 * fs))
    m = int(np.clip(m, 8, int(0.9 * period_s * fs)))
    h = reference_chip(fs, bit_us, tx.get("mod", "BARKER13"))
    if len(h) < 8:
        raise RuntimeError("reference chip shorter than 8 samples - raise the rate")
    m = len(h)

    def matched(template):
        # Slicing off the first M-1 samples of the full convolution puts the
        # output into "pulse start" coordinates: index i is the correlation for a
        # segment beginning at y[i]. Without this the segments start half a
        # template late and every echo reads ~M/2 too early.
        return np.abs(fftconvolve(y, np.conj(template[::-1]),
                                  mode="full"))[len(template) - 1:].astype(np.float32)

    mf = matched(h)
    if n_expected <= 0:
        n_expected = int(np.floor(len(y) / (period_s * fs)))
    # search window: the chip itself plus room for the per-chip timing jitter
    search_s = max(1.5 * chip_us * 1e-6, 0.02 * period_s)
    starts, coasted, n_train = track_pulse_train(mf, fs, period_s, n_expected, search_s)
    if len(starts) < 2:
        raise RuntimeError("could not track the pulse train inside the capture")

    h_ref = refine_template(y, starts, m, mf_vals=mf[starts])
    if h_ref is not None:                       # second pass on the measured shape
        h = h_ref
        mf = matched(h)
        starts, coasted, n_train = track_pulse_train(mf, fs, period_s,
                                                     n_expected, search_s)
    n_found = len(starts)
    if n_found < 2:
        raise RuntimeError("could not track the pulse train inside the capture")
    steps = np.diff(starts) / fs * 1e6
    jitter_us = float(np.std(steps)) if len(steps) else 0.0
    detected = n_found - coasted
    if detected < 0.3 * n_found:
        raise RuntimeError(
            f"pulse train not detected: only {detected}/{n_found} slots carry a "
            f"pulse (the rest were coasted) - the capture has no usable sounding")

    pulse_snr = float(20 * np.log10(np.median(mf[starts]) / (np.median(mf) + 1e-30)))

    period_samp = int(round(period_s * fs))
    lags_s, mag_mean, _nb, used, _d, _t = rc.coherent_batch_mean_fast(
        y.astype(np.complex128), h.astype(np.complex128), starts, period_samp, fs,
        batch_size=ap.coh_batch, max_lag_ms=ap.max_lag_ms,
        align=True, frac=True, return_traces=False)
    if mag_mean is None:
        raise RuntimeError("coherent integration produced no data")

    win_len = int(round((len(h) / 100.0) * ap.smooth_scale))
    mag_sm = rc.smooth_moving_avg(mag_mean, win_len)
    eps = np.finfo(float).eps
    db = 20.0 * np.log10((mag_sm + eps) / (np.median(mag_sm) + eps))
    km = rc.ms_to_km(lags_s * 1000.0, ap.c_mps)

    # rc.coherent_batch_mean_fast sums coherently INSIDE a batch of coh_batch and
    # then averages the batch magnitudes - phase is dropped between batches. So
    # the coherent integration time is coh_batch x period, not the whole frame.
    n_batches = int(np.ceil(n_found / max(1, ap.coh_batch)))
    info = {"fs": fs, "fs_raw": fs0, "decim": decim,
            "coh_batch": int(ap.coh_batch), "n_batches": n_batches,
            "coh_time_s": ap.coh_batch * period_s,
            "n_expected": n_expected, "n_found": n_found, "coasted": coasted,
            "notched": [round(f0/1e3, 2) for f0, _ in notched],
            "detected": detected, "n_train": n_train, "step_mean_us": float(np.mean(steps)) if len(steps) else 0.0,
            "jitter_us": jitter_us,
            "used": int(used), "period_ms": period_s * 1000.0, "period_r": ac_r,
            "probe_len_us": len(h) / fs * 1e6, "band_crest": crest,
            "pulse_snr_db": pulse_snr, "offset_hz": offset_hz}
    return km, db, info


def _seq_len(mod):
    return {"CARRIER": 1, "BARKER2": 2, "BARKER3": 3, "BARKER4": 4, "BARKER5": 5,
            "BARKER7": 7, "BARKER11": 11, "BARKER13": 13}.get(str(mod).upper(), 13)


# =========================================================
#  Assemble and draw
# =========================================================

def build_matrix(columns, km_min, km_max, n_bins=500):
    """columns: list of (freq_hz, km, db). Returns freqs, km_axis, matrix[km, freq]."""
    freqs = np.array([c[0] for c in columns], dtype=float)
    order = np.argsort(freqs)
    freqs = freqs[order]
    km_axis = np.linspace(km_min, km_max, n_bins)
    M = np.full((n_bins, len(freqs)), np.nan)
    for j, idx in enumerate(order):
        _f, km, db = columns[idx]
        if km is None or db is None:
            continue                        # failed sounding stays a gap
        M[:, j] = np.interp(km_axis, km, db, left=np.nan, right=np.nan)
    return freqs, km_axis, M


def auto_levels(M):
    """Default colour range: noise at the bottom, 99.5th percentile at the top."""
    finite = M[np.isfinite(M)]
    if finite.size == 0:
        return 0.0, 3.0
    vmin = 0.0
    return vmin, float(max(np.percentile(finite, 99.5), vmin + 3.0))


def plot_ionogram(freqs_hz, km_axis, M, out_png, title=None, subtitle=None,
                  vmin=None, vmax=None):
    """Sequential single-ramp heat map: magnitude, so no rainbow, no dual axis."""
    finite = M[np.isfinite(M)]
    if finite.size == 0:
        raise RuntimeError("nothing to plot - every sounding failed")
    a, b = auto_levels(M)
    vmin = a if vmin is None else float(vmin)
    vmax = b if vmax is None else float(vmax)
    if vmax <= vmin:
        vmax = vmin + 0.5

    f_mhz = freqs_hz / 1e6
    # cell edges so every sounding keeps its own column even with uneven steps
    if len(f_mhz) > 1:
        mid = (f_mhz[1:] + f_mhz[:-1]) / 2.0
        x_edges = np.concatenate(([f_mhz[0] - (mid[0] - f_mhz[0])], mid,
                                  [f_mhz[-1] + (f_mhz[-1] - mid[-1])]))
    else:
        x_edges = np.array([f_mhz[0] - 0.05, f_mhz[0] + 0.05])
    dy = (km_axis[1] - km_axis[0]) / 2.0
    y_edges = np.concatenate((km_axis - dy, [km_axis[-1] + dy]))

    cmap = plt.get_cmap("viridis").copy()
    cmap.set_bad("#e8e8e8")                 # frequencies that produced no profile

    fig, ax = plt.subplots(figsize=(13, 7))
    mesh = ax.pcolormesh(x_edges, y_edges, np.ma.masked_invalid(M),
                         cmap=cmap, norm=Normalize(vmin=vmin, vmax=vmax),
                         shading="flat")
    cb = fig.colorbar(mesh, ax=ax, pad=0.015)
    cb.set_label("Echo strength over profile noise [dB]")
    cb.outline.set_visible(False)

    ax.set_xlabel("Sounding frequency [MHz]")
    ax.set_ylabel("Virtual height [km]")
    ax.set_title(title or "Ionogram", loc="left", fontsize=13)
    if subtitle:
        ax.set_title(subtitle, loc="right", fontsize=9, color="#666")
    ax.grid(True, color="white", alpha=0.15, linewidth=0.6)
    ax.set_axisbelow(False)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.tick_params(length=3, color="#999")

    n_ok = int(np.isfinite(M).any(axis=0).sum())
    ax.annotate(f"{n_ok}/{len(f_mhz)} frequencies analysed",
                xy=(0.005, -0.115), xycoords="axes fraction",
                fontsize=8, color="#666")

    fig.tight_layout()
    fig.savefig(out_png, dpi=150)
    plt.close(fig)
    return out_png


def detrend_profile(km, db, scale_km=150.0):
    """Remove the slow range-dependent background, keep narrow echoes.

    The receive chain settles for milliseconds after each T/R switch, and that
    transient is locked to the pulse train, so coherent averaging turns it into a
    smooth several-dB ramp across the whole profile. A median filter far wider
    than any echo estimates that background; subtracting it leaves the echoes.
    """
    from scipy.ndimage import median_filter
    step = float(np.median(np.diff(km))) if len(km) > 1 else 1.0
    win = int(max(5, round(scale_km / max(step, 1e-9))))
    if win % 2 == 0:
        win += 1
    if win >= len(db):
        return db - np.median(db)
    return db - median_filter(db, size=win, mode="nearest")


def plot_profile(km, db, out_png, title=None, km_min=100.0, km_max=650.0,
                 c_mps=C_MPS, subtitle=None):
    """Single sounding: echo strength against range - the per-cycle plot."""
    sel = (km >= km_min) & (km <= km_max)
    if not sel.any():
        sel = np.ones_like(km, dtype=bool)
    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(km[sel], db[sel], linewidth=1.6, color="#2a6fb0")
    ax.set_xlabel("Virtual height [km]")
    ax.set_ylabel("Echo strength over noise [dB]")
    ax.set_title(title or "Sounding", loc="left", fontsize=12)
    if subtitle:
        ax.set_title(subtitle, loc="right", fontsize=9, color="#666")
    secax = ax.secondary_xaxis(
        "top", functions=(lambda k: rc.km_to_ms(k, c_mps),
                          lambda m: rc.ms_to_km(m, c_mps)))
    secax.set_xlabel("Delay [ms]")
    ax.grid(alpha=0.18, linewidth=0.6)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    fig.tight_layout()
    fig.savefig(out_png, dpi=150)
    plt.close(fig)
    return out_png


def analyse_capture(wav_path, analyze_args="", meta=None, km_min=100.0, km_max=650.0,
                    detrend_km=0.0):
    """Analyse one capture and write its range plot next to it."""
    import json
    if meta is None:
        js = os.path.splitext(wav_path)[0] + ".json"
        meta = json.load(open(js)) if os.path.exists(js) else {}
    ap = parse_analysis_args(analyze_args)
    ap.km_min, ap.km_max = km_min, km_max
    km, db, info = profile_from_wav(wav_path, ap, meta)
    if detrend_km > 0:
        db = detrend_profile(km, db, detrend_km)
        info["detrend_km"] = detrend_km
    stem = os.path.splitext(wav_path)[0]
    png = stem + "_coh.png"
    f_mhz = meta.get("tx_freq_hz", 0) / 1e6
    plot_profile(km, db, png,
                 title=f"{f_mhz:.3f} MHz - {info['n_found']} pulses, "
                       f"{info['n_batches']} x {info['coh_batch']} coherent "
                       f"({info['coh_time_s']*1e3:.0f} ms), magnitudes averaged",
                 km_min=km_min, km_max=km_max, c_mps=ap.c_mps,
                 subtitle=(meta.get("utc", "")[:19].replace("T", " ") + " UTC")
                 if meta.get("utc") else "")
    return png, info


def save_data(npz_path, freqs_hz, km_axis, M, meta=None):
    np.savez_compressed(npz_path, freqs_hz=freqs_hz, km=km_axis, db=M,
                        meta=np.array([repr(meta or {})], dtype=object))
    return npz_path


def load_data(npz_path):
    d = np.load(npz_path, allow_pickle=True)
    return d["freqs_hz"], d["km"], d["db"]


def replot(npz_path, out_png=None, vmin=None, vmax=None, title=None, subtitle=None):
    """Redraw a saved ionogram with a different colour range - no re-analysis."""
    freqs, km, M = load_data(npz_path)
    out_png = out_png or os.path.splitext(npz_path)[0] + ".png"
    return plot_ionogram(freqs, km, M, out_png, title=title, subtitle=subtitle,
                         vmin=vmin, vmax=vmax)


# =========================================================
#  Analyse a folder of captures (phase 2 + 3 on their own)
# =========================================================

def analyse_folder(folder, analyze_args="", km_min=100.0, km_max=650.0,
                   log=print, keep_wav=True, stop_evt=None, title=None,
                   detrend_km=0.0, progress=None, out_stem=None):
    """Build an ionogram from every capture in a folder (uses the .json sidecars
    for the transmit frequency, falls back to the file name)."""
    import glob
    import json

    ap = parse_analysis_args(analyze_args)
    ap.km_min, ap.km_max = km_min, km_max
    wavs = sorted(glob.glob(os.path.join(folder, "*.wav")))
    if not wavs:
        raise RuntimeError(f"no .wav files in {folder}")
    log(f"ionogram: analysing {len(wavs)} captures")

    columns, full_count = [], 0
    for i, wav in enumerate(wavs, 1):
        if stop_evt is not None and stop_evt.is_set():
            log("ionogram: analysis stopped on request")
            break
        js = os.path.splitext(wav)[0] + ".json"
        meta, freq = None, None
        if os.path.exists(js):
            try:
                with open(js) as f:
                    meta = json.load(f)
                freq = float(meta["tx_freq_hz"])
            except Exception:
                meta, freq = None, None
        if freq is None:
            log(f"   [{i}/{len(wavs)}] {os.path.basename(wav)}: no metadata, skipped")
            continue
        t0 = time.time()
        try:
            if progress:
                progress(done=i - 1, total=len(wavs),
                         text=f"analysing {i}/{len(wavs)}")
            km, db, info = profile_from_wav(wav, ap, meta)
            # Every capture must account for all transmitted chips. A detector
            # that returns fewer has locked onto the wrong period, and its column
            # is nonsense (short range axis, noise stripes) - so fall back to the
            # band-isolating detector rather than plotting the garbage.
            n_exp = int(info.get("n_expected") or 0)
            if n_exp and int(info.get("n_found") or 0) != n_exp:
                raise RuntimeError(
                    f"only {info.get('n_found')}/{n_exp} chips accounted for")
            if detrend_km > 0:
                db = detrend_profile(km, db, detrend_km)
            columns.append((freq, km, db))
            full_count += 1
            sel = (km >= km_min) & (km <= km_max)
            peak = float(np.nanmax(db[sel])) if sel.any() else float(np.nanmax(db))
            period = info.get("period_ms")
            period_txt = f"T={period:.4f} ms, " if period else ""
            det = info.get("detected")
            det_txt = f" ({det} detected)" if det is not None and det < info['n_found'] else ""
            log(f"   [{i}/{len(wavs)}] {freq/1e6:7.3f} MHz  "
                f"{info['n_found']}/{info['n_expected']} pulses{det_txt}, {period_txt}"
                f"peak {peak:+.1f} dB   ({time.time()-t0:.1f} s)")
            if not keep_wav:
                try:
                    os.remove(wav)          # only ever delete data we could use
                except OSError:
                    pass
        except Exception as e:
            columns.append((freq, None, None))
            log(f"   [{i}/{len(wavs)}] {freq/1e6:7.3f} MHz  FAILED: {e}  "
                f"(raw I/Q kept for a retry)")

    if not columns:
        raise RuntimeError("no usable captures")
    log(f"ionogram: {full_count}/{len(wavs)} captures with the full pulse count")
    freqs, km_axis, M = build_matrix(columns, km_min, km_max)
    stem = out_stem or os.path.join(folder, "ionogram")
    os.makedirs(os.path.dirname(stem) or ".", exist_ok=True)
    png = plot_ionogram(freqs, km_axis, M, stem + ".png",
                        title=title or "Ionogram",
                        subtitle=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
    save_data(stem + ".npz", freqs, km_axis, M)
    log(f"ionogram: {png}")
    return png, freqs, km_axis, M


def main():
    ap = argparse.ArgumentParser(
        description="Build an ionogram from a folder of captures "
                    "(recorded earlier by ionosonde_auto.py --ionogram).")
    ap.add_argument("folder", help="folder with *.wav + *.json captures")
    ap.add_argument("--analyze-args", default="", help="same string as the sounder uses")
    ap.add_argument("--km_min", type=float, default=100.0)
    ap.add_argument("--km_max", type=float, default=650.0)
    ap.add_argument("--keep-wav", action="store_true", default=True)
    a = ap.parse_args()
    analyse_folder(a.folder, a.analyze_args, a.km_min, a.km_max,
                   keep_wav=a.keep_wav)


if __name__ == "__main__":
    main()

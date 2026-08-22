#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Pełny, posprzątany skrypt (bez błędów print/f-string), z wszystkimi funkcjami:
- Domyślnie WAV stereo traktowany jako I/Q → REAL (symetria hermitowska).
- Auto-probe (median/MAD) lub własny --probe.
- Analiza widma → automatyczny filtr (można ograniczyć lub wyłączyć).
- Szybka korelacja (OA-convolve) i szybka integracja koherentna (batched) z dosuwem.
- Zakres wykresu zachowany (domyślnie 100..650 km, regulowane --km_min/--km_max).
- Czekanie „do skutku” aż plik się domknie (watch i single-file) + bezterminowe retry w watch.

Uruchomienie (przykład):
python3 radar_corr_autoprobe_fixed.py \
    --auto_win_ms 0.2 --use_first 2048 --coh_batch 64 \
    --max_lag_ms 650 --km_min 100 --km_max 650
"""

import argparse, os, time, wave
from glob import glob
import numpy as np
import matplotlib.pyplot as plt

try:
    from scipy.io import wavfile
    from scipy.signal import (
        fftconvolve, oaconvolve, find_peaks, butter, sosfiltfilt, sosfreqz, hilbert
    )
except ImportError as e:
    raise SystemExit("Wymagane pakiety: pip install numpy scipy matplotlib") from e

# =========================
#  IQ (stereo)  ->  REAL
# =========================

def analytic_to_real_from_pos(x: np.ndarray) -> np.ndarray:
    """Konwersja sygnału analitycznego x[n] = I + jQ na sygnał rzeczywisty y[n],
    tak aby dodatnie widmo |Y+(f)| odpowiadało dodatniemu widmu |X(f)|.
    """
    x = np.asarray(x, dtype=np.complex128)
    N = x.size
    X = np.fft.fft(x)
    pos_len = N // 2 + 1
    X_pos = X[:pos_len]
    Y_pos = 0.5 * X_pos  # połowa amplitudy na +f (druga połowa trafi w -f)

    if N % 2 == 0:
        Y_neg = np.conj(Y_pos[-2:0:-1])
    else:
        Y_neg = np.conj(Y_pos[-1:0:-1])

    Y = np.concatenate([Y_pos, Y_neg])
    y = np.fft.ifft(Y).real
    return y


def _to_float64(data: np.ndarray) -> np.ndarray:
    if data.dtype.kind in "iu":
        return data.astype(np.float64) / np.iinfo(data.dtype).max
    return data.astype(np.float64)


def read_iq_wav(path: str, swapIQ: bool = False, invertQ: bool = False):
    fs, data = wavfile.read(path)
    if data.ndim != 2 or data.shape[1] != 2:
        raise ValueError("Oczekiwano WAV stereo (I w lewym, Q w prawym kanale).")
    data_f = _to_float64(data)
    I = data_f[:, 0]
    Q = data_f[:, 1]
    if swapIQ:
        I, Q = Q, I
    if invertQ:
        Q = -Q
    x = I + 1j * Q
    return fs, x


def load_1d(path, fs_cli=None, stereo_avg=False, swapIQ=False, invertQ=False):
    """Wczytaj 1D sygnał.
    Domyślne zachowanie dla WAV stereo: traktuj jako I/Q i konwertuj na REAL
    przez symetrię hermitowską. Jeśli chcesz uśrednić do mono, podaj stereo_avg=True.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".wav":
        fs, raw = wavfile.read(path)
        if raw.ndim == 2 and raw.shape[1] == 2:
            if stereo_avg:
                x = _to_float64(raw).mean(axis=1)
                return fs, x
            _, x_c = read_iq_wav(path, swapIQ=swapIQ, invertQ=invertQ)
            x_r = analytic_to_real_from_pos(x_c).astype(np.float64)
            return fs, x_r
        x = _to_float64(raw)
        return fs, x
    elif ext == ".npy":
        if fs_cli is None:
            raise ValueError("Dla .npy podaj --fs.")
        x = np.load(path).astype(np.float64).squeeze()
        return fs_cli, x
    else:
        if fs_cli is None:
            raise ValueError("Dla .txt/.csv podaj --fs.")
        x = np.loadtxt(path, dtype=np.float64).squeeze()
        return fs_cli, x


def dc_norm(x):
    x = x - np.mean(x)
    rms = np.sqrt(np.mean(x**2))
    return x / rms if rms > 0 else x

# ==================================
#  Korelacja / detekcja
# ==================================

def cross_corr(x, h, use_oa=True):
    """x ⋆ h  ≡  conv(x, conj(h)[::-1])  (pełna korelacja)."""
    hmf = np.conj(h)[::-1]
    if use_oa and (len(x) + len(h)) > 250_000:  # heurystyka
        return oaconvolve(x, hmf, mode="full")
    else:
        return fftconvolve(x, hmf, mode="full")


def detect_impulses(corr, fs, probe_len, holdoff_ms=1.0, thr_ratio=0.5):
    abs_c = np.abs(corr)
    thr = thr_ratio * np.max(abs_c) if abs_c.size else 0.0
    zero_idx = probe_len - 1
    pos_part = abs_c[zero_idx:]
    offset = zero_idx
    holdoff_samples = max(1, int(round(fs * holdoff_ms * 1e-3)))
    peaks_rel, _ = find_peaks(pos_part, height=thr, distance=holdoff_samples)
    peaks_idx = peaks_rel + offset
    lags_s = (peaks_idx - zero_idx) / fs
    return peaks_idx, lags_s, thr

# ==========================================
#  Widmo impulsu i projekt filtru
# ==========================================

def next_pow2(n):
    return 1 << (int(np.ceil(np.log2(max(1, n)))))


def probe_spectrum(h, fs, nfft_factor=4, bw_thresh_db=20.0):
    N = len(h)
    Nfft = next_pow2(N) * nfft_factor
    win = np.hanning(N)
    h_w = h * win
    H = np.fft.rfft(h_w, n=Nfft)
    f = np.fft.rfftfreq(Nfft, d=1.0/fs)
    mag = np.abs(H)
    eps = np.finfo(float).eps
    mag_db = 20.0 * np.log10(mag / (mag.max() + eps) + eps)

    k_peak = int(np.argmax(mag))
    peak_db = mag_db[k_peak]
    mask = mag_db >= (peak_db - bw_thresh_db)

    k_lo = k_peak
    while k_lo > 0 and mask[k_lo - 1]:
        k_lo -= 1
    k_hi = k_peak
    while k_hi < len(mask) - 1 and mask[k_hi + 1]:
        k_hi += 1

    f_center = f[k_peak]
    f_lo = f[k_lo]
    f_hi = f[k_hi]
    return f, mag_db, float(f_center), float(f_lo), float(f_hi)


def design_auto_filter(fs, f_lo, f_hi, guard_factor=1.3, order=6):
    nyq = fs / 2.0
    bw = max(1.0, (f_hi - f_lo))
    fp1 = max(0.0, f_lo - (guard_factor - 1.0) * bw / 2.0)
    fp2 = min(nyq * 0.999, f_hi + (guard_factor - 1.0) * bw / 2.0)

    if fp1 <= 0.0 and fp2 < nyq:
        sos = butter(order, fp2 / nyq, btype="lowpass", output="sos")
        ftype = "lowpass"
    elif fp2 >= nyq and fp1 > 0.0:
        sos = butter(order, fp1 / nyq, btype="highpass", output="sos")
        ftype = "highpass"
    elif fp1 <= 0.0 and fp2 >= nyq:
        sos = None
        ftype = "none"
    else:
        sos = butter(order, [fp1 / nyq, fp2 / nyq], btype="bandpass", output="sos")
        ftype = "bandpass"
    return sos, ftype, (fp1, fp2)

# ================
#  Wygładzanie
# ================

def smooth_moving_avg(y, win_len):
    win_len = int(max(1, win_len))
    if win_len % 2 == 0:
        win_len += 1
    if win_len == 1:
        return y
    k = np.ones(win_len, dtype=np.float64) / win_len
    return np.convolve(y, k, mode="same")

# ======================
#  ms <-> km (2-way)
# ======================

def ms_to_km(ms, c_mps):
    return (c_mps * (ms / 1000.0) / 2.0) / 1000.0


def km_to_ms(km, c_mps):
    return (2.0 * (km * 1000.0) / c_mps) * 1000.0


def add_top_range_axis(ax, c_mps):
    secax = ax.secondary_xaxis('top',
        functions=(lambda ms: ms_to_km(ms, c_mps),
                   lambda km: km_to_ms(km, c_mps)))
    secax.set_xlabel("Effective height [km]")
    return secax

# =========================
#  Auto-ekstrakcja impulsu
# =========================

def auto_extract_probe(x, fs, win_ms=1.0, thr_ratio=6.0, thr_kmad=6.0, pad_ms=0.0,
                       min_ms=0.05, max_ms=20.0):
    eps = np.finfo(float).eps
    win_len = max(1, int(round(win_ms * 1e-3 * fs)))
    env = smooth_moving_avg(np.abs(x), win_len)

    med = np.median(env) + eps
    mad = np.median(np.abs(env - med)) + eps
    thr1 = med * thr_ratio
    thr2 = med + thr_kmad * mad
    thr = max(thr1, thr2)

    peaks, props = find_peaks(env, height=thr, distance=max(1, win_len // 2))
    if len(peaks) == 0:
        raise SystemExit("Auto-probe: nie znaleziono impulsu powyżej progu. Zmień --auto_*. ")

    p = int(peaks[0])
    peak_val = float(props['peak_heights'][0])
    boundary_level = med + 0.3 * (peak_val - med)

    a = p
    while a > 0 and env[a] > boundary_level:
        a -= 1
    b = p
    N = len(env)
    while b < N - 1 and env[b] > boundary_level:
        b += 1

    pad = int(round(pad_ms * 1e-3 * fs))
    a = max(0, a - pad)
    b = min(len(x), b + pad)

    min_len = max(1, int(round(min_ms * 1e-3 * fs)))
    max_len = max(min_len, int(round(max_ms * 1e-3 * fs)))

    cur_len = b - a
    if cur_len < min_len:
        extra = (min_len - cur_len)
        left_extra = extra // 2
        right_extra = extra - left_extra
        a = max(0, a - left_extra)
        b = min(len(x), b + right_extra)
    elif cur_len > max_len:
        cut = cur_len - max_len
        left_cut = cut // 2
        right_cut = cut - left_cut
        a += left_cut
        b -= right_cut

    h = x[a:b]
    if len(h) < 4:
        raise SystemExit("Auto-probe: impuls zbyt krótki.")

    return h, a, b, env, thr

# =====================================
#  Align/phase-lock w domenie MF
# =====================================

def _parabolic_subsample(yc, k):
    if not (1 <= k < len(yc) - 1):
        return 0.0
    eps = np.finfo(float).eps
    y1 = np.log(np.abs(yc[k-1]) + eps)
    y2 = np.log(np.abs(yc[k])   + eps)
    y3 = np.log(np.abs(yc[k+1]) + eps)
    denom = (y1 - 2.0*y2 + y3)
    if abs(denom) < 1e-12:
        return 0.0
    delta = 0.5 * (y1 - y3) / denom
    return float(np.clip(delta, -1.0, 1.0))


def _shift1d_interp_complex(y, shift):
    """Przesuń wektor y o 'shift' próbek (ujemny = w lewo), liniowo, z zerowaniem brzegów."""
    N = len(y)
    x = np.arange(N, dtype=np.float64)
    x_new = x + shift
    re = np.interp(x, x_new, y.real, left=0.0, right=0.0)
    im = np.interp(x, x_new, y.imag, left=0.0, right=0.0)
    return re + 1j * im

# =====================================
#  SZYBKA integracja koherentna (batch)
# =====================================

def coherent_batch_mean_fast(x_real, h_a, starts_samp, period_samp, fs, batch_size=64,
                              max_lag_ms=None, align=True, frac=True, return_traces=False):
    """
    Szybka wersja: liczymy MF raz na segment (batched FFT), a dosuw i blokadę fazy
    robimy na wektorach „pos” (odpowiedź dodatnich lagów), bez ponownego MF.
    Jeśli return_traces=True — zwracamy wyrównane przebiegi czasowe (kosztownie).
    """
    M = len(h_a)
    Lpos = int(period_samp)
    if max_lag_ms is not None:
        Lpos = min(Lpos, int(round(max_lag_ms * 1e-3 * fs)))
    if Lpos <= 0 or len(starts_samp) == 0:
        return None, None, 0, 0, None, None

    # Kernel MF
    hmf = np.conj(h_a[::-1])
    Lconv = period_samp + M - 1
    Lfft = next_pow2(Lconv)
    H = np.fft.fft(hmf, n=Lfft)

    mags = []
    used = 0
    B = int(max(1, batch_size))
    K = len(starts_samp)
    n_batches = (K + B - 1) // B

    traces_aligned = [] if return_traces else None

    for bi in range(n_batches):
        s = bi * B
        e = min(K, s + B)
        bsz = e - s
        # Zbierz segmenty (wejściowo real)
        Segs = np.zeros((bsz, Lfft), dtype=np.complex128)
        for i, st in enumerate(starts_samp[s:e]):
            seg_r = x_real[st:st+period_samp]
            Segs[i, :len(seg_r)] = seg_r   # ostatni impuls może być urwany na końcu pliku
        # Batched konwolucja
        X = np.fft.fft(Segs, n=Lfft, axis=1)
        C = np.fft.ifft(X * H[None, :], n=Lfft, axis=1)  # [bsz, Lconv]
        pos = C[:, M-1:M-1+Lpos]  # dodatnie lagi

        if align:
            for i in range(bsz):
                row = pos[i]
                k = int(np.argmax(np.abs(row)))
                d = _parabolic_subsample(row, k) if frac else 0.0
                shift = -(k + d)
                row_al = _shift1d_interp_complex(row, shift) if (frac or k != 0) else np.roll(row, int(shift))
                phi = np.angle(row_al[0])
                if np.isfinite(phi):
                    row_al *= np.exp(-1j * phi)
                pos[i] = row_al

                if return_traces:
                    seg_r = x_real[starts_samp[s+i]:starts_samp[s+i]+period_samp]
                    seg_a = hilbert(seg_r)
                    seg_a = _shift1d_interp_complex(seg_a, shift)
                    if np.isfinite(phi):
                        seg_a *= np.exp(-1j * phi)
                    traces_aligned.append(seg_a)

        coh_sum = np.sum(pos, axis=0)
        mags.append(np.abs(coh_sum))
        used += bsz

    mag_mean = np.mean(np.stack(mags, axis=0), axis=0)
    lags_s = np.arange(Lpos) / fs

    per_impulse_seg = np.array(traces_aligned, dtype=np.complex128) if return_traces and traces_aligned else None
    return lags_s, mag_mean, n_batches, used, None, per_impulse_seg

# ==============================
#  Czekanie aż plik się domknie
# ==============================

def _wav_header_ok(path: str) -> bool:
    try:
        with wave.open(path, 'rb') as w:
            _ = (w.getnchannels(), w.getframerate(), w.getsampwidth(), w.getnframes())
        return True
    except Exception:
        return False


def wait_for_file_complete(path: str, stable_ms: float = 500.0, check_ms: float = 200.0, verbose: bool = True):
    """Czeka, aż rozmiar pliku ustabilizuje się przez stable_ms. Dla WAV sprawdza nagłówek.
    Pętla bezterminowa – przerywasz Ctrl+C."""
    last_size = -1
    stable_for = 0.0
    ext = os.path.splitext(path)[1].lower()
    tick = max(1e-3, check_ms / 1000.0)
    stable_need = max(0.0, stable_ms)
    t_last_info = 0.0
    while True:
        try:
            size = os.path.getsize(path)
        except FileNotFoundError:
            size = -1
        if size > 0 and size == last_size:
            stable_for += check_ms
            if stable_for >= stable_need:
                if ext == ".wav":
                    if _wav_header_ok(path):
                        return
                    else:
                        stable_for = 0.0
                else:
                    return
        else:
            stable_for = 0.0
            last_size = size
        # status
        if verbose:
            t_last_info += tick
            if t_last_info >= 1.0:
                print(f"[wait] Plik w trakcie zapisu, rozmiar={size} B, stabilne={stable_for:.0f} ms…")
                t_last_info = 0.0
        time.sleep(tick)

# ============
#  PIPELINE
# ============

def process_one(long_path, args):
    # poczekaj aż plik się domknie (do skutku)
    if not getattr(args, 'no_file_wait', False):
        wait_for_file_complete(long_path, stable_ms=args.file_stable_ms, check_ms=args.file_check_ms, verbose=True)

    # 1) Wejście
    fs1, x = load_1d(long_path, args.fs, stereo_avg=args.stereo_avg,
                     swapIQ=args.swapIQ, invertQ=args.invertQ)
    x = dc_norm(x)
    fs = fs1

    # 2) Impuls
    if args.probe is not None and os.path.isfile(str(args.probe)):
        fs2, h = load_1d(args.probe, args.fs, stereo_avg=args.stereo_avg,
                         swapIQ=args.swapIQ, invertQ=args.invertQ)
        if abs(fs1 - fs2) > 1e-6:
            raise SystemExit(f"Fs niezgodny: {fs1} vs {fs2}")
        h = dc_norm(h)
        auto_info = None
    else:
        h, a_idx, b_idx, env, thr_used = auto_extract_probe(
            x, fs,
            win_ms=args.auto_win_ms,
            thr_ratio=args.auto_thr_ratio,
            thr_kmad=args.auto_thr_kmad,
            pad_ms=args.auto_pad_ms,
            min_ms=args.auto_min_ms,
            max_ms=args.auto_max_ms,
        )
        auto_info = (a_idx, b_idx, env, thr_used)

    if len(h) > len(x):
        raise SystemExit("Impuls dłuższy niż sygnał.")

    out_dir = os.path.dirname(os.path.abspath(long_path)) or "."
    stem = os.path.splitext(os.path.basename(long_path))[0]

    # DEBUG: impuls
    if auto_info is not None and args.debug:
        t_imp_ms = np.arange(len(h)) * 1000.0 / fs
        plt.figure(figsize=(12, 3.6))
        plt.title("Impuls wyizolowany automatycznie (z pierwszego piku)")
        plt.plot(t_imp_ms, h)
        plt.xlabel("Czas [ms]")
        plt.ylabel("Amplituda")
        plt.grid(alpha=0.2)
        plt.tight_layout()
        plt.savefig(os.path.join(out_dir, f"{stem}_impulse.png"), dpi=150)

    # 3) Widmo + filtr
    f, mag_db, f_center, f_lo, f_hi = probe_spectrum(h, fs, nfft_factor=4, bw_thresh_db=args.bw_thresh_db)
    sos, ftype, (fp1, fp2) = design_auto_filter(fs, f_lo, f_hi, guard_factor=args.guard_factor, order=args.filt_order)

    print("=== ANALIZA IMPULSU ===")
    if args.no_filter:
        sos = None
        ftype = "none"
    print(f"f_center ≈ {f_center:.2f} Hz")
    print(f"Pasmo (peak-{args.bw_thresh_db:.1f} dB): [{f_lo:.1f}, {f_hi:.1f}] Hz  (BW≈{(f_hi - f_lo):.1f} Hz)")
    if args.no_filter:
        print("Filtr: wyłączony (--no_filter)")
    elif sos is None:
        print("Filtr: pominięty (pasmo pokrywa całe Nyquista).")
    else:
        print(f"Filtr: {ftype}, rząd={args.filt_order}, projekt ~ [{fp1:.1f}, {fp2:.1f}] Hz, guard×{args.guard_factor:.2f}")

    # 4) Filtracja
    def sosfiltfilt_safe(sos, sig, name="sygnał"):
        if sos is None:
            return sig
        try:
            return sosfiltfilt(sos, sig)
        except ValueError as e:
            for frac in (0.75, 0.5, 0.33, 0.25):
                padlen = int(max(1, min(len(sig)-1, round(frac * len(sig)))))
                try:
                    return sosfiltfilt(sos, sig, padlen=padlen)
                except ValueError:
                    continue
            print(f"Uwaga: {name} zbyt krótki dla filtru (filtfilt). Pozostawiam bez filtracji. {e}")
            return sig

    if sos is not None:
        x_f = sosfiltfilt_safe(sos, x, name="długi sygnał")
        h_f = h if args.no_filter_probe else sosfiltfilt_safe(sos, h, name="impuls")
    else:
        x_f, h_f = x, h

    # 5) Globalna korelacja
    h_a = hilbert(h_f)  # tylko impuls analityczny
    corr_global = cross_corr(x_f, h_a, use_oa=not args.no_oa)
    M = len(h_a)
    peaks_idx, lags_s, thr = detect_impulses(corr_global, fs, M,
                                             holdoff_ms=args.holdoff_ms,
                                             thr_ratio=args.thr_ratio)
    N_all = len(peaks_idx)
    if N_all == 0:
        print("Brak detekcji powyżej progu po filtracji.")
        return

    t_first, t_last = lags_s[0], lags_s[-1]
    T = (t_last - t_first) / (N_all - 1) if N_all > 1 else np.nan
    period_samp = int(round(T * fs)) if np.isfinite(T) else len(h_f)
    starts_samp_all = np.round(lags_s * fs).astype(int)

    if args.use_first is not None:
        starts_samp = starts_samp_all[:max(0, int(args.use_first))]
    else:
        starts_samp = starts_samp_all

    # 6) Integracja koherentna (batched)
    lags_pos_s, mag_mean, n_batches, used, _pos_dbg, per_impulse_seg = coherent_batch_mean_fast(
        x_f.astype(np.complex128), h_a,
        starts_samp, period_samp, fs,
        batch_size=args.coh_batch,
        max_lag_ms=args.max_lag_ms,
        align=(not args.no_align),
        frac=(not args.no_frac),
        return_traces=args.show_traces
    )

    if mag_mean is None:
        print("Brak danych do integracji (used=0)")
        return

    # 6a) Overlay czasowy (opcjonalnie)
    if (per_impulse_seg is not None) and (args.debug or args.show_traces):
        B = int(max(1, args.coh_batch))
        K = per_impulse_seg.shape[0]
        total_batches = (K + B - 1) // B
        batches_to_plot = range(total_batches) if args.show_traces else range(min(1, total_batches))
        center_idx = (len(h_f) - 1)
        for bi in range(total_batches if args.show_traces else 1):
            s = bi * B
            e = min(K, s + B)
            batch = per_impulse_seg[s:e]
            L_here = batch.shape[1]
            t_ms = ((np.arange(L_here) - center_idx) / fs) * 1000.0
            plt.figure(figsize=(12, 5))
            ax = plt.gca()
            for i in range(batch.shape[0]):
                ax.plot(t_ms, np.real(batch[i]), alpha=0.35, linewidth=0.8)
            ax.set_title(f"Batch {bi+1} ({e - s} time traces, aligned + phase-locked)")
            ax.set_xlabel("Time [ms]")
            ax.set_ylabel("Amplitude")
            ax.grid(alpha=0.2)
            plt.tight_layout()
            plt.savefig(os.path.join(out_dir, f"{stem}_traces_time_b{bi+1:02d}.png"), dpi=150)

    # 7) Wygładzenie
    win_len = int(round((len(h_f) / 100.0) * args.smooth_scale))
    mag_sm = smooth_moving_avg(mag_mean, win_len) if not args.disable_smooth else mag_mean

    win_ms = (win_len / fs) * 1000.0
    print("=== POWTÓRZENIA IMPULSU (po filtracji) ===")
    print(f"Wykrytych łącznie: N_all = {N_all}")
    print(f"Zaplanowanych do użycia: {len(starts_samp)}")
    print(f"Użyte do integracji: {used} (paczki: {n_batches}, coh_batch: {args.coh_batch}, limit: {args.use_first if args.use_first is not None else 'brak'})")
    print(f"Czas pierwszego: {t_first:.6f} s ({t_first*1000:.3f} ms)")
    print(f"Czas ostatniego:  {t_last:.6f} s ({t_last*1000:.3f} ms)")
    if N_all > 1 and np.isfinite(T):
        print(f"Odstęp T = {T:.9f} s ({T*1000:.6f} ms)  ~ {period_samp} próbek")
    print(f"Wygładzanie: okno = {win_len} próbek ≈ {win_ms:.3f} ms (≈ 1/100 impulsu × scale={args.smooth_scale:.2f})")

    # 8) Wykres końcowy (z zakresem)
    lags_pos_ms = lags_pos_s * 1000.0
    eps = np.finfo(float).eps
    med = np.median(mag_sm) + eps
    coh_db = 20.0 * np.log10((mag_sm + eps) / med)

    plt.figure(figsize=(12, 5))
    ax = plt.gca()
    ax.set_title(f"N={used} (coh={args.coh_batch})")
    ax.plot(lags_pos_ms, coh_db)
    ax.set_xlabel("Delay [ms]")
    ax.set_ylabel("Correlation [dB]")
    add_top_range_axis(ax, args.c_mps)

    if not args.debug:
        x_min = km_to_ms(args.km_min, args.c_mps)
        x_max = km_to_ms(args.km_max, args.c_mps)
        ax.set_xlim(x_min, x_max)
        mask = (lags_pos_ms >= x_min) & (lags_pos_ms <= x_max)
        yseg = coh_db[mask] if np.any(mask) else coh_db
    else:
        yseg = coh_db

    yseg = yseg[np.isfinite(yseg)]
    if yseg.size:
        ymin = float(np.min(yseg)); ymax = float(np.max(yseg))
        span = ymax - ymin
        if span <= 1e-9:
            ax.set_ylim(ymin - 0.1, ymax + 1.0)
        else:
            pad = max(0.1, 0.05 * span)
            ax.set_ylim(ymin - pad, ymax + pad)

    ax.grid(alpha=0.2)
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, f"{stem}_coh.png"), dpi=150)


# ============
#  CLI
# ============

def main():
    ap = argparse.ArgumentParser(description=(
        "Widmo impulsu → filtracja → koherentna integracja (batch, align w MF) → wykres. "
        "Jeśli --probe pominięty, impuls jest auto-wycinany z pierwszego piku."
    ))
    ap.add_argument("--long", default=None, help="Długi sygnał (wav/npy/txt/csv) lub katalog; gdy pominięte lub katalog — watch & batch po *.wav")
    ap.add_argument("--probe", required=False, default=None, help="Impuls sondujący (wav/npy/txt/csv) — opcjonalne, gdy AUTO")
    ap.add_argument("--fs", type=float, default=None, help="Fs [Hz] (dla plików nie-wav)")

    # WAV stereo -> I/Q domyślnie
    ap.add_argument("--stereo-avg", dest="stereo_avg", action="store_true",
                    help="Zamiast traktować stereo jako I/Q, uśrednij L/R do mono.")
    ap.add_argument("--swapIQ", action="store_true", help="Zamień kanały I<->Q (dla WAV stereo).")
    ap.add_argument("--invertQ", action="store_true", help="Odwróć znak kanału Q (dla WAV stereo).")

    # detekcja
    ap.add_argument("--holdoff_ms", type=float, default=1.0, help="Holdoff po detekcji piku [ms]")
    ap.add_argument("--thr_ratio", type=float, default=0.5, help="Próg = thr_ratio * max(|C|)")
    ap.add_argument("--max_lag_ms", type=float, default=None,
                    help="Ogranicz dodatnie lagi do 0..max_lag_ms w integracji (przyspiesza)")

    # parametry propagacji (oś km)
    ap.add_argument("--c_mps", type=float, default=299_792_458.0,
                    help="Prędkość propagacji [m/s] dla osi 'km'")
    ap.add_argument("--km_min", type=float, default=100.0, help="Lewy kraniec zakresu [km] (gdy brak --debug)")
    ap.add_argument("--km_max", type=float, default=650.0, help="Prawy kraniec zakresu [km] (gdy brak --debug)")

    # analiza / filtr
    ap.add_argument("--bw_thresh_db", type=float, default=20.0,
                    help="Definicja pasma: poziom ≥ (peak - bw_thresh_db) [dB]")
    ap.add_argument("--guard_factor", type=float, default=1.3,
                    help="Mnożnik pasma na margines filtru (np. 1.3)")
    ap.add_argument("--filt_order", type=int, default=6, help="Rząd filtru Butterwortha")
    ap.add_argument("--no_filter_probe", action="store_true",
                    help="Nie filtruj impulsu (domyślnie filtruję tylko długi sygnał).")
    ap.add_argument("--no_filter", action="store_true",
                    help="CAŁKOWICIE wyłącz filtrację (ani długiego sygnału, ani impulsu)")

    # AUTO-probe
    ap.add_argument("--auto_win_ms", type=float, default=1.0, help="Okno MA obwiedni [ms] dla AUTO-probe")
    ap.add_argument("--auto_thr_ratio", type=float, default=6.0, help="Próg = max(med*ratio, med + k*MAD)")
    ap.add_argument("--auto_thr_kmad", type=float, default=6.0, help="k w med + k*MAD dla AUTO-probe")
    ap.add_argument("--auto_pad_ms", type=float, default=0.0, help="Dodatkowy margines dookoła impulsu [ms]")
    ap.add_argument("--auto_min_ms", type=float, default=0.05, help="Minimalna długość impulsu [ms]")
    ap.add_argument("--auto_max_ms", type=float, default=20.0, help="Maksymalna długość impulsu [ms]")

    # Integracja koherentna
    ap.add_argument("--coh_batch", type=int, default=64, help="Rozmiar paczki do koherentnej sumy (np. 64)")
    ap.add_argument("--no_align", action="store_true", help="Wyłącz dosuw i blokadę fazy (najszybciej)")
    ap.add_argument("--no_frac", action="store_true", help="Wyłącz frakcyjny dosuw (zostaje integer align)")
    ap.add_argument("--no_oa", action="store_true", help="Wyłącz OA-convolve (użyj zwykłej fftconvolve)")

    # Wygładzanie
    ap.add_argument("--smooth_scale", type=float, default=1.0,
                    help="Skaluj szerokość okna wygładzania (1.0 ≈ 1/100 długości impulsu).")
    ap.add_argument("--disable_smooth", action="store_true",
                    help="Wyłącz wygładzanie krzywej wynikowej (najszybciej)")

    # Overlay czasowy
    ap.add_argument("--show_traces", action="store_true", help="Rysuj overlay wyrównanych przebiegów czasowych (wolniej, więcej RAM)")

    # Ograniczenia/ilości
    ap.add_argument("--use_first", type=int, default=None, help="Użyj tylko pierwszych K wykrytych impulsów (np. 2048)")

    # UX / tryb katalogowy
    ap.add_argument("--debug", action="store_true", help="Dodatkowe wykresy diagnostyczne; wyłącza ograniczenie zakresu osi X")
    ap.add_argument("--keep", action="store_true", help="Nie usuwaj przetworzonych plików .wav w trybie watch")

    # Retry / czekanie na domknięcie pliku
    ap.add_argument("--retries", type=int, default=1,
                    help="Liczba natychmiastowych powtórek process_one po wyjątku (single-file)")
    ap.add_argument("--file_stable_ms", type=float, default=500.0,
                    help="Ile ms musi być stały rozmiar pliku zanim zaczniemy przetwarzanie")
    ap.add_argument("--file_check_ms", type=float, default=200.0,
                    help="Co ile ms sprawdzać rozmiar pliku")
    ap.add_argument("--no_file_wait", action="store_true",
                    help="Nie czekaj na stabilizację pliku - próbuj od razu (dla eksperymentów)")

    args = ap.parse_args()

    # --- Watch mode ---
    if args.long is None or os.path.isdir(str(args.long)):
        base_dir = str(args.long) if args.long else os.getcwd()
        print("Watch mode: oczekuję na pliki .wav w:", base_dir)
        print("Ctrl+C aby zakończyć.")
        try:
            seen = set()
            while True:
                wavs = sorted(glob(os.path.join(base_dir, "*.wav")))
                new = [w for w in wavs if w not in seen]
                if new:
                    for path in new:
                        print(f"\n=== PRZETWARZANIE: {path} ===")
                        # pętla bezterminowa do skutku (aż się uda przetworzyć)
                        while True:
                            try:
                                process_one(path, args)
                                if not args.keep:
                                    try:
                                        os.remove(path)
                                        print(f"Usunięto: {path}")
                                    except Exception as e:
                                        print(f"Uwaga: nie udało się usunąć {path}: {e}")
                                else:
                                    print(f"Zachowano plik (--keep): {path}")
                                break  # sukces -> wyjdź z pętli prób
                            except SystemExit as e:
                                print(f"Pominięto {path}: {e}")
                                break
                            except Exception as e:
                                print(f"Błąd przy {path} (retry): {e}")
                                time.sleep(0.2)
                                continue
                        seen.add(path)
                    plt.show(); plt.close('all')
                else:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            print("Zakończono (Ctrl+C).")
    else:
        print(f"=== PRZETWARZANIE: {args.long} ===")
        attempts = max(1, int(args.retries) + 1)
        for i in range(attempts):
            try:
                process_one(args.long, args)
                break
            except Exception as e:
                print(f"Błąd (próba {i+1}/{attempts}): {e}")
                if i < attempts - 1:
                    time.sleep(0.2)
                    continue
                else:
                    raise
        plt.show()


if __name__ == "__main__":
    main()


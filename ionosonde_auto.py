#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Automatic ionosonde runner: TX (Pico) -> RX (RTL-SDR) -> correlation analysis.

One cycle:
  1. push TX parameters to the Pico over USB serial (SET ...)
  2. tune the RTL-SDR OFFSET below the carrier, so the sounding signal does NOT
     sit on the DC spike of the receiver (default: carrier - 50 kHz)
  3. start the RTL-SDR stream (direct sampling, Q-branch = HF input of RTL-SDR v3)
  4. fire TX_ONCE and record the whole frame as a stereo I/Q WAV
  5. run corr/radar_corr_autoprobe.py on the recording -> *_coh.png

Then it repeats: same frequency or a whole list (--freqs 3.655,7.022,...).

Typical use:
    python3 ionosonde_auto.py                      # 7.022 MHz, forever
    python3 ionosonde_auto.py --freqs 3.655,7.022 --cycles 20
    python3 ionosonde_auto.py --no-tx              # listen only, check the dongle

Requires: numpy, scipy, matplotlib, pyserial, SoapySDR (python3-soapysdr +
soapysdr-module-rtlsdr). The DVB-T kernel driver must NOT hold the dongle
(see the hint printed on open failure).
"""

import argparse
import json
import os
import re
import collections
import shlex
import shutil
import subprocess
import sys
import threading
import time
import wave
from datetime import datetime

import numpy as np

try:
    import SoapySDR
    from SoapySDR import (SOAPY_SDR_RX, SOAPY_SDR_CS16,
                          SOAPY_SDR_OVERFLOW, SOAPY_SDR_TIMEOUT)
except ImportError:  # reported later, only when the SDR is actually needed
    SoapySDR = None
    SOAPY_SDR_RX, SOAPY_SDR_CS16 = 1, "CS16"
    SOAPY_SDR_OVERFLOW, SOAPY_SDR_TIMEOUT = -4, -1

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ANALYZE_ARGS = "--coh_batch 256"
# librtlsdr accepts only these sample rate windows
RTL_RATE_RANGES = ((225001, 300000), (900001, 3200000))

_LOG_FH = None


def log(msg):
    line = f"[{time.strftime('%H:%M:%SZ', time.gmtime())}] {msg}"
    print(line, flush=True)
    if _LOG_FH:
        _LOG_FH.write(line + "\n")
        _LOG_FH.flush()


def progress(text="", seconds=None, done=None, total=None):
    """Report a long operation. The GUI replaces this to drive its progress bar.

    seconds  - the operation has a known duration (transmitting a frame, waiting)
    done/total - discrete steps (sweep position)
    neither  - busy with an open-ended job; empty text clears the bar.
    """
    pass


# =========================================================
#  Audible alarm - a link that dies unattended is otherwise silent
# =========================================================

class Beeper:
    """Repeating beep while the transmitter is missing.

    An unattended sounder that loses its Pico keeps looking busy: the sweep runs,
    the bars move, and every capture is noise. The point of this is to be heard
    from the next room, so it keeps beeping until the link is back rather than
    chirping once.
    """

    PLAYERS = (("paplay", []), ("aplay", ["-q"]), ("pw-play", []))

    def __init__(self, enabled=True, freq_hz=880.0, beep_s=0.18, gap_s=0.55):
        self.enabled = bool(enabled)
        self.freq_hz, self.beep_s, self.gap_s = freq_hz, beep_s, gap_s
        self._thread = None
        self._stop = threading.Event()
        self._wav = None
        self._cmd = None
        self.reason = ""

    # -- one short tone on disk, played over and over
    def _tone_file(self):
        if self._wav and os.path.exists(self._wav):
            return self._wav
        import tempfile
        fs = 44100
        n = int(fs * self.beep_s)
        t = np.arange(n) / fs
        env = np.minimum(1.0, np.minimum(t, self.beep_s - t) / 0.008)   # no clicks
        pcm = (0.6 * env * np.sin(2 * np.pi * self.freq_hz * t) * 32767).astype("<i2")
        fd, path = tempfile.mkstemp(prefix="ionosonde_beep_", suffix=".wav")
        os.close(fd)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(fs)
            w.writeframes(pcm.tobytes())
        self._wav = path
        return path

    def _player_cmd(self):
        if self._cmd is not None:
            return self._cmd
        for exe, args in self.PLAYERS:
            path = shutil.which(exe)
            if path:
                self._cmd = [path] + args
                break
        else:
            self._cmd = []
        return self._cmd

    def _loop(self):
        cmd = self._player_cmd()
        wav = None
        if cmd:
            try:
                wav = self._tone_file()
            except Exception:
                cmd = []
        while not self._stop.is_set():
            try:
                if cmd and wav:
                    subprocess.run(cmd + [wav], timeout=5,
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
                else:
                    sys.stderr.write("\a")     # terminal bell, last resort
                    sys.stderr.flush()
            except Exception:
                pass
            self._stop.wait(self.gap_s)

    def start(self, reason=""):
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self.reason = reason
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="beeper")
        self._thread.start()
        log(f"   *** ALARM: {reason} - beeping until it is back ***")

    def stop(self, note=""):
        if not (self._thread and self._thread.is_alive()):
            return
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._thread = None
        if note:
            log(f"   alarm off: {note}")

    @property
    def active(self):
        return bool(self._thread and self._thread.is_alive())


ALARM = Beeper()


# =========================================================
#  TX side - Raspberry Pi Pico over USB serial
# =========================================================

PICO_VID = 0x2E8A          # Raspberry Pi (RP2040 USB CDC)


def find_serial_port(preferred=None, quiet=False):
    """Pick the Pico's USB CDC port.

    pyserial also reports the 32 legacy /dev/ttyS* 16550 ports, which are not
    real devices here - opening one fails with EIO, so they are never picked
    automatically (pass --port explicitly if you really want one).
    """
    try:
        from serial.tools import list_ports
    except ImportError as e:
        raise SystemExit("pyserial missing: pip install pyserial") from e

    ports = list(list_ports.comports())
    if preferred:
        if any(p.device == preferred for p in ports) or os.path.exists(preferred):
            return preferred
        raise SystemExit(f"Serial port {preferred} not found. Seen: "
                         f"{[p.device for p in ports] or 'none'}")

    def score(p):
        if p.vid == PICO_VID:
            return 0                                  # certainly a Pico
        if "ttyACM" in p.device:
            return 1                                  # some other USB CDC device
        if p.vid is not None:
            return 2                                  # USB serial adapter
        return 99                                     # /dev/ttyS* placeholder

    cand = sorted((p for p in ports if score(p) < 99), key=lambda p: (score(p), p.device))
    if not cand:
        raise SystemExit(
            "No USB serial device found - the Pico does not seem to be plugged in "
            "(no /dev/ttyACM*).\n"
            "  - plug it in and check with: ls /dev/ttyACM*\n"
            "  - or run receive-only:       python3 ionosonde_auto.py --no-tx\n"
            "  - or force a port:           python3 ionosonde_auto.py --port /dev/ttyACM0")
    if not quiet:
        best = cand[0]
        log(f"TX: using {best.device} ({best.description or 'no description'})")
        if len(cand) > 1:
            log(f"TX: other candidates: {[p.device for p in cand[1:]]}")
    return cand[0].device


def usb_device_node(tty_path):
    """/dev/ttyACM0 -> /dev/bus/usb/BBB/DDD for the board behind it."""
    name = os.path.basename(os.path.realpath(tty_path))
    d = os.path.realpath(f"/sys/class/tty/{name}/device")
    for _ in range(8):
        d = os.path.dirname(d)
        if not d or d == "/":
            break
        if os.path.exists(os.path.join(d, "busnum")):
            with open(os.path.join(d, "busnum")) as f:
                bus = int(f.read())
            with open(os.path.join(d, "devnum")) as f:
                dev = int(f.read())
            return f"/dev/bus/usb/{bus:03d}/{dev:03d}"
    return None


def usb_reset(tty_path):
    """Force a USB bus reset on the board, which re-enumerates its CDC port.

    This is the only reset available without extra tools: the 1200-baud touch
    would drop a stock pico-sdk build into BOOTSEL and take the sounder off the
    air until it is reflashed.
    """
    import fcntl
    node = usb_device_node(tty_path)
    if not node or not os.path.exists(node):
        return False, "USB device node not found"
    try:
        fd = os.open(node, os.O_WRONLY)
    except OSError as e:
        return False, (f"cannot open {node}: {e} - add a udev rule for VID 2e8a "
                       f'(MODE="0666") or run as root')
    try:
        fcntl.ioctl(fd, 0x5514, 0)          # USBDEVFS_RESET
        return True, node
    except OSError as e:
        return False, f"ioctl failed: {e}"
    finally:
        os.close(fd)


class PicoTx:
    """Thin wrapper over the firmware's line protocol (see main.c)."""

    def __init__(self, port, baud=115200, echo=False, requested=None):
        try:
            import serial
        except ImportError as e:
            raise SystemExit("pyserial missing: pip install pyserial") from e
        self.port = port
        self.baud = baud
        self.requested = requested          # what the user asked for (may be None)
        self.echo = echo
        self._buf = b""
        self.last_lines = []
        self.ok = True
        try:
            # write_timeout matters: the firmware does not read the USB CDC while
            # it transmits, so a write issued mid-frame would block forever.
            self.ser = serial.Serial(port, baud, timeout=0.05, write_timeout=3.0)
        except Exception as e:
            raise SystemExit(
                f"Cannot open {port}: {e}\n"
                "  - is this really the Pico? check: ls -l /dev/ttyACM*\n"
                "  - permission denied? add yourself to the dialout group:\n"
                "        sudo usermod -aG dialout $USER   (then log out and back in)\n"
                "  - or run receive-only: python3 ionosonde_auto.py --no-tx")

    def reconnect(self, timeout=60.0):
        """USB CDC can drop (Pico reset, replug) - come back on our own."""
        import serial
        self.close()
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                port = find_serial_port(self.requested, quiet=True)
                self.ser = serial.Serial(port, self.baud, timeout=0.05,
                                         write_timeout=3.0)
                self.port = port
                self._buf = b""
                self.ok = True
                self.drain(0.5)
                return True
            except (Exception, SystemExit):
                # find_serial_port exits when the port is missing, which is the
                # normal case while we wait for the Pico to come back
                time.sleep(2.0)
        return False

    def alive(self, timeout=1.5):
        """Is the firmware still answering? Call between frames, never during one.

        Checks the device node first: when the Pico is unplugged or crashes off
        the bus, /dev/ttyACM* disappears while an already-open handle can keep
        accepting writes, so a silent link looks exactly like a healthy one.
        """
        if not self.ok:
            return False
        if not os.path.exists(self.port):
            self.ok = False
            return False
        try:
            self.poll()
            self.send("STATUS", quiet=True)
        except Exception:
            self.ok = False
            return False
        answer = self.wait_for([["Ionosonde Status"], ["Frequency:"],
                                ["Chip Count"]], timeout)
        if answer is None:
            self.ok = False
        return answer is not None

    def recover(self, timeout=120.0):
        """Bring the link back, escalating until the deadline.

        Reopening the port fixes a stale handle; a USB bus reset re-enumerates a
        board whose CDC has wedged; picotool, when installed, reboots the
        firmware itself. A 1200-baud touch is deliberately NOT used - on a stock
        pico-sdk build it drops the board into BOOTSEL and takes the sounder off
        the air until it is reflashed.
        """
        import shutil
        log(f"   TX: link down - recovering (up to {timeout:.0f} s)")
        deadline = time.time() + timeout
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            # 1. plain reopen - covers unplug/replug and a stale file handle
            if self.reconnect(timeout=min(15.0, max(3.0, deadline - time.time()))) \
                    and self.alive():
                log(f"   TX: link restored (attempt {attempt}: reopen)")
                return True
            if time.time() >= deadline:
                break
            # 2. the port is there but the firmware is silent - reset the board
            port = self.port if os.path.exists(self.port) else None
            if port:
                ok, info = usb_reset(port)
                log(f"   TX: USB reset {'sent' if ok else 'failed'} ({info})")
                if ok:
                    time.sleep(3.0)
                    if self.reconnect(timeout=15.0) and self.alive():
                        log(f"   TX: link restored (attempt {attempt}: USB reset)")
                        return True
            # 3. a real firmware reboot, if picotool is around
            if shutil.which("picotool"):
                log("   TX: asking picotool to reboot the Pico")
                try:
                    subprocess.run(["picotool", "reboot", "-f"], timeout=10,
                                   capture_output=True)
                except Exception as e:
                    log(f"   TX: picotool failed: {e}")
                time.sleep(3.0)
                if self.reconnect(timeout=15.0) and self.alive():
                    log(f"   TX: link restored (attempt {attempt}: picotool)")
                    return True
            time.sleep(2.0)
        log("   TX: could not recover the link")
        return False

    def poll(self):
        """Return complete lines received so far (non-blocking)."""
        out = []
        try:
            data = self.ser.read(4096)
        except Exception as e:
            if self.ok:                     # report the break once, not per poll
                log(f"serial read error: {e}")
            self.ok = False
            return out
        if data:
            self._buf += data
            while b"\n" in self._buf:
                raw, self._buf = self._buf.split(b"\n", 1)
                s = raw.decode("utf-8", "ignore").strip()
                if s:
                    out.append(s)
                    self.last_lines.append(s)
                    if self.echo:
                        log(f"   pico| {s}")
        return out

    def drain(self, seconds=0.3):
        t0 = time.time()
        while time.time() - t0 < seconds:
            self.poll()
            time.sleep(0.02)

    def send(self, cmd, quiet=False):
        try:
            self.ser.write((cmd + "\n").encode())
            self.ser.flush()
        except Exception:
            self.ok = False
            raise
        if not quiet:
            log(f"   >>> {cmd}")

    def wait_for(self, groups, timeout):
        """Wait for a line containing every substring of any group."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            for s in self.poll():
                for g in groups:
                    if all(n in s for n in g):
                        return s
            time.sleep(0.02)
        return None

    def set_params(self, freq_mhz, cfg):
        self.send("SET FREQ={:.6f},BIT={:.1f},AMP={:.2f},MOD={},CHIPS={},S2S={}".format(
            freq_mhz, cfg.bit_us, cfg.amp / 100.0, cfg.mod, cfg.chips, cfg.s2s))
        self.drain(0.4)

    def stop_auto(self):
        try:
            self.send("TX_STOP")
            self.drain(0.2)
        except Exception:
            pass

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


# =========================================================
#  RX side - RTL-SDR via SoapySDR, direct sampling
# =========================================================

def dvb_driver_hint():
    """The classic 'usb_claim_interface error -6' cause on Linux."""
    try:
        with open("/proc/modules") as f:
            mods = f.read()
    except OSError:
        return ""
    if "dvb_usb_rtl28xxu" not in mods and "rtl2832_sdr" not in mods:
        return ""
    return ("\nThe DVB-T kernel driver is bound to the dongle. Free it with:\n"
            "    sudo modprobe -r dvb_usb_rtl28xxu rtl2832_sdr rtl2832\n"
            "and make it permanent:\n"
            "    printf 'blacklist dvb_usb_rtl28xxu\\nblacklist rtl2832_sdr\\n"
            "blacklist rtl2832\\n' | sudo tee /etc/modprobe.d/blacklist-rtl.conf\n"
            "    sudo rmmod dvb_usb_rtl28xxu rtl2832_sdr rtl2832   # or replug the dongle\n")


class RtlRx:
    def __init__(self, device_args="driver=rtlsdr", rate=250000, direct_samp=2,
                 gain=None, agc=False, ppm=0.0, digital_agc=False):
        self.device_args = device_args
        self.rate = float(rate)
        self.direct_samp = int(direct_samp)
        self.gain = gain
        self.agc = agc
        self.digital_agc = digital_agc
        self.ppm = ppm
        self.dev = None
        self.stream = None
        self.fs = None
        self.center = None

    def open(self):
        if SoapySDR is None:
            raise SystemExit("SoapySDR python module missing: "
                             "sudo apt install python3-soapysdr soapysdr-module-rtlsdr")
        args = dict(kv.split("=", 1) for kv in self.device_args.split(",") if "=" in kv)
        try:
            self.dev = SoapySDR.Device(args)
        except Exception as e:
            raise SystemExit(f"Cannot open SDR ({self.device_args}): {e}{dvb_driver_hint()}")

        # direct sampling first - it changes how tuning is applied inside librtlsdr
        try:
            self.dev.writeSetting("direct_samp", str(self.direct_samp))
            got = self.dev.readSetting("direct_samp")
            log(f"RX: direct_samp={got} "
                f"({'Q-branch' if str(got) in ('2', 'Q-ADC') else 'CHECK THIS'})")
        except Exception as e:
            log(f"RX: WARNING - direct_samp not applied: {e}")

        self.dev.setSampleRate(SOAPY_SDR_RX, 0, self.rate)
        self.fs = float(self.dev.getSampleRate(SOAPY_SDR_RX, 0))

        try:
            self.dev.setGainMode(SOAPY_SDR_RX, 0, bool(self.agc))
        except Exception:
            pass
        if self.gain is not None:
            try:
                self.dev.setGain(SOAPY_SDR_RX, 0, float(self.gain))
                if self.direct_samp:
                    log(f"RX: WARNING - tuner gain {self.gain:.1f} dB has no effect "
                        "in direct sampling: the R820T is bypassed, the ADC input "
                        "is wired straight to the antenna. Use an external "
                        "attenuator/preamp, or --digital-agc.")
                else:
                    log(f"RX: tuner gain {self.dev.getGain(SOAPY_SDR_RX, 0):.1f} dB")
            except Exception as e:
                log(f"RX: gain not settable ({e}) - ignored")

        # The RTL2832's own digital AGC lives after the ADC, so unlike the tuner
        # gain it does work in direct sampling. It rescales digital samples - it
        # cannot undo ADC clipping and its steps blur coherent integration.
        try:
            self.dev.writeSetting("digital_agc", "true" if self.digital_agc else "false")
            if self.digital_agc:
                log("RX: RTL2832 digital AGC ON (level only, no extra dynamic range)")
        except Exception as e:
            log(f"RX: digital AGC not available: {e}")

        self.stream = self.dev.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CS16)
        log(f"RX: rate={self.fs/1e3:.1f} kS/s (requested {self.rate/1e3:.1f})")

    def tune(self, hz):
        self.dev.setFrequency(SOAPY_SDR_RX, 0, float(hz))
        self.center = float(self.dev.getFrequency(SOAPY_SDR_RX, 0))
        if abs(self.center - hz) > 1.0:
            log(f"RX: WARNING - asked {hz/1e6:.6f} MHz, tuned {self.center/1e6:.6f} MHz")
        return self.center

    def activate(self):
        self.dev.activateStream(self.stream)

    def deactivate(self):
        try:
            self.dev.deactivateStream(self.stream)
        except Exception:
            pass

    def close(self):
        if self.dev and self.stream:
            self.deactivate()
            try:
                self.dev.closeStream(self.stream)
            except Exception:
                pass
        self.stream = None
        self.dev = None


class Recorder(threading.Thread):
    """Pulls CS16 samples off the stream into memory (interleaved I,Q int16)."""

    def __init__(self, rx, chunk=65536, max_samples=None):
        super().__init__(daemon=True)
        self.rx = rx
        self.chunk = int(chunk)
        self.max_samples = max_samples
        self._chunks = []
        self._stop_evt = threading.Event()   # not _stop: Thread uses that name
        self.samples = 0
        self.overflows = 0
        self.timeouts = 0
        self.errors = []

    def run(self):
        buf = np.empty(self.chunk * 2, np.int16)
        while not self._stop_evt.is_set():
            try:
                sr = self.rx.dev.readStream(self.rx.stream, [buf], self.chunk,
                                            timeoutUs=500000)
            except Exception as e:
                self.errors.append(str(e))
                break
            n = sr.ret
            if n > 0:
                self._chunks.append(buf[:2 * n].copy())
                self.samples += n
                if self.max_samples and self.samples >= self.max_samples:
                    break
            elif n == SOAPY_SDR_OVERFLOW:
                self.overflows += 1
            elif n == SOAPY_SDR_TIMEOUT:
                self.timeouts += 1
            elif n < 0:
                self.errors.append(f"readStream={n}")

    def stop(self):
        self._stop_evt.set()
        self.join(timeout=3.0)

    def data(self):
        if not self._chunks:
            return np.empty(0, np.int16)
        return np.concatenate(self._chunks)


# =========================================================
#  Signal checks on the raw capture
# =========================================================

def spectrum_stats(iq_int16, fs, offset_hz, bw_hz, nfft=8192, max_blocks=64):
    """Average |FFT|^2 and return band powers at +offset / -offset plus noise.

    The TX signal is at +offset_hz relative to the tuned center. If the dongle
    delivered an inverted spectrum, the energy would show up at -offset_hz.
    Measured on this dongle (RTL2832U, direct sampling Q-branch): the sense is
    normal - retuning +20 kHz moved a carrier from -62 to -82 kHz - so the
    inversion fix is off by default and only used with --iq-sense auto/invert.
    """
    v = iq_int16.reshape(-1, 2)
    n_avail = v.shape[0]
    if n_avail < nfft:
        return None
    nblocks = min(max_blocks, n_avail // nfft)
    start = max(0, (n_avail - nblocks * nfft) // 2)   # middle of the capture
    z = (v[start:start + nblocks * nfft, 0].astype(np.float32)
         + 1j * v[start:start + nblocks * nfft, 1].astype(np.float32))
    z = z.reshape(nblocks, nfft)
    z = z - z.mean(axis=1, keepdims=True)             # kill the DC offset
    win = np.hanning(nfft).astype(np.float32)
    psd = np.abs(np.fft.fft(z * win, axis=1)) ** 2
    psd = psd.mean(axis=0)
    f = np.fft.fftfreq(nfft, d=1.0 / fs)

    half = max(bw_hz / 2.0, fs / nfft * 2)
    m_pos = np.abs(f - offset_hz) <= half
    m_neg = np.abs(f + offset_hz) <= half
    m_noise = ~(m_pos | m_neg) & (np.abs(f) > 3 * half)
    eps = np.finfo(np.float32).eps
    return {
        "pos": float(psd[m_pos].mean() + eps),
        "neg": float(psd[m_neg].mean() + eps),
        "noise": float(np.median(psd[m_noise]) + eps) if m_noise.any() else eps,
        "psd": psd, "f": f,
    }


def capture_quality(iq_int16):
    v = iq_int16.reshape(-1, 2).astype(np.float32)
    peak = float(np.abs(v).max()) if v.size else 0.0
    rms = float(np.sqrt((v ** 2).mean())) if v.size else 0.0
    clip = float((np.abs(v) >= 32000).mean()) if v.size else 0.0
    return peak, rms, clip


def conjugate_q(iq_int16):
    v = iq_int16.reshape(-1, 2)
    np.clip(v[:, 1], -32767, 32767, out=v[:, 1])
    v[:, 1] = -v[:, 1]
    return iq_int16


def write_iq_wav(path, iq_int16, fs):
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(int(round(fs)))
        w.writeframes(iq_int16.astype("<i2", copy=False).tobytes())


# =========================================================
#  Analysis
# =========================================================

def run_analysis(wav_path, cfg, meta=None):
    """Isolate the sounding band, track the pulse train, integrate, plot."""
    import ionogram as ig
    progress(text="analysing capture")
    t0 = time.time()
    try:
        png, info = ig.analyse_capture(wav_path, cfg.analyze_args, meta,
                                       cfg.ion_km_min, cfg.ion_km_max,
                                       cfg.detrend_km)
    except Exception as e:
        log(f"   analysis FAILED: {e}")
        return False, str(e)
    log(f"   {info['n_found']}/{info['n_expected']} pulses tracked "
        f"({info.get('detected', info['n_found'])} detected, "
        f"{info['coasted']} coasted), "
        f"interval {info['step_mean_us']:.1f} us (jitter {info['jitter_us']:.1f} us), "
        f"pulse SNR {info['pulse_snr_db']:.1f} dB, "
        f"decimated {info['fs_raw']/1e3:.0f}->{info['fs']/1e3:.0f} kS/s")
    log(f"   integration: {info['n_batches']} batches x {info['coh_batch']} "
        f"coherent ({info['coh_time_s']*1e3:.0f} ms each), "
        f"magnitudes then averaged")
    if info["n_found"] != info["n_expected"]:
        log(f"   WARNING: expected {info['n_expected']} chips, "
            f"only {info['n_found']} fit in the recording")
    log(f"   plot: {png}  ({time.time()-t0:.1f} s)")
    return True, ""



PEAK_POWER_W = 1.5        # PA output in the pulse at 100 % DAC amplitude
CODE_LEN = {"CARRIER": 1, "BARKER2": 2, "BARKER3": 3, "BARKER4": 4, "BARKER5": 5,
            "BARKER7": 7, "BARKER11": 11, "BARKER13": 13}


def chip_seconds(cfg):
    """Length of one transmitted chip = code length x bit duration."""
    return CODE_LEN.get(str(cfg.mod).upper(), 13) * cfg.bit_us * 1e-6


def frame_seconds(cfg):
    """Real frame length: the firmware adds a fixed scheduling delay plus its own
    per-chip housekeeping to every chip interval (bpsk_tx.c schedules each chip
    150 us ahead of the current time), so a frame always runs longer than
    chips x S2S. The overhead is learned from completed frames."""
    overhead = getattr(cfg, "_overhead_us", cfg.chip_overhead_us)
    return cfg.chips * (cfg.s2s + overhead) / 1e6


def tx_budget(cfg):
    """Peak power, energy and mean power of one frame.

    The DAC sets the amplitude and power follows its square, so at 100 % the PA
    delivers PEAK_POWER_W in the pulse.
    """
    amp = max(0.0, cfg.amp) / 100.0
    peak_w = PEAK_POWER_W * amp * amp
    chip_s = chip_seconds(cfg)
    frame_s = frame_seconds(cfg)
    on_s = chip_s * cfg.chips
    energy_j = peak_w * on_s
    return {"peak_w": peak_w, "chip_s": chip_s, "frame_s": frame_s, "on_s": on_s,
            "energy_j": energy_j,
            "avg_w": energy_j / frame_s if frame_s > 0 else 0.0,
            "duty": on_s / frame_s if frame_s > 0 else 0.0}


def ig_coh_batch(analyze_args):
    """The coherent batch the analysis will use - the rolling pass length."""
    import ionogram as ig
    return int(ig.parse_analysis_args(analyze_args).coh_batch)


def s2s_from_range(range_km, overhead_us):
    """Chip interval giving the asked unambiguous radar range.

    The echo from that range must arrive before the next chip goes out, so the
    interval is the two-way travel time; the firmware adds its per-chip overhead
    on top, so it is subtracted from what we program.
    """
    return 2.0 * range_km * 1000.0 / 299_792_458.0 * 1e6 - overhead_us


def range_km_from_frame(cfg):
    """The range that the actual chip interval corresponds to."""
    return frame_seconds(cfg) / max(1, cfg.chips) * 299_792_458.0 / 2.0 / 1000.0


def pico_healthy(pico, why=""):
    """Verify the transmitter is still there, and bring it back if it is not.

    Never raises: a health check that throws would abort the very run it is meant
    to protect.
    """
    if pico is None:
        return True
    try:
        if getattr(pico, "silent_frames", 0) >= 2:
            log(f"   TX: {pico.silent_frames} frames with no reply{why}")
            pico.ok = False
        if pico.alive():
            pico.silent_frames = 0
            ALARM.stop("transmitter is answering again")
            return True
        # Sound the alarm before recovery, not after it fails: reopening the port
        # and resetting the USB bus can take a minute, and that is exactly the
        # minute somebody should be walking over to look at the rig.
        ALARM.start(f"lost the transmitter{why}")
        if pico.recover():
            pico.silent_frames = 0
            ALARM.stop("transmitter recovered")
            return True
    except Exception as e:
        log(f"   TX: health check failed: {type(e).__name__}: {e}")
    return False


def sound_once(rx, pico, cfg, freq_hz, seq):
    center = freq_hz - cfg.offset_hz
    frame_s = frame_seconds(cfg) if pico else cfg.rx_seconds
    total_s = cfg.settle_ms / 1e3 + frame_s + cfg.tail_ms / 1e3 + 2.0

    log(f"--- cycle {seq}: TX {freq_hz/1e6:.6f} MHz, RX center "
        f"{center/1e6:.6f} MHz (signal at +{cfg.offset_hz/1e3:.1f} kHz), "
        f"frame {frame_s:.2f} s ---")

    rx.tune(center)
    if pico:
        pico.set_params(freq_hz / 1e6, cfg)

    rec = Recorder(rx, max_samples=int(total_s * rx.fs))
    rx.activate()
    rec.start()

    progress(text="receiver settling", seconds=cfg.settle_ms / 1e3)
    time.sleep(cfg.settle_ms / 1e3)
    settle_idx = rec.samples                     # drop the stream start transient

    started = completed = None
    if pico:
        pico.poll()
        pico.send("TX_ONCE")
        tx_idx = rec.samples
        t_tx = time.time()
        progress(text=f"transmitting {freq_hz/1e6:.3f} MHz "
                      f"({cfg.chips} chips)", seconds=frame_s)
        started = pico.wait_for([["[TX-", "Started"]], timeout=5.0)
        if started is None:
            log("   WARNING: no '[TX-n] Started' from the Pico")
            if not pico.alive(timeout=1.0):
                # nothing is transmitting - stop now instead of writing 100 MB
                # of noise and only noticing at the end of the frame
                log("   transmitter is not responding - aborting this capture")
                rec.stop()
                rx.deactivate()
                progress()
                return None

        # Match the frame number, so a line left over from an earlier cycle can
        # never be mistaken for this frame's completion.
        tag = None
        if started:
            m = re.search(r"\[TX-(\d+)\]", started)
            tag = f"[TX-{m.group(1)}]" if m else None
        groups = [[tag, "Completed"], [tag, "ERROR"]] if tag else \
                 [["Completed"], ["ERROR"]]
        # Deadline measured from the start of the frame, not added on top of it:
        # if the message is lost we give up 1.5 s after the frame should have
        # ended instead of idling for another full timeout.
        completed = pico.wait_for(
            groups, timeout=max(0.5, (t_tx + frame_s + 1.5) - time.time()))

        if completed is None:
            # The serial line is only an optimisation - keep recording for the
            # frame we know was started, so one lost message costs nothing. But a
            # frame with no messages at all is a hint that the Pico is gone, and
            # that is checked between frames.
            if started is None:
                pico.silent_frames = getattr(pico, "silent_frames", 0) + 1
            log("   note: no 'Completed' message, recording the expected frame")
        elif "ERROR" in completed:
            log(f"   WARNING: firmware reported: {completed}")
        else:
            pico.silent_frames = 0
            measured = time.time() - t_tx
            per_chip = measured / max(1, cfg.chips) * 1e6 - cfg.s2s
            prev = getattr(cfg, "_overhead_us", cfg.chip_overhead_us)
            cfg._overhead_us = 0.7 * prev + 0.3 * max(0.0, per_chip)
            if abs(measured - frame_s) > 0.25:
                log(f"   frame took {measured:.2f} s (expected {frame_s:.2f} s), "
                    f"per-chip overhead now {cfg._overhead_us:.0f} us")

        left = (t_tx + frame_s) - time.time()      # never stop mid-frame
        if left > 0:
            time.sleep(left)
    else:
        tx_idx = rec.samples
        progress(text=f"recording {freq_hz/1e6:.3f} MHz", seconds=frame_s)
        time.sleep(frame_s)

    time.sleep(cfg.tail_ms / 1e3)
    rec.stop()
    rx.deactivate()
    progress(text="writing capture")

    iq = rec.data()
    if iq.size < 2 * int(0.5 * rx.fs):
        log(f"   ERROR: capture too short ({iq.size//2} samples) - skipping")
        return None
    if rec.overflows:
        log(f"   WARNING: {rec.overflows} stream overflows (samples dropped)")
    if rec.errors:
        log(f"   WARNING: stream errors: {rec.errors[:3]}")

    # keep a short lead-in before the first TX pulse, drop the rest
    start_idx = max(settle_idx, tx_idx - int(cfg.lead_ms / 1e3 * rx.fs))
    iq = iq[2 * start_idx:]
    dur = iq.size / 2 / rx.fs
    if dur < 0.5:
        log(f"   ERROR: only {dur:.2f} s left after trimming - stream stalled? skipping")
        return None

    peak, rms, clip = capture_quality(iq)
    # The dongle delivers 8-bit samples scaled into int16, so one ADC step is 256
    # counts. Judging the level in ADC steps is what tells you whether the
    # receiver is quantisation limited or about to clip - there is no analog gain
    # in direct sampling, so the answer is always an external pad or preamp.
    lsb_rms, lsb_peak = rms / 256.0, peak / 256.0
    log(f"   captured {dur:.2f} s, peak={peak:.0f}/32767, rms={rms:.0f}, "
        f"clipped={clip*100:.2f}%  (ADC steps: noise {lsb_rms:.1f}, peak {lsb_peak:.0f} "
        f"of 128)")
    if clip > 0.01:
        log("   WARNING: input clipping - add an attenuator (no analog gain here)")
    elif lsb_peak > 120:
        log("   note: peaks reach the ADC ceiling - a few dB of pad would help")
    if lsb_rms < 3.0:
        log(f"   note: band noise is only {lsb_rms:.1f} ADC steps - the receiver is "
            "quantisation limited, a 10-15 dB preamp would buy real sensitivity")

    # which sideband holds the signal (spectrum inversion check)
    bw = 2.0 / (cfg.bit_us * 1e-6)
    st = spectrum_stats(iq, rx.fs, cfg.offset_hz, bw)
    inverted = False
    snr_db = float("nan")
    if st:
        pos_db = 10 * np.log10(st["pos"] / st["noise"])
        neg_db = 10 * np.log10(st["neg"] / st["noise"])
        log(f"   band power: +{cfg.offset_hz/1e3:.0f} kHz = {pos_db:+.1f} dB, "
            f"-{cfg.offset_hz/1e3:.0f} kHz = {neg_db:+.1f} dB (over noise)")
        snr_db = max(pos_db, neg_db)
        if cfg.iq_sense == "invert":
            inverted = True
        elif cfg.iq_sense == "auto":
            # Needs a decisive margin: our own TX beats everything on the band by
            # tens of dB, while a random QSO at -offset must not flip the stream.
            inverted = neg_db > pos_db + 15.0
        if snr_db < 20.0:
            log("   WARNING: no strong signal at the expected offset - "
                "TX off? PA off? antenna disconnected?")
    if inverted:
        log("   spectrum is inverted -> conjugating Q so the signal lands on +f")
        iq = conjugate_q(iq)

    now = datetime.utcnow()                      # UTC everywhere, as is usual
    stem = "baseband_{:.0f}Hz_{}_{}".format(center, now.strftime("%H-%M-%S"),
                                            now.strftime("%d-%m-%Y"))
    wav_path = os.path.join(cfg.out_dir, stem + ".wav")
    write_iq_wav(wav_path, iq, rx.fs)
    log(f"   wav: {wav_path} ({os.path.getsize(wav_path)/1e6:.1f} MB)")

    meta = {
        "utc": now.isoformat(timespec="seconds") + "Z",
        "local": datetime.now().isoformat(timespec="seconds"),
        "tx_freq_hz": freq_hz, "rx_center_hz": center, "offset_hz": cfg.offset_hz,
        "sample_rate_hz": rx.fs, "direct_samp": rx.direct_samp,
        "duration_s": round(dur, 3), "overflows": rec.overflows,
        "peak": peak, "rms": rms, "clip_frac": clip,
        "snr_db": None if not np.isfinite(snr_db) else round(float(snr_db), 2),
        "spectrum_inverted": bool(inverted),
        "tx": {"bit_us": cfg.bit_us, "amp_pct": cfg.amp, "mod": cfg.mod,
               "chips": cfg.chips, "s2s_us": cfg.s2s, "enabled": pico is not None},
        "tx_started": started, "tx_completed": completed,
    }
    with open(os.path.join(cfg.out_dir, stem + ".json"), "w") as f:
        json.dump(meta, f, indent=2)

    meta["wav_path"] = wav_path
    meta["png_path"] = None
    meta["analysis_ok"] = None
    meta["cycle"] = seq
    if cfg.no_analyze:
        return meta

    ok, _ = run_analysis(wav_path, cfg, meta)
    png = os.path.join(cfg.out_dir, stem + "_coh.png")
    meta["analysis_ok"] = ok
    meta["png_path"] = png if os.path.exists(png) else None
    if ok and not cfg.keep_wav:
        try:
            os.remove(wav_path)
            meta["wav_path"] = None
            log("   wav removed (use --keep-wav to keep raw I/Q)")
        except OSError as e:
            log(f"   could not remove wav: {e}")
    return meta


# =========================================================
#  Transmit only - the SDR is left alone
# =========================================================

def run_tx_only(pico, cfg, stop_evt=None):
    """Key the transmitter and the T/R sequencing, without touching the SDR.

    Nothing here opens the dongle, so another program (SDR#, gqrx, SDRuno...) can
    hold it and you can watch the sounding live while it runs.
    """
    frame_s = frame_seconds(cfg)
    freqs = cfg.freq_list
    log(f"=== TX ONLY: {len(freqs)} frequency/ies, frame {frame_s:.2f} s, "
        f"{'infinite' if cfg.cycles == 0 else cfg.cycles} cycles ===")
    log("    the SDR is not opened - use your own receiver program on it")

    def wait(seconds):
        if stop_evt is not None:
            return stop_evt.wait(seconds)
        time.sleep(seconds)
        return False

    seq = 0
    last_freq = None
    while (cfg.cycles == 0 or seq < cfg.cycles):
        if stop_evt is not None and stop_evt.is_set():
            break
        t_cycle = time.time()
        freq = freqs[seq % len(freqs)]
        seq += 1
        log(f"--- TX {seq}: {freq/1e6:.6f} MHz, {cfg.chips} chips, "
            f"frame {frame_s:.2f} s ---")
        if freq != last_freq:
            pico.set_params(freq / 1e6, cfg)
            last_freq = freq
        pico.poll()
        pico.send("TX_ONCE")
        t_tx = time.time()
        progress(text=f"transmitting {freq/1e6:.3f} MHz ({cfg.chips} chips)",
                 seconds=frame_s)

        started = pico.wait_for([["[TX-", "Started"]], timeout=5.0)
        if started is None:
            log("   WARNING: no '[TX-n] Started' from the Pico")
            if not pico.alive(timeout=1.0):
                # nothing is transmitting - stop now instead of writing 100 MB
                # of noise and only noticing at the end of the frame
                log("   transmitter is not responding - aborting this capture")
                rec.stop()
                rx.deactivate()
                progress()
                return None
        tag = None
        if started:
            m = re.search(r"\[TX-(\d+)\]", started)
            tag = f"[TX-{m.group(1)}]" if m else None
        groups = [[tag, "Completed"], [tag, "ERROR"]] if tag else \
                 [["Completed"], ["ERROR"]]
        completed = pico.wait_for(
            groups, timeout=max(0.5, (t_tx + frame_s + 1.5) - time.time()))
        if completed is None:
            log("   note: no 'Completed' message")
        elif "ERROR" in completed:
            log(f"   WARNING: firmware reported: {completed}")
        else:
            pico.silent_frames = 0
            measured = time.time() - t_tx
            per_chip = measured / max(1, cfg.chips) * 1e6 - cfg.s2s
            prev = getattr(cfg, "_overhead_us", cfg.chip_overhead_us)
            cfg._overhead_us = 0.7 * prev + 0.3 * max(0.0, per_chip)
            log(f"   frame done in {measured:.2f} s "
                f"(per-chip overhead {cfg._overhead_us:.0f} us)")

        if pico and not pico.ok:
            log("   TX serial link lost - reconnecting...")
            log("   TX link restored" if pico.reconnect(60.0) else "   still down")
        if cfg.period > 0:
            left = cfg.period - (time.time() - t_cycle)
            if left > 0:
                log(f"   idle {left:.1f} s until next frame")
                progress(text="idle until next frame", seconds=left)
                if wait(left):
                    break
    progress()
    log(f"TX only finished, {seq} frame(s) sent.")
    return seq


# =========================================================
#  Ionogram: record the whole sweep first, analyse afterwards
# =========================================================

def sweep_plan(start_hz, stop_hz, step_hz):
    if step_hz <= 0:
        raise SystemExit("Ionogram step must be positive")
    if stop_hz < start_hz:
        start_hz, stop_hz = stop_hz, start_hz
    n = int(np.floor((stop_hz - start_hz) / step_hz + 1e-9)) + 1
    return [start_hz + i * step_hz for i in range(n)]


class AnalysisPool:
    """Crunch captures while the next frequency is already on the air.

    Recording is the part that must not be delayed - the transmitter and the
    ionosphere do not wait - so analysis runs beside it rather than after it.
    A small pool absorbs the load (the numpy/scipy inner loops drop the GIL),
    and exactly one thread draws, coalescing results that land while a plot is
    being rendered so that drawing can never become the bottleneck.

    If the pool still cannot keep up, the stalest queued capture is dropped
    rather than blocking the transmitter or filling the disk. Callers that must
    not lose a frequency get it back through `failed` and can re-record it.
    """

    def __init__(self, ap, redraw, detrend_km=0.0, keep_wav=False, workers=0,
                 on_ok=None, on_fail=None):
        import ionogram as ig
        self.ig = ig
        self.ap = ap
        self.redraw = redraw
        self.detrend_km = detrend_km
        self.keep_wav = keep_wav
        self.on_ok = on_ok
        self.on_fail = on_fail
        self.workers = int(workers or max(1, min(4, (os.cpu_count() or 2) - 1)))
        self.cap = max(2 * self.workers, 4)

        self._pend = collections.deque()
        self._cv = threading.Condition()
        self._dirty = threading.Event()
        self._quit = threading.Event()
        self._busy = 0
        self.ok = 0
        self.bad = 0
        self.dropped = 0
        self.peak_backlog = 0
        self.failed = []                 # frequencies worth another attempt

        self._pool = [threading.Thread(target=self._worker, daemon=True,
                                       name=f"analysis-{k+1}")
                      for k in range(self.workers)]
        for t in self._pool:
            t.start()
        self._drawer = threading.Thread(target=self._plotter, daemon=True,
                                        name="plotter")
        self._drawer.start()

    # -- producer side
    def submit(self, freq, meta, tag):
        with self._cv:
            if len(self._pend) >= self.cap:
                of, om, ot = self._pend.popleft()
                self.dropped += 1
                self.failed.append(of)
                self._drop_wav(om)
                log(f"   analysis is behind - dropped {ot} {of/1e6:.3f} MHz "
                    f"(backlog {len(self._pend)+1}/{self.cap})")
            self._pend.append((freq, meta, tag))
            self.peak_backlog = max(self.peak_backlog, len(self._pend))
            self._cv.notify()

    @property
    def backlog(self):
        with self._cv:
            return len(self._pend) + self._busy

    def drain(self, timeout=900):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.backlog == 0:
                return True
            time.sleep(0.2)
        return False

    def close(self, timeout=900):
        self.drain(timeout)
        self._quit.set()
        with self._cv:
            self._cv.notify_all()
        for t in self._pool:
            t.join(timeout=30)
        self._drawer.join(timeout=60)
        self._dirty.clear()

    def request_redraw(self):
        self._dirty.set()

    # -- internals
    def _drop_wav(self, meta):
        w = (meta or {}).get("wav_path")
        if w and not self.keep_wav:
            try:
                os.remove(w)
            except OSError:
                pass

    def _analyse(self, freq, meta, tag):
        try:
            km, db, info = self.ig.profile_from_wav(meta.get("wav_path"),
                                                    self.ap, meta)
            n_exp = int(info.get("n_expected") or 0)
            if n_exp and int(info.get("n_found") or 0) != n_exp:
                raise RuntimeError(
                    f"only {info.get('n_found')}/{n_exp} chips accounted for")
            if self.detrend_km > 0:
                db = self.ig.detrend_profile(km, db, self.detrend_km)
            self.ok += 1
            if self.on_ok:
                self.on_ok(freq, km, db, info, meta)
            log(f"   {tag} {freq/1e6:7.3f} MHz folded in "
                f"({info['n_found']}/{info['n_expected']} pulses)")
        except Exception as e:
            self.bad += 1
            self.failed.append(freq)
            if self.on_fail:
                self.on_fail(freq, str(e))
            log(f"   {tag} {freq/1e6:7.3f} MHz analysis FAILED: {e}")
        finally:
            self._drop_wav(meta)
            self._dirty.set()

    def _worker(self):
        while True:
            with self._cv:
                while not self._pend and not self._quit.is_set():
                    self._cv.wait(0.25)
                if not self._pend:
                    return
                item = self._pend.popleft()
                self._busy += 1
            try:
                self._analyse(*item)
            finally:
                with self._cv:
                    self._busy -= 1
                    self._cv.notify_all()

    def _plotter(self):
        while not self._quit.is_set():
            if self._dirty.wait(0.25):
                self._dirty.clear()
                try:
                    self.redraw()
                except Exception as e:
                    log(f"   redraw failed: {type(e).__name__}: {e}")
        if self._dirty.is_set():
            self._dirty.clear()
            try:
                self.redraw()
            except Exception as e:
                log(f"   redraw failed: {type(e).__name__}: {e}")


def run_ionogram(rx, pico, cfg, stop_evt=None, on_update=None):
    """One deep sweep, analysed beside the recording instead of after it.

    The sweep itself is unchanged and still runs back to back, so the map still
    describes one state of the ionosphere; what changed is that each capture is
    crunched while the next frequency is already on the air, which takes the
    analysis time off the end of the run instead of adding it.

    A frequency whose capture will not analyse is re-recorded rather than left
    as a hole in the map - interference that ruined one capture is usually gone
    a minute later. --ion-retries bounds how many extra attempts each one gets.
    """
    import copy
    import ionogram as ig

    freqs = sweep_plan(cfg.ion_start * 1e6, cfg.ion_stop * 1e6, cfg.ion_step_khz * 1e3)
    ap = ig.parse_analysis_args(cfg.analyze_args)
    retries = int(getattr(cfg, "ion_retries", 1) or 0)
    frame_s = frame_seconds(cfg) if pico else cfg.rx_seconds
    per_cycle_s = cfg.settle_ms / 1e3 + frame_s + cfg.tail_ms / 1e3 + 1.5
    bytes_each = 4 * cfg.rate * (frame_s + cfg.lead_ms / 1e3 + cfg.tail_ms / 1e3)

    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%SZ")
    session = os.path.join(cfg.out_dir, "sweeps", f"sweep_{stamp}")
    maps_dir = os.path.join(cfg.out_dir, "ionograms")
    os.makedirs(session, exist_ok=True)
    os.makedirs(maps_dir, exist_ok=True)
    stem = os.path.join(maps_dir, f"ionogram_{stamp}")
    title = f"Ionogram {freqs[0]/1e6:.2f}-{freqs[-1]/1e6:.2f} MHz"

    log(f"=== IONOGRAM: {len(freqs)} frequencies "
        f"{freqs[0]/1e6:.3f} - {freqs[-1]/1e6:.3f} MHz, "
        f"step {cfg.ion_step_khz:.0f} kHz ===")
    log(f"    sweep ~{len(freqs)*per_cycle_s/60:.1f} min, "
        f"~{len(freqs)*bytes_each/1e9:.1f} GB of raw I/Q in {session}")
    log(f"    analysed alongside the sweep; up to {retries} retry/ies per frequency")
    log(f"    the map lands in {maps_dir}, with every other ionogram")

    # depth 1: one look per frequency, and a frequency never sounded stays a gap
    grid = ig.RollingIonogram(freqs, 1, cfg.ion_km_min, cfg.ion_km_max)
    grid_lock = threading.Lock()
    started = datetime.utcnow().isoformat(timespec="seconds") + "Z"

    def redraw():
        with grid_lock:
            f_hz, km_axis, M = grid.matrix()
            footer = grid.footer()
        if not np.isfinite(M).any():
            return
        subtitle = ig.fmt_stamp(started)
        png = ig.plot_ionogram(f_hz, km_axis, M, stem + ".png", title=title,
                               subtitle=subtitle, footer=footer)
        ig.save_data(stem + ".npz", f_hz, km_axis, M,
                     meta={"title": title, "subtitle": subtitle, "footer": footer})
        if on_update:
            on_update(png)

    def took_it(freq, km, db, _info, _meta):
        with grid_lock:
            grid.add(freq, km, db)

    pool = AnalysisPool(ap, redraw, detrend_km=cfg.detrend_km,
                        keep_wav=cfg.keep_wav,
                        workers=int(getattr(cfg, "ion_workers", 0) or 0),
                        on_ok=took_it)
    log(f"    {pool.workers} analysis thread(s) running alongside the sweep")

    rec_cfg = copy.copy(cfg)
    rec_cfg.out_dir = session
    rec_cfg.no_analyze = True
    rec_cfg.keep_wav = True

    seq = 0
    recoveries = 0
    recorded = 0
    stopped = False

    def sweep(todo, label):
        """Record every frequency in `todo`, handing each to the pool."""
        nonlocal seq, recoveries, recorded, stopped
        idx = 0
        while idx < len(todo):
            if stop_evt is not None and stop_evt.is_set():
                log("ionogram: stopped on request")
                stopped = True
                return
            i, f = idx + 1, todo[idx]
            if not pico_healthy(pico, " during the sweep"):
                recoveries += 1
                if recoveries > cfg.max_recoveries:
                    log(f"=== sweep stopped at {f/1e6:.3f} MHz: transmitter did "
                        f"not come back after {cfg.max_recoveries} attempts ===")
                    stopped = True
                    return
                log(f"   retrying {f/1e6:.3f} MHz after recovery "
                    f"({recoveries}/{cfg.max_recoveries})")
                continue
            seq += 1
            progress(done=idx, total=len(todo),
                     text=f"{_series_label()}{label} {i}/{len(todo)} "
                          f"({f/1e6:.3f} MHz)")
            try:
                meta = sound_once(rx, pico, rec_cfg, f, seq)
            except Exception as e:
                log(f"   recording FAILED: {type(e).__name__}: {e}")
                meta = None
            if meta and meta.get("wav_path"):
                recorded += 1
                pool.submit(f, meta, f"[{label} {i}/{len(todo)}]")
            elif pico is not None and not pico.alive():
                continue                     # dead link: redo this frequency
            else:
                pool.failed.append(f)        # nothing recorded - worth a retry
            idx += 1

    try:
        sweep(list(freqs), "sweep step")
        # Analysis lags the sweep, so failures are only all known once it drains.
        if not stopped:
            progress(text="finishing the analysis backlog")
            pool.drain()
        for attempt in range(1, retries + 1):
            if stopped or (stop_evt is not None and stop_evt.is_set()):
                break
            with grid_lock:
                missing = [float(f) for f, n in zip(grid.freqs, grid.counts())
                           if n == 0]
            pool.failed.clear()
            if not missing:
                break
            log(f"=== retry {attempt}/{retries}: {len(missing)} frequency/ies "
                f"with no usable profile ===")
            if pico:
                pico.stop_auto()
            sweep(missing, f"retry {attempt}")
            pool.drain()
    finally:
        pool.close()
        if pico:
            pico.stop_auto()
        try:
            redraw()
        except Exception as e:
            log(f"   final redraw failed: {type(e).__name__}: {e}")
        progress()

    with grid_lock:
        done = int((grid.counts() > 0).sum())
    if done == 0:
        raise SystemExit("Ionogram: nothing analysed")
    log(f"=== ionogram done: {done}/{len(freqs)} frequencies, "
        f"{recorded} captures, {pool.bad} analysis failure(s) ===")
    log(f"    {stem}.png")
    return stem + ".png"


def run_ionogram_rolling(rx, pico, cfg, stop_evt=None, on_update=None):
    """Repeated shallow sweeps, averaged into one map that updates as it goes.

    The deep sweep spends every pulse on one frequency before moving on: with
    2048 chips and a coherent batch of 256 that is eight batches stacked at a
    single frequency, and nothing at all is drawable until the whole band has
    been recorded and analysed. Here one pass spends a single batch per
    frequency, so the map covers the band immediately and deepens as passes
    accumulate; past `depth` passes the oldest is dropped as the newest lands.

    Analysis runs in a worker thread, so a capture is being crunched while the
    next frequency is already on the air, and the map is rewritten after every
    single sounding.
    """
    import copy
    import queue
    import ionogram as ig

    freqs = sweep_plan(cfg.ion_start * 1e6, cfg.ion_stop * 1e6, cfg.ion_step_khz * 1e3)
    ap = ig.parse_analysis_args(cfg.analyze_args)
    pass_chips = int(getattr(cfg, "ion_pass_chips", 0) or ap.coh_batch)
    depth = int(getattr(cfg, "ion_depth", 0)
                or max(1, int(round(cfg.chips / max(1, pass_chips)))))
    passes_wanted = int(getattr(cfg, "ion_passes", 0) or 0)

    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%SZ")
    session = os.path.join(cfg.out_dir, "rolling", f"rolling_{stamp}")
    raw_dir = os.path.join(session, "captures")
    os.makedirs(raw_dir, exist_ok=True)
    # One file per sweep. The map is a rolling average, so every completed pass
    # is a real observation of the ionosphere at that moment - overwriting a
    # single file would throw all of them away but the last.
    cur = {"stem": None, "pass": 0, "stamp": None}

    def new_pass_file(n):
        cur["pass"] = n
        cur["stamp"] = datetime.utcnow().strftime("%Y%m%d_%H%M%SZ")
        cur["stem"] = os.path.join(session, f"ionogram_p{n:03d}_{cur['stamp']}")
        return cur["stem"]

    rec_cfg = copy.copy(cfg)
    rec_cfg.out_dir = raw_dir
    rec_cfg.no_analyze = True          # the worker does it, off the critical path
    rec_cfg.keep_wav = True
    rec_cfg.chips = pass_chips

    frame_s = frame_seconds(rec_cfg) if pico else cfg.rx_seconds
    per_step_s = cfg.settle_ms / 1e3 + frame_s + cfg.tail_ms / 1e3 + 1.5
    title = (f"Rolling ionogram {freqs[0]/1e6:.2f}-{freqs[-1]/1e6:.2f} MHz")

    log(f"=== ROLLING IONOGRAM: {len(freqs)} frequencies "
        f"{freqs[0]/1e6:.3f} - {freqs[-1]/1e6:.3f} MHz, step {cfg.ion_step_khz:.0f} kHz ===")
    log(f"    {pass_chips} chips per frequency per pass, averaging {depth} passes "
        f"({depth * pass_chips} chips deep once full)")
    log(f"    one pass ~{len(freqs)*per_step_s/60:.1f} min; "
        f"{'endless' if passes_wanted == 0 else str(passes_wanted) + ' passes'}")
    log(f"    map and raw I/Q in {session}")

    roll = ig.RollingIonogram(freqs, depth, cfg.ion_km_min, cfg.ion_km_max)
    roll_lock = threading.Lock()
    retries = int(getattr(cfg, "ion_retries", 1) or 0)
    state = {"png": None, "newest": None}

    def redraw():
        with roll_lock:
            f_hz, km_axis, M = roll.matrix()
            footer = roll.footer()
            newest = state["newest"]
            stem = cur["stem"]
            n = cur["pass"]
        if stem is None or not np.isfinite(M).any():
            return
        subtitle = ig.fmt_stamp(newest)
        head = f"{title} - pass {n}"
        png = ig.plot_ionogram(f_hz, km_axis, M, stem + ".png", title=head,
                               subtitle=subtitle, footer=footer)
        ig.save_data(stem + ".npz", f_hz, km_axis, M,
                     meta={"title": head, "subtitle": subtitle, "footer": footer,
                           "depth": depth, "pass_chips": pass_chips, "pass": n})
        state["png"] = png
        if on_update:
            on_update(png)

    def took_it(freq, km, db, _info, meta):
        with roll_lock:
            roll.add(freq, km, db)
            u = meta.get("utc")
            if u and (state["newest"] is None or u > state["newest"]):
                state["newest"] = u

    pool = AnalysisPool(ap, redraw, detrend_km=cfg.detrend_km,
                        keep_wav=cfg.keep_wav,
                        workers=int(getattr(cfg, "ion_workers", 0) or 0),
                        on_ok=took_it)
    log(f"    {pool.workers} analysis thread(s), backlog cap {pool.cap} captures, "
        f"up to {retries} retry/ies per frequency")

    seq = 0
    n_pass = 0
    recoveries = 0
    stopped = False

    def sweep(todo, label):
        """Record every frequency in `todo`, handing each to the pool."""
        nonlocal seq, recoveries, stopped
        idx = 0
        while idx < len(todo):
            if stop_evt is not None and stop_evt.is_set():
                log("rolling ionogram: stopped on request")
                stopped = True
                return
            i, f = idx + 1, todo[idx]
            if not pico_healthy(pico, " during the rolling sweep"):
                recoveries += 1
                if recoveries > cfg.max_recoveries:
                    log(f"=== stopped: transmitter did not come back after "
                        f"{cfg.max_recoveries} attempts ===")
                    stopped = True
                    return
                log(f"   retrying {f/1e6:.3f} MHz after recovery "
                    f"({recoveries}/{cfg.max_recoveries})")
                continue
            seq += 1
            progress(done=idx, total=len(todo),
                     text=f"pass {n_pass} - {label} {i}/{len(todo)} "
                          f"({f/1e6:.3f} MHz)"
                          + (f", {pool.backlog} waiting" if pool.backlog else ""))
            try:
                meta = sound_once(rx, pico, rec_cfg, f, seq)
            except Exception as e:
                log(f"   recording FAILED: {type(e).__name__}: {e}")
                meta = None
            if meta and meta.get("wav_path"):
                pool.submit(f, meta, f"[p{n_pass} {label} {i}/{len(todo)}]")
            elif pico is not None and not pico.alive():
                continue                     # dead link: redo this frequency
            else:
                pool.failed.append(f)
            idx += 1

    def close_pass():
        """Fill the holes, then finish this pass file so nothing overwrites it."""
        if cur["stem"] is None:
            return
        progress(text=f"finishing pass {cur['pass']}")
        pool.drain()
        for attempt in range(1, retries + 1):
            if stopped or (stop_evt is not None and stop_evt.is_set()):
                break
            # A frequency is a hole only if it has nothing at all; one that
            # still holds older passes is not worth re-recording out of turn.
            with roll_lock:
                holes = [float(f) for f, n in zip(roll.freqs, roll.counts())
                         if n == 0]
            pool.failed.clear()
            if not holes:
                break
            log(f"   pass {cur['pass']} retry {attempt}/{retries}: "
                f"{len(holes)} empty column(s)")
            sweep(holes, f"retry {attempt}")
            pool.drain()
        pool.request_redraw()
        time.sleep(0.3)
        pool.drain()
        if state["png"]:
            log(f"   pass {cur['pass']} saved: {os.path.basename(state['png'])}")

    try:
        while not stopped and (passes_wanted == 0 or n_pass < passes_wanted):
            n_pass += 1
            with roll_lock:
                new_pass_file(n_pass)
            log(f"########## PASS {n_pass}"
                f"{'' if passes_wanted == 0 else '/' + str(passes_wanted)} "
                f"-> {os.path.basename(cur['stem'])}.png ##########")
            sweep(list(freqs), "step")
            close_pass()
    finally:
        progress(text="finishing the analysis backlog")
        pool.close()
        try:
            redraw()                    # an interrupted pass still keeps its map
        except Exception as e:
            log(f"   final redraw failed: {type(e).__name__}: {e}")
        progress()

    log(f"=== rolling ionogram finished: {n_pass} pass(es), "
        f"{pool.ok} soundings folded in, {pool.bad} failed"
        + (f", {pool.dropped} dropped to keep up" if pool.dropped else "")
        + " ===")
    if pool.dropped:
        log(f"    analysis could not keep up (peak backlog {pool.peak_backlog}): "
            f"raise --ion-workers, widen --ion-step-khz or lower --coh_batch")
    import glob as _glob
    maps = sorted(_glob.glob(os.path.join(session, "ionogram_p*.png")))
    log(f"    {len(maps)} map(s) in {session}")
    for m in maps[-3:]:
        log(f"      {os.path.basename(m)}")
    if len(maps) > 3:
        log(f"      ... and {len(maps)-3} earlier")
    return state["png"]


_SERIES = {"n": 0, "total": 0}


def _series_label():
    if _SERIES["total"] == 1 or _SERIES["n"] == 0:
        return ""
    tot = "inf" if _SERIES["total"] == 0 else str(_SERIES["total"])
    return f"ionogram {_SERIES['n']}/{tot} - "


def run_ionogram_series(rx, pico, cfg, stop_evt=None, on_done=None, on_update=None):
    """One ionogram after another, each into its own timestamped folder.

    Repeating the whole sweep is how you watch the ionosphere move: the layer
    heights and the top usable frequency change over minutes, so a series of maps
    is far more informative than a single one.
    """
    total = int(getattr(cfg, "ion_repeat", 1) or 0)
    made = 0
    quick_fails = 0
    while total == 0 or made < total:
        if stop_evt is not None and stop_evt.is_set():
            break
        t_start = time.time()
        made += 1
        _SERIES["n"], _SERIES["total"] = made, total
        log(f"########## IONOGRAM {made}"
            f"{'' if total == 0 else '/' + str(total)} ##########")
        try:
            png = run_ionogram(rx, pico, cfg, stop_evt=stop_evt,
                               on_update=on_update)
            if on_done and png:
                on_done(png)
        except SystemExit as e:
            log(f"ionogram {made} aborted: {e}")
        except Exception as e:
            log(f"ionogram {made} FAILED: {type(e).__name__}: {e}")
        took = time.time() - t_start
        log(f"########## ionogram {made} done in {took/60:.1f} min ##########")

        if took < 5.0:                      # nothing real can finish that fast
            quick_fails += 1
            if quick_fails >= 3:
                log("three ionograms failed immediately - stopping the series "
                    "instead of spinning")
                break
            if stop_evt is not None and stop_evt.wait(5.0):
                break
            elif stop_evt is None:
                time.sleep(5.0)
        else:
            quick_fails = 0

        if (total == 0 or made < total) and cfg.ion_period > 0:
            wait = cfg.ion_period * 60.0 - took
            if wait > 0:
                log(f"waiting {wait/60:.1f} min until the next ionogram")
                progress(text="waiting for the next ionogram", seconds=wait)
                if stop_evt is not None:
                    if stop_evt.wait(wait):
                        break
                else:
                    time.sleep(wait)
    progress()
    log(f"ionogram series finished: {made} map(s)")
    return made


# =========================================================
#  Self test / device listing
# =========================================================

def do_list_devices():
    if SoapySDR is None:
        raise SystemExit("SoapySDR python module missing")
    for i, d in enumerate(SoapySDR.Device.enumerate()):
        log(f"device {i}: " + ", ".join(f"{k}={d[k]}" for k in d.keys()))
    return 0


# =========================================================
#  CLI
# =========================================================

def parse_args():
    ap = argparse.ArgumentParser(
        description="Automatic ionosonde: Pico TX + RTL-SDR RX (direct sampling, "
                    "Q-branch) + coherent correlation analysis.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    g = ap.add_argument_group("sounding")
    g.add_argument("--freqs", default="7.022",
                   help="carrier frequencies in MHz, comma separated (swept in order)")
    g.add_argument("--offset-khz", type=float, default=50.0,
                   help="how far BELOW the carrier the SDR is tuned, so the signal "
                        "does not sit on the DC spike")
    g.add_argument("--cycles", type=int, default=0, help="number of cycles, 0 = forever")
    g.add_argument("--period", type=float, default=0.0,
                   help="seconds between cycle starts (0 = back to back)")

    g = ap.add_argument_group("TX (Pico)")
    g.add_argument("--port", default=None, help="serial port (default: first /dev/ttyACM*)")
    g.add_argument("--tx", action=argparse.BooleanOptionalAction, default=True,
                   help="key the Pico; --no-tx listens only")
    g.add_argument("--rx", action=argparse.BooleanOptionalAction, default=True,
                   help="record and analyse; --no-rx transmits without opening the "
                        "SDR at all, so another program can use the dongle")
    g.add_argument("--bit-us", type=float, default=40.0, help="BPSK bit duration [us]")
    g.add_argument("--amp", type=float, default=90.0, help="DAC amplitude [%%]")
    g.add_argument("--mod", default="BARKER13", help="CARRIER / BARKER2..BARKER13")
    g.add_argument("--chips", type=int, default=2048, help="chips per frame")
    g.add_argument("--range-km", type=float, default=780.0,
                   help="unambiguous radar range; the chip interval is derived "
                        "from it (two-way travel time minus the firmware overhead)")
    g.add_argument("--s2s", type=int, default=None,
                   help="chip start-to-start [us]; normally left alone and computed "
                        "from --range-km")
    g.add_argument("--max-recoveries", type=int, default=3,
                   help="how many times a sweep may stop to revive the transmitter "
                        "before giving up")
    g.add_argument("--chip-overhead-us", type=float, default=250.0,
                   help="firmware per-chip scheduling overhead added to S2S; the "
                        "real frame is chips x (S2S + overhead). Learned from "
                        "completed frames, this is only the starting guess")
    g.add_argument("--echo-serial", action="store_true", help="print every Pico line")

    g = ap.add_argument_group("RX (RTL-SDR)")
    g.add_argument("--device", default="driver=rtlsdr", help="SoapySDR device args")
    g.add_argument("--rate", type=float, default=250000, help="sample rate [S/s]")
    g.add_argument("--direct-samp", type=int, default=2, choices=(0, 1, 2),
                   help="RTL direct sampling: 0=off, 1=I-branch, 2=Q-branch (HF)")
    g.add_argument("--gain", type=float, default=None,
                   help="tuner gain [dB]; the tuner is bypassed in direct sampling, "
                        "so this only does something with --direct-samp 0")
    g.add_argument("--agc", action="store_true",
                   help="tuner AGC (bypassed in direct sampling too)")
    g.add_argument("--digital-agc", action=argparse.BooleanOptionalAction, default=True,
                   help="RTL2832 digital AGC - the only gain that acts in direct "
                        "sampling. It rescales samples after the ADC, so it adds no "
                        "dynamic range and, being a real scalar, does not affect "
                        "phase or coherent integration; it does make the ADC-step "
                        "diagnostic read the scaled numbers instead of the raw ones. "
                        "On by default, --no-digital-agc turns it off")
    g.add_argument("--ppm", type=float, default=0.0, help="frequency correction [ppm]")
    g.add_argument("--iq-sense", choices=("auto", "normal", "invert"), default="normal",
                   help="spectrum orientation of the I/Q stream; 'normal' verified "
                        "by measurement on this RTL2832U in direct sampling mode "
                        "(a signal above the tuned center lands on +f)")
    g.add_argument("--settle-ms", type=float, default=300.0, help="discarded stream start")
    g.add_argument("--lead-ms", type=float, default=50.0, help="recording kept before TX start")
    g.add_argument("--tail-ms", type=float, default=300.0, help="recording kept after TX end")
    g.add_argument("--rx-seconds", type=float, default=12.0,
                   help="capture length when --no-tx is used")

    g = ap.add_argument_group("output / analysis")
    g.add_argument("--out-dir", default=os.path.join(HERE, "captures"),
                   help="where WAV/PNG/JSON go")
    g.add_argument("--keep-wav", action="store_true",
                   help="keep the raw I/Q WAV after a successful analysis")
    g.add_argument("--no-analyze", action="store_true", help="record only")
    g.add_argument("--analyze-args", default=DEFAULT_ANALYZE_ARGS,
                   help="extra args for the correlation script")
    g.add_argument("--analyze-timeout", type=float, default=900.0, help="[s]")
    g.add_argument("--detrend-km", type=float, default=0.0,
                   help="remove the slow receiver-settling background by median "
                        "filtering over this range width [km]; 0 = off. 150 keeps "
                        "echoes narrower than ~40 km and flattens the rest")
    g.add_argument("--log-file", default=None, help="append console log to this file")

    g = ap.add_argument_group("ionogram")
    g.add_argument("--ionogram", action="store_true",
                   help="sweep a band: record every step first, analyse afterwards, "
                        "then draw the frequency vs height map")
    g.add_argument("--ion-start", type=float, default=2.0, help="sweep start [MHz]")
    g.add_argument("--ion-stop", type=float, default=10.0, help="sweep stop [MHz]")
    g.add_argument("--ion-step-khz", type=float, default=100.0, help="sweep step [kHz]")
    g.add_argument("--ion-repeat", type=int, default=1,
                   help="how many ionograms to make one after another, 0 = forever")
    g.add_argument("--ion-period", type=float, default=0.0,
                   help="minutes between the START of consecutive ionograms "
                        "(0 = start the next one immediately)")
    g.add_argument("--ion-km-min", type=float, default=100.0, help="map bottom [km]")
    g.add_argument("--ion-km-max", type=float, default=650.0, help="map top [km]")

    g = ap.add_argument_group("rolling ionogram")
    g.add_argument("--ion-rolling", action="store_true",
                   help="sweep the band shallow and repeatedly instead of deep "
                        "once: one coherent batch per frequency per pass, the "
                        "passes averaged, the map redrawn after every sounding")
    g.add_argument("--ion-pass-chips", type=int, default=0,
                   help="chips per frequency per pass (0 = one coherent batch)")
    g.add_argument("--ion-depth", type=int, default=0,
                   help="how many passes are averaged before the oldest is "
                        "dropped (0 = chips / coherent batch)")
    g.add_argument("--ion-passes", type=int, default=0,
                   help="how many passes to run, 0 = until stopped")
    g.add_argument("--ion-workers", type=int, default=0,
                   help="analysis threads running alongside the sweep "
                        "(0 = one per spare CPU core, max 4)")
    g.add_argument("--ion-retries", type=int, default=1,
                   help="extra attempts at a frequency whose capture will not "
                        "analyse, so a failure leaves a filled column instead "
                        "of a hole (0 = never retry)")
    g.add_argument("--no-alarm", dest="alarm", action="store_false", default=True,
                   help="do not beep when the transmitter goes missing")

    g = ap.add_argument_group("diagnostics")
    g.add_argument("--list-devices", action="store_true", help="list SoapySDR devices")

    cfg = ap.parse_args()
    if not cfg.tx and not cfg.rx:
        ap.error("nothing to do: enable --tx, --rx or both")
    cfg.no_tx = not cfg.tx                    # legacy names used inside
    cfg.tx_only = cfg.tx and not cfg.rx
    if (cfg.ionogram or cfg.ion_rolling) and not cfg.rx:
        ap.error("an ionogram needs the receiver: drop --no-rx")
    if cfg.ion_rolling:
        cfg.ionogram = True                   # same sweep settings, different loop
    if cfg.s2s is None:                       # range is the knob, S2S follows
        cfg.s2s = int(round(s2s_from_range(cfg.range_km, cfg.chip_overhead_us)))
    chip_us = CODE_LEN.get(str(cfg.mod).upper(), 13) * cfg.bit_us
    if cfg.s2s < chip_us * 1.5:
        ap.error(f"range {cfg.range_km:.0f} km gives a {cfg.s2s} us chip interval, "
                 f"shorter than 1.5 x the {chip_us:.0f} us chip - raise --range-km")
    cfg.offset_hz = cfg.offset_khz * 1e3
    cfg.freq_list = [float(x) * 1e6 for x in cfg.freqs.split(",") if x.strip()]
    if not cfg.freq_list:
        ap.error("--freqs is empty")
    return cfg


def validate(cfg):
    if not any(lo <= cfg.rate <= hi for lo, hi in RTL_RATE_RANGES):
        log(f"WARNING: {cfg.rate/1e3:.1f} kS/s is outside the RTL-SDR ranges "
            "225.001-300 kS/s and 900.001-3200 kS/s - the dongle may refuse it")
    nyq = cfg.rate / 2.0
    bw = 2.0 / (cfg.bit_us * 1e-6)
    if cfg.offset_hz + bw / 2 > 0.9 * nyq:
        log(f"WARNING: offset {cfg.offset_hz/1e3:.0f} kHz + half bandwidth "
            f"{bw/2e3:.0f} kHz is close to Nyquist ({nyq/1e3:.0f} kHz) - "
            "lower --offset-khz or raise --rate")
    if cfg.offset_hz < 10e3:
        log("WARNING: offset below 10 kHz puts the signal near the DC spike")
    for f in cfg.freq_list:
        if cfg.direct_samp and f > 14.4e6:
            log(f"WARNING: {f/1e6:.3f} MHz is above the 14.4 MHz direct sampling "
                "Nyquist - it will alias")


def main():
    global _LOG_FH
    cfg = parse_args()
    if cfg.log_file:
        _LOG_FH = open(cfg.log_file, "a")

    if cfg.list_devices:
        return do_list_devices()

    validate(cfg)
    ALARM.enabled = bool(getattr(cfg, "alarm", True))
    os.makedirs(cfg.out_dir, exist_ok=True)

    # TX first: it fails fast and cheaply, before the dongle is reconfigured
    pico = None
    if not cfg.no_tx:
        port = find_serial_port(cfg.port)
        pico = PicoTx(port, echo=cfg.echo_serial, requested=cfg.port)
        log(f"TX: connected to {port}")
        pico.drain(0.5)
        pico.send("TX_STOP")            # make sure firmware auto-TX is off
        pico.drain(0.3)
        pico.send("STATUS")
        if pico.wait_for([["Ionosonde Status"], ["Frequency:"]], timeout=3.0) is None:
            log("TX: WARNING - no STATUS reply, is the firmware running?")
    else:
        log("TX: disabled (--no-tx), receive only")

    if cfg.tx_only:
        if pico is None:
            raise SystemExit("--tx-only needs the Pico: drop --no-tx")
        try:
            run_tx_only(pico, cfg)
        except KeyboardInterrupt:
            log("Ctrl+C - stopping")
        finally:
            pico.stop_auto()
            pico.close()
        return 0

    rx = RtlRx(cfg.device, cfg.rate, cfg.direct_samp, cfg.gain, cfg.agc, cfg.ppm,
               cfg.digital_agc)
    try:
        rx.open()
    except BaseException:
        if pico:
            pico.close()
        raise

    log(f"Output directory: {cfg.out_dir}")

    if cfg.ionogram:
        try:
            if cfg.ion_rolling:
                run_ionogram_rolling(rx, pico, cfg)
            else:
                run_ionogram_series(rx, pico, cfg)
        except KeyboardInterrupt:
            log("Ctrl+C - shutting down")
        finally:
            ALARM.stop()
            if pico:
                pico.stop_auto()
                pico.close()
            rx.close()
        return 0

    log(f"Plan: {len(cfg.freq_list)} frequency/ies, "
        f"{'infinite' if cfg.cycles == 0 else cfg.cycles} cycles. Ctrl+C to stop.")

    seq = 0
    try:
        while cfg.cycles == 0 or seq < cfg.cycles:
            t_start = time.time()
            freq = cfg.freq_list[seq % len(cfg.freq_list)]
            seq += 1
            try:
                sound_once(rx, pico, cfg, freq, seq)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log(f"   cycle FAILED: {type(e).__name__}: {e}")
                time.sleep(2.0)
            if pico and not pico_healthy(pico):
                log("   TX link down - waiting for the Pico to come back")
                if not pico.recover(timeout=300.0):
                    log("   giving up on this cycle, will check again")
            if cfg.period > 0:
                wait = cfg.period - (time.time() - t_start)
                if wait > 0:
                    log(f"   idle {wait:.1f} s until next cycle")
                    progress(text="idle until next cycle", seconds=wait)
                    time.sleep(wait)
    except KeyboardInterrupt:
        log("Ctrl+C - shutting down")
    finally:
        if pico:
            pico.stop_auto()
            pico.close()
        rx.close()
        log(f"Done, {seq} cycle(s) run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

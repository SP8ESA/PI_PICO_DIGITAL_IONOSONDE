"""TX enabled, RX disabled: key the transmitter, never open the SDR."""
import os, pty, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ionosonde_auto as ia


class Forbidden:                       # tripwire
    def __init__(self, *a, **k):
        raise AssertionError("TX-only touched the SDR!")


ia.RtlRx = Forbidden
ia.SoapySDR = type("S", (), {"Device": staticmethod(
    lambda *a, **k: (_ for _ in ()).throw(AssertionError("SDR opened!")))})

CHIPS, S2S = 60, 4975
frame_s = CHIPS * S2S / 1e6
master, slave = pty.openpty()
seen, stop = [], threading.Event()


def firmware():
    buf, n = b"", 0
    while not stop.is_set():
        try:
            data = os.read(master, 1024)
        except OSError:
            break
        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            cmd = line.decode(errors="ignore").strip()
            if not cmd:
                continue
            seen.append(cmd)
            if cmd == "TX_ONCE":
                os.write(master, f"[TX-{n}] Started at {n} us\r\n".encode())
                time.sleep(frame_s)
                os.write(master, f"[TX-{n}] Completed\r\n".encode())
                n += 1


threading.Thread(target=firmware, daemon=True).start()
sys.argv = ["ionosonde_auto.py", "--no-rx", "--port", os.ttyname(slave),
            "--freqs", "3.5,7.022", "--chips", str(CHIPS), "--s2s", str(S2S),
            "--cycles", "4", "--chip-overhead-us", "0"]
cfg = ia.parse_args()
pico = ia.PicoTx(ia.find_serial_port(cfg.port), requested=cfg.port)
pico.drain(0.2)
sent = ia.run_tx_only(pico, cfg, stop_evt=threading.Event())
pico.close(); stop.set()

onces = [c for c in seen if c == "TX_ONCE"]
sets = [c for c in seen if c.startswith("SET FREQ=")]
ok = sent == 4 and len(onces) == 4 and len(sets) == 4
print(f"frames {sent}, TX_ONCE {len(onces)}, SET {len(sets)}")
print("RESULT:", "OK - transmitted, SDR untouched" if ok else "FAILED")
sys.exit(0 if ok else 1)

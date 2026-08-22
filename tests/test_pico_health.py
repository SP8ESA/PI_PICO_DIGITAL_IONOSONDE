"""A dead or unplugged Pico must be noticed, not transmitted into the void."""
import os, pty, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ionosonde_auto as ia

master, slave = pty.openpty()
port = os.ttyname(slave)
answering = threading.Event(); answering.set()
stop = threading.Event()


def firmware():
    buf = b""
    while not stop.is_set():
        try:
            data = os.read(master, 1024)
        except OSError:
            break
        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            cmd = line.decode(errors="ignore").strip()
            if cmd == "STATUS" and answering.is_set():
                os.write(master, b"\r\n=== Ionosonde Status ===\r\n"
                                 b"Frequency: 3.822000 MHz\r\nChip Count: 2048\r\n")


threading.Thread(target=firmware, daemon=True).start()
pico = ia.PicoTx(port, requested=port)
pico.drain(0.2)

print("firmware odpowiada  -> alive():", pico.alive())
assert pico.alive() is True

answering.clear()                      # Pico wisi: port jest, odpowiedzi nie ma
t0 = time.time()
hung = pico.alive(timeout=1.0)
print(f"firmware zawieszony -> alive(): {hung}  (sprawdzone w {time.time()-t0:.1f} s)")
assert hung is False

answering.set()
pico.ok = True
assert pico.alive() is True
print("po ożyciu           -> alive():", True)

# a health gate must fail when recovery cannot work (port gone for good)
os.close(slave); os.close(master); stop.set()
pico.ok = True
t0 = time.time()
gone = pico.alive(timeout=1.0)
print(f"port zniknął        -> alive(): {gone}  ({time.time()-t0:.2f} s)")
assert gone is False

healthy = ia.pico_healthy(pico)
print("pico_healthy() po utracie portu:", healthy, "(recover ma się nie udać)")
assert healthy is False
print("\nRESULT: OK - hang, unplug and failed recovery all detected")

#!/usr/bin/env python3
"""Run every check: python3 tests/run_all.py"""
import glob, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
fails = []
for t in sorted(glob.glob(os.path.join(HERE, "test_*.py"))):
    name = os.path.basename(t)
    print(f"--- {name} ", end="", flush=True)
    t0 = time.time()
    p = subprocess.run([sys.executable, t], capture_output=True, text=True)
    ok = p.returncode == 0
    print(f"{'PASS' if ok else 'FAIL'} ({time.time()-t0:.0f} s)")
    if not ok:
        fails.append(name)
        print((p.stdout + p.stderr)[-1500:])
print(f"\n{'ALL PASSED' if not fails else 'FAILED: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)

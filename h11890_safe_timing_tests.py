#!/usr/bin/env python3
"""
h11890_safe_timing_tests.py

Safe standalone timing test for the 32-bit Hamamatsu H11890api.dll.

This version:
- NEVER calls H11890CountStop().
- Runs each risky test in a separate child process.
- Gives each child a timeout, so one blocking DLL call cannot hang the whole test.
- Runs RN=1 tests first.
- Runs RN=0 continuous mode last.

No GUI, no pmt_helper.py, no JSON bridge.
HV stays OFF; this is a timing test.

Put this file next to H11890api.dll and run with 32-bit Python:

    py -3.13-32 h11890_safe_timing_tests.py
"""

import ctypes
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

DLL_NAME = "H11890api.dll"
IT_MS = 1
HV_ON = True

RN1_TRIALS = 20
CONTINUOUS_READS = 20
WORKER_TIMEOUT_S = 8.0


class H11890_INF(ctypes.Structure):
    _fields_ = [
        ("hDeviceHandle", ctypes.c_void_p),
        ("cSerialNumber", ctypes.c_char * 10),
        ("IT", ctypes.c_uint32),
        ("RN", ctypes.c_uint32),
        ("HVON", ctypes.c_int32),
    ]


def ms(ns):
    return ns / 1_000_000.0


def load_dll():
    bits = ctypes.sizeof(ctypes.c_void_p) * 8
    if bits != 32:
        raise RuntimeError(
            f"Running under {bits}-bit Python. H11890api.dll is 32-bit.\n"
            "Use 32-bit Python, e.g.:\n"
            "  py -3.13-32 h11890_safe_timing_tests.py"
        )

    here = Path(__file__).resolve().parent
    dll_path = here / DLL_NAME
    if not dll_path.exists():
        raise FileNotFoundError(f"{DLL_NAME} not found next to this script.")

    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(str(here))

    dll = ctypes.WinDLL(str(dll_path))

    dll.H11890OpenDevices.argtypes = [ctypes.POINTER(H11890_INF)]
    dll.H11890OpenDevices.restype = ctypes.c_uint32

    dll.H11890CloseDevices.argtypes = [ctypes.POINTER(H11890_INF)]
    dll.H11890CloseDevices.restype = None

    dll.H11890SetInf.argtypes = [ctypes.POINTER(H11890_INF)]
    dll.H11890SetInf.restype = ctypes.c_int32

    dll.H11890ReadInf.argtypes = [ctypes.POINTER(H11890_INF)]
    dll.H11890ReadInf.restype = ctypes.c_int32

    dll.H11890CountStart.argtypes = [ctypes.c_void_p, ctypes.c_int32]
    dll.H11890CountStart.restype = ctypes.c_int32

    dll.H11890ReadData.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_int32),
    ]
    dll.H11890ReadData.restype = ctypes.c_uint32

    return dll


def open_first(dll):
    devices = (H11890_INF * 16)()
    n = int(dll.H11890OpenDevices(devices))
    if n <= 0:
        raise RuntimeError("No H11890 device found.")

    dev = devices[0]
    serial = bytes(dev.cSerialNumber).split(b"\x00", 1)[0].decode(
        "ascii", errors="ignore"
    )
    return devices, dev, serial


def set_config(dll, dev, rn):
    dev.IT = IT_MS
    dev.RN = rn
    dev.HVON = 1 if HV_ON else 0

    if not dll.H11890SetInf(ctypes.byref(dev)):
        raise RuntimeError(f"H11890SetInf failed for RN={rn}")

    if not dll.H11890ReadInf(ctypes.byref(dev)):
        raise RuntimeError("H11890ReadInf failed")

    if int(dev.IT) != IT_MS or int(dev.RN) != rn:
        raise RuntimeError(
            f"Settings mismatch: requested IT={IT_MS}, RN={rn}; "
            f"device reports IT={dev.IT}, RN={dev.RN}"
        )


def read_once(dll, handle):
    gate = ctypes.c_uint32(0)
    buf = (ctypes.c_uint32 * 16)()
    old = ctypes.c_int32(0)

    t0 = time.perf_counter_ns()
    n = int(
        dll.H11890ReadData(
            handle,
            ctypes.byref(gate),
            buf,
            ctypes.byref(old),
        )
    )
    t1 = time.perf_counter_ns()

    used = n if 0 < n <= 16 else 16
    values = [int(buf[i]) for i in range(used)]

    return {
        "read_ms": ms(t1 - t0),
        "n": n,
        "gate": int(gate.value),
        "old": int(old.value),
        "values": values,
        "return_ns": t1,
    }


def worker_rn1(wait_before_read_ms):
    dll = load_dll()
    devices, dev, serial = open_first(dll)

    rows = []

    for i in range(RN1_TRIALS):
        set_config(dll, dev, rn=1)

        t_start0 = time.perf_counter_ns()
        ok = int(dll.H11890CountStart(dev.hDeviceHandle, 0))
        t_start1 = time.perf_counter_ns()

        if not ok:
            raise RuntimeError(f"CountStart failed on trial {i + 1}")

        actual_wait_ms = 0.0
        if wait_before_read_ms > 0:
            tw0 = time.perf_counter_ns()
            time.sleep(wait_before_read_ms / 1000.0)
            tw1 = time.perf_counter_ns()
            actual_wait_ms = ms(tw1 - tw0)

        r = read_once(dll, dev.hDeviceHandle)

        rows.append({
            "trial": i + 1,
            "start_call_ms": ms(t_start1 - t_start0),
            "requested_wait_ms": wait_before_read_ms,
            "actual_wait_ms": actual_wait_ms,
            "read_call_ms": r["read_ms"],
            "start_to_read_return_ms": ms(r["return_ns"] - t_start0),
            "n": r["n"],
            "gate": r["gate"],
            "old": r["old"],
            "values": r["values"],
        })

    try:
        dll.H11890CloseDevices(devices)
    except Exception:
        pass

    return {
        "serial": serial,
        "mode": "rn1_immediate" if wait_before_read_ms == 0 else "rn1_wait_1ms",
        "rows": rows,
    }


def worker_continuous():
    dll = load_dll()
    devices, dev, serial = open_first(dll)

    set_config(dll, dev, rn=0)

    t0 = time.perf_counter_ns()
    ok = int(dll.H11890CountStart(dev.hDeviceHandle, 0))
    t1 = time.perf_counter_ns()
    if not ok:
        raise RuntimeError("CountStart failed in continuous mode")

    rows = []
    prev_return = None

    for i in range(CONTINUOUS_READS):
        r = read_once(dll, dev.hDeviceHandle)
        interval_ms = None if prev_return is None else ms(r["return_ns"] - prev_return)
        prev_return = r["return_ns"]

        rows.append({
            "trial": i + 1,
            "read_call_ms": r["read_ms"],
            "interval_ms": interval_ms,
            "n": r["n"],
            "gate": r["gate"],
            "old": r["old"],
            "values": r["values"],
        })

    # No CountStop() here on purpose.
    return {
        "serial": serial,
        "mode": "continuous_rn0",
        "start_call_ms": ms(t1 - t0),
        "rows": rows,
    }


def child_main(mode):
    try:
        if mode == "rn1_now":
            result = worker_rn1(0.0)
        elif mode == "rn1_wait":
            result = worker_rn1(1.0)
        elif mode == "continuous":
            result = worker_continuous()
        else:
            raise RuntimeError(f"Unknown worker mode: {mode}")

        print("RESULT_JSON=" + json.dumps(result), flush=True)

        if mode == "continuous":
            os._exit(0)

    except Exception as e:
        print("ERROR_JSON=" + json.dumps({"error": str(e)}), flush=True)
        os._exit(2)


def run_worker(mode, timeout_s=WORKER_TIMEOUT_S):
    cmd = [sys.executable, str(Path(__file__).resolve()), "--worker", mode]

    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return None, f"TIMEOUT after {timeout_s:.1f} s"

    result = None
    error = None

    for line in p.stdout.splitlines():
        if line.startswith("RESULT_JSON="):
            result = json.loads(line[len("RESULT_JSON="):])
        elif line.startswith("ERROR_JSON="):
            error = json.loads(line[len("ERROR_JSON="):]).get("error")

    if result is None and error is None:
        error = (
            f"Worker exited with code {p.returncode} without a result.\n"
            f"stdout:\n{p.stdout}\n"
            f"stderr:\n{p.stderr}"
        )

    return result, error


def stat(values):
    values = [float(v) for v in values]
    return {
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
    }


def print_rn1(title, result):
    print("\n" + "=" * 82)
    print(title)
    print("=" * 82)
    print(f"Serial: {result['serial']}")
    print("trial | start ms | wait ms | ReadData ms | START->return ms | n | gate")
    print("-" * 82)

    for r in result["rows"]:
        print(
    		f"{r['trial']:5d} | "
    		f"{r['start_call_ms']:8.3f} | "
    		f"{r['actual_wait_ms']:7.3f} | "
    		f"{r['read_call_ms']:11.3f} | "
    		f"{r['start_to_read_return_ms']:16.3f} | "
    		f"{r['n']:2d} | "
    		f"{r['gate']} | "
    		f"data={r['values']}"
	)

    s = stat([r["start_to_read_return_ms"] for r in result["rows"]])
    rd = stat([r["read_call_ms"] for r in result["rows"]])

    print("-" * 82)
    print(
        "START -> ReadData return: "
        f"mean={s['mean']:.3f} ms, median={s['median']:.3f} ms, "
        f"min={s['min']:.3f} ms, max={s['max']:.3f} ms"
    )
    print(
        "ReadData call itself:     "
        f"mean={rd['mean']:.3f} ms, median={rd['median']:.3f} ms"
    )
    print("n values:", sorted(set(r["n"] for r in result["rows"])))


def print_continuous(result):
    print("\n" + "=" * 82)
    print("TEST C — IT=1 ms, RN=0, CONTINUOUS (RUN LAST)")
    print("=" * 82)
    print(f"Serial: {result['serial']}")
    print(f"CountStart API call: {result['start_call_ms']:.3f} ms")
    print("trial | ReadData ms | interval ms | n | gate")
    print("-" * 62)

    for r in result["rows"]:
        interval = "---" if r["interval_ms"] is None else f"{r['interval_ms']:.3f}"
        print(
            f"{r['trial']:5d} | "
            f"{r['read_call_ms']:11.3f} | "
            f"{interval:>11} | "
            f"{r['n']:2d} | "
            f"{r['gate']}"
        )

    rd = stat([r["read_call_ms"] for r in result["rows"]])
    intervals = [r["interval_ms"] for r in result["rows"] if r["interval_ms"] is not None]
    iv = stat(intervals)

    print("-" * 62)
    print(f"ReadData: mean={rd['mean']:.3f} ms, median={rd['median']:.3f} ms")
    print(f"Batch interval: mean={iv['mean']:.3f} ms, median={iv['median']:.3f} ms")
    print("n values:", sorted(set(r["n"] for r in result["rows"])))


def parent_main():
    bits = ctypes.sizeof(ctypes.c_void_p) * 8
    print("=" * 82)
    print("H11890 SAFE 1 ms TIMING TEST")
    print("=" * 82)
    print(f"Python: {bits}-bit")
    print("No CountStop() is used.")
    print("Each test is isolated in a child process with a timeout.")
    print()

    if bits != 32:
        raise RuntimeError(
            "Run this file with the 32-bit Python used for H11890api.dll."
        )

    result, error = run_worker("rn1_now")
    if error:
        print("TEST A (RN=1 immediate):", error)
    else:
        print_rn1("TEST A — IT=1 ms, RN=1: CountStart -> ReadData immediately", result)

    time.sleep(0.5)

    result, error = run_worker("rn1_wait")
    if error:
        print("\nTEST B (RN=1 wait 1 ms):", error)
    else:
        print_rn1("TEST B — IT=1 ms, RN=1: CountStart -> wait ~1 ms -> ReadData", result)

    time.sleep(0.5)

    result, error = run_worker("continuous")
    if error:
        print("\nTEST C (RN=0 continuous):", error)
    else:
        print_continuous(result)

    print("\n" + "=" * 82)
    print("FINISHED")
    print("=" * 82)
    print(
        "The key number is TEST A's START -> ReadData return time.\n"
        "If it is ~1-2 ms with n=1, one software-started 1 ms gate can be read\n"
        "per pixel without waiting for a 15-gate continuous batch."
    )
    print(
        "\nIf the PMT is not immediately detected on the next run, unplug/replug "
        "its USB cable once; the continuous test intentionally exits without "
        "calling the DLL's blocking CountStop()."
    )


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        child_main(sys.argv[2])
    else:
        try:
            parent_main()
        except Exception as e:
            print("\nERROR:", e)

        input("\nPress Enter to exit...")

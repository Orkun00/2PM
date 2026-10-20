#!/usr/bin/env python3
"""
h11890_all_timing_tests.py

Standalone timing/behavior test for Hamamatsu H11890api.dll (32-bit).

NO GUI
NO pmt_helper.py
NO subprocess / JSON bridge

Put this file next to H11890api.dll and run it with 32-bit Python, e.g.:

    py -3.13-32 h11890_all_timing_tests.py

The script tests four cases:

A) RN=0, IT=1 ms, continuous acquisition
   CountStart once -> repeated ReadData
   Purpose: see how often batches arrive and how many values each call returns.

B) RN=1, IT=1 ms, immediate read
   CountStart -> ReadData immediately
   Purpose: test whether a single 1 ms gate can be started and read directly.

C) RN=1, IT=1 ms, wait 1 ms before read
   CountStart -> sleep ~1 ms -> ReadData
   Purpose: see whether explicitly allowing the gate to finish changes behavior.

D) RN=1, IT=1 ms, polling
   CountStart -> repeatedly ReadData until data appears / timeout
   Purpose: test whether ReadData is non-blocking and must be polled.

All tests measure DLL/API timing with time.perf_counter_ns().

IMPORTANT
---------
- The H11890 DLL is 32-bit. This script refuses to run under 64-bit Python.
- HV is OFF by default. Timing tests normally do not need PMT high voltage.
- The script does not assume that "n=15" means 15 valid pixels. It only reports
  exactly what H11890ReadData returns.
"""

import ctypes
import csv
import os
import statistics
import sys
import time
from collections import Counter
from pathlib import Path


# =====================================================================
# USER SETTINGS
# =====================================================================

DLL_NAME = "H11890api.dll"

IT_MS = 1
HV_ON = False

WARMUP_TRIALS = 3
TRIALS = 100

POLL_TIMEOUT_MS = 100.0
POLL_SLEEP_US = 0          # 0 = poll as fast as possible

CSV_NAME = "h11890_all_timing_results.csv"

# =====================================================================


class H11890_INF(ctypes.Structure):
    _fields_ = [
        ("hDeviceHandle", ctypes.c_void_p),
        ("cSerialNumber", ctypes.c_char * 10),
        ("IT", ctypes.c_uint32),
        ("RN", ctypes.c_uint32),
        ("HVON", ctypes.c_int32),
    ]


def ns_to_ms(ns):
    return ns / 1_000_000.0


def fmt_ms(x):
    if x is None:
        return "---"
    return f"{x:.3f}"


def percentile(values, p):
    if not values:
        return None
    s = sorted(values)
    idx = round((len(s) - 1) * p)
    idx = max(0, min(idx, len(s) - 1))
    return s[idx]


def print_summary(name, values):
    values = [v for v in values if v is not None]
    if not values:
        print(f"{name}: no values")
        return

    print(
        f"{name}: "
        f"mean={statistics.mean(values):.3f} ms, "
        f"median={statistics.median(values):.3f} ms, "
        f"min={min(values):.3f} ms, "
        f"p95={percentile(values, 0.95):.3f} ms, "
        f"max={max(values):.3f} ms"
    )


def load_dll():
    bits = ctypes.sizeof(ctypes.c_void_p) * 8

    if bits != 32:
        raise RuntimeError(
            f"Running under {bits}-bit Python.\n"
            "H11890api.dll is 32-bit. Use 32-bit Python, for example:\n"
            "    py -3.13-32 h11890_all_timing_tests.py"
        )

    here = Path(__file__).resolve().parent
    dll_path = here / DLL_NAME

    if not dll_path.exists():
        raise FileNotFoundError(
            f"{DLL_NAME} not found next to this script:\n{dll_path}"
        )

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

    dll.H11890CountStop.argtypes = [ctypes.c_void_p]
    dll.H11890CountStop.restype = ctypes.c_int32

    dll.H11890ReadData.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),  # GateNum
        ctypes.POINTER(ctypes.c_uint32),  # DataBuf
        ctypes.POINTER(ctypes.c_int32),   # OLD / excessive light flag
    ]
    dll.H11890ReadData.restype = ctypes.c_uint32

    return dll


def read_data(dll, handle):
    """
    Perform one H11890ReadData() call and report the raw API result.
    """
    gate_num = ctypes.c_uint32(0)
    data_buf = (ctypes.c_uint32 * 16)()
    old = ctypes.c_int32(0)

    t0 = time.perf_counter_ns()
    n_raw = dll.H11890ReadData(
        handle,
        ctypes.byref(gate_num),
        data_buf,
        ctypes.byref(old),
    )
    t1 = time.perf_counter_ns()

    # Restype is DWORD/uint32. Keep both raw and signed representations
    # to make error-looking return values obvious.
    n_unsigned = int(n_raw)
    n_signed = ctypes.c_int32(n_raw).value

    # Buffer only has 16 slots in the documented API pattern.
    if 0 < n_unsigned <= 16:
        values = [int(data_buf[i]) for i in range(n_unsigned)]
    else:
        values = [int(data_buf[i]) for i in range(16)]

    return {
        "read_ms": ns_to_ms(t1 - t0),
        "n_unsigned": n_unsigned,
        "n_signed": n_signed,
        "gate_num": int(gate_num.value),
        "old": int(old.value),
        "values": values,
        "return_ns": t1,
    }


def stop_quietly(dll, handle):
    try:
        dll.H11890CountStop(handle)
    except Exception:
        pass


def configure(dll, dev, rn):
    """
    Configure IT, RN, HV directly in the H11890_INF structure.
    """
    stop_quietly(dll, dev.hDeviceHandle)
    time.sleep(0.01)

    dev.IT = int(IT_MS)
    dev.RN = int(rn)
    dev.HVON = 1 if HV_ON else 0

    if not dll.H11890SetInf(ctypes.byref(dev)):
        raise RuntimeError(
            f"H11890SetInf failed (IT={IT_MS}, RN={rn}, HV={int(HV_ON)})"
        )

    if not dll.H11890ReadInf(ctypes.byref(dev)):
        raise RuntimeError("H11890ReadInf failed")

    if int(dev.IT) != IT_MS or int(dev.RN) != rn:
        raise RuntimeError(
            "Device did not report the requested settings: "
            f"IT={dev.IT}, RN={dev.RN}"
        )


def start_count(dll, handle):
    t0 = time.perf_counter_ns()
    ok = dll.H11890CountStart(handle, 0)  # correction = FALSE
    t1 = time.perf_counter_ns()

    if not ok:
        raise RuntimeError("H11890CountStart failed")

    return t0, t1, ns_to_ms(t1 - t0)


def stop_count_timed(dll, handle):
    t0 = time.perf_counter_ns()
    ok = dll.H11890CountStop(handle)
    t1 = time.perf_counter_ns()
    return bool(ok), ns_to_ms(t1 - t0)


def append_row(rows, **kwargs):
    base = {
        "test": "",
        "trial": "",
        "warmup": "",
        "start_ms": "",
        "intentional_wait_ms": "",
        "read_ms": "",
        "start_to_read_return_ms": "",
        "batch_interval_ms": "",
        "poll_count": "",
        "stop_ms": "",
        "full_cycle_ms": "",
        "n_unsigned": "",
        "n_signed": "",
        "gate_num": "",
        "old": "",
        "values": "",
    }
    base.update(kwargs)
    rows.append(base)


# =====================================================================
# TEST A
# =====================================================================

def test_a_continuous(dll, dev, rows):
    print("\n" + "=" * 88)
    print("TEST A — CONTINUOUS: IT=1 ms, RN=0")
    print("CountStart once -> repeated ReadData")
    print("=" * 88)

    configure(dll, dev, rn=0)

    _, start_return_ns, start_ms = start_count(dll, dev.hDeviceHandle)
    print(f"CountStart API call: {start_ms:.3f} ms")

    total = WARMUP_TRIALS + TRIALS
    previous_return_ns = None

    measured_read = []
    measured_interval = []
    measured_n = []

    for i in range(total):
        r = read_data(dll, dev.hDeviceHandle)

        interval_ms = None
        if previous_return_ns is not None:
            interval_ms = ns_to_ms(r["return_ns"] - previous_return_ns)
        previous_return_ns = r["return_ns"]

        warmup = i < WARMUP_TRIALS
        trial = i - WARMUP_TRIALS + 1

        if not warmup:
            measured_read.append(r["read_ms"])
            if interval_ms is not None:
                measured_interval.append(interval_ms)
            measured_n.append(r["n_unsigned"])

        label = f"W{i+1}" if warmup else f"{trial:03d}"
        print(
            f"{label:>4}  "
            f"read={r['read_ms']:8.3f} ms   "
            f"interval={fmt_ms(interval_ms):>8} ms   "
            f"n={r['n_unsigned']:>10}   "
            f"gate={r['gate_num']:>8}   "
            f"data={r['values'][:4]}"
        )

        append_row(
            rows,
            test="A_continuous_RN0",
            trial=i + 1 if warmup else trial,
            warmup=int(warmup),
            start_ms=start_ms if i == 0 else "",
            read_ms=r["read_ms"],
            batch_interval_ms="" if interval_ms is None else interval_ms,
            n_unsigned=r["n_unsigned"],
            n_signed=r["n_signed"],
            gate_num=r["gate_num"],
            old=r["old"],
            values=" ".join(map(str, r["values"])),
        )

    ok, stop_ms = stop_count_timed(dll, dev.hDeviceHandle)

    print("\nA summary:")
    print_summary("ReadData call duration", measured_read)
    print_summary("Returned-batch interval", measured_interval)
    print(f"n distribution: {dict(Counter(measured_n))}")
    print(f"CountStop: {stop_ms:.3f} ms, ok={ok}")


# =====================================================================
# TEST B
# =====================================================================

def test_b_single_immediate(dll, dev, rows):
    print("\n" + "=" * 88)
    print("TEST B — SINGLE GATE IMMEDIATE READ: IT=1 ms, RN=1")
    print("CountStart -> ReadData immediately")
    print("=" * 88)

    total = WARMUP_TRIALS + TRIALS

    start_times = []
    read_times = []
    start_to_return = []
    stop_times = []
    full_cycles = []
    measured_n = []

    for i in range(total):
        configure(dll, dev, rn=1)

        cycle0 = time.perf_counter_ns()
        start_t0, _, start_ms = start_count(dll, dev.hDeviceHandle)

        r = read_data(dll, dev.hDeviceHandle)

        ok, stop_ms = stop_count_timed(dll, dev.hDeviceHandle)
        cycle1 = time.perf_counter_ns()

        total_ms = ns_to_ms(cycle1 - cycle0)
        start_to_read_ms = ns_to_ms(r["return_ns"] - start_t0)

        warmup = i < WARMUP_TRIALS
        trial = i - WARMUP_TRIALS + 1

        if not warmup:
            start_times.append(start_ms)
            read_times.append(r["read_ms"])
            start_to_return.append(start_to_read_ms)
            stop_times.append(stop_ms)
            full_cycles.append(total_ms)
            measured_n.append(r["n_unsigned"])

        label = f"W{i+1}" if warmup else f"{trial:03d}"

        print(
            f"{label:>4}  "
            f"start={start_ms:7.3f} ms   "
            f"read={r['read_ms']:8.3f} ms   "
            f"START->return={start_to_read_ms:8.3f} ms   "
            f"n={r['n_unsigned']:>10}   "
            f"gate={r['gate_num']:>8}   "
            f"stop={stop_ms:7.3f} ms"
        )

        append_row(
            rows,
            test="B_RN1_immediate",
            trial=i + 1 if warmup else trial,
            warmup=int(warmup),
            start_ms=start_ms,
            read_ms=r["read_ms"],
            start_to_read_return_ms=start_to_read_ms,
            stop_ms=stop_ms,
            full_cycle_ms=total_ms,
            n_unsigned=r["n_unsigned"],
            n_signed=r["n_signed"],
            gate_num=r["gate_num"],
            old=r["old"],
            values=" ".join(map(str, r["values"])),
        )

    print("\nB summary:")
    print_summary("CountStart call", start_times)
    print_summary("ReadData call", read_times)
    print_summary("CountStart -> ReadData return", start_to_return)
    print_summary("CountStop call", stop_times)
    print_summary("Full test cycle", full_cycles)
    print(f"n distribution: {dict(Counter(measured_n))}")


# =====================================================================
# TEST C
# =====================================================================

def test_c_single_wait_1ms(dll, dev, rows):
    print("\n" + "=" * 88)
    print("TEST C — SINGLE GATE + 1 ms WAIT: IT=1 ms, RN=1")
    print("CountStart -> wait ~1 ms -> ReadData")
    print("=" * 88)

    total = WARMUP_TRIALS + TRIALS

    waits = []
    read_times = []
    start_to_return = []
    full_cycles = []
    measured_n = []

    for i in range(total):
        configure(dll, dev, rn=1)

        cycle0 = time.perf_counter_ns()
        start_t0, _, start_ms = start_count(dll, dev.hDeviceHandle)

        wait0 = time.perf_counter_ns()
        time.sleep(IT_MS / 1000.0)
        wait1 = time.perf_counter_ns()
        actual_wait_ms = ns_to_ms(wait1 - wait0)

        r = read_data(dll, dev.hDeviceHandle)

        ok, stop_ms = stop_count_timed(dll, dev.hDeviceHandle)
        cycle1 = time.perf_counter_ns()

        start_to_read_ms = ns_to_ms(r["return_ns"] - start_t0)
        full_ms = ns_to_ms(cycle1 - cycle0)

        warmup = i < WARMUP_TRIALS
        trial = i - WARMUP_TRIALS + 1

        if not warmup:
            waits.append(actual_wait_ms)
            read_times.append(r["read_ms"])
            start_to_return.append(start_to_read_ms)
            full_cycles.append(full_ms)
            measured_n.append(r["n_unsigned"])

        label = f"W{i+1}" if warmup else f"{trial:03d}"

        print(
            f"{label:>4}  "
            f"start={start_ms:7.3f} ms   "
            f"actual_wait={actual_wait_ms:7.3f} ms   "
            f"read={r['read_ms']:8.3f} ms   "
            f"START->return={start_to_read_ms:8.3f} ms   "
            f"n={r['n_unsigned']:>10}   "
            f"gate={r['gate_num']:>8}"
        )

        append_row(
            rows,
            test="C_RN1_wait_1ms",
            trial=i + 1 if warmup else trial,
            warmup=int(warmup),
            start_ms=start_ms,
            intentional_wait_ms=actual_wait_ms,
            read_ms=r["read_ms"],
            start_to_read_return_ms=start_to_read_ms,
            stop_ms=stop_ms,
            full_cycle_ms=full_ms,
            n_unsigned=r["n_unsigned"],
            n_signed=r["n_signed"],
            gate_num=r["gate_num"],
            old=r["old"],
            values=" ".join(map(str, r["values"])),
        )

    print("\nC summary:")
    print_summary("Actual Python 1 ms sleep", waits)
    print_summary("ReadData after wait", read_times)
    print_summary("CountStart -> ReadData return", start_to_return)
    print_summary("Full test cycle", full_cycles)
    print(f"n distribution: {dict(Counter(measured_n))}")


# =====================================================================
# TEST D
# =====================================================================

def test_d_single_poll(dll, dev, rows):
    print("\n" + "=" * 88)
    print("TEST D — SINGLE GATE POLLING: IT=1 ms, RN=1")
    print("CountStart -> repeatedly ReadData until API reports data / timeout")
    print("=" * 88)

    total = WARMUP_TRIALS + TRIALS

    latencies = []
    poll_counts = []
    full_cycles = []
    final_ns = []

    for i in range(total):
        configure(dll, dev, rn=1)

        cycle0 = time.perf_counter_ns()
        start_t0, _, start_ms = start_count(dll, dev.hDeviceHandle)

        polls = 0
        total_read_ms = 0.0
        last = None

        deadline_ns = start_t0 + int(POLL_TIMEOUT_MS * 1_000_000)

        while True:
            polls += 1
            last = read_data(dll, dev.hDeviceHandle)
            total_read_ms += last["read_ms"]

            # We do not know a priori whether the DLL returns 1, 15, or
            # another documented-success value in RN=1 mode.
            # Treat 1..16 as "data-shaped" success and report the exact n.
            if 1 <= last["n_unsigned"] <= 16:
                break

            if time.perf_counter_ns() >= deadline_ns:
                break

            if POLL_SLEEP_US > 0:
                time.sleep(POLL_SLEEP_US / 1_000_000.0)

        ok, stop_ms = stop_count_timed(dll, dev.hDeviceHandle)
        cycle1 = time.perf_counter_ns()

        latency_ms = ns_to_ms(last["return_ns"] - start_t0)
        full_ms = ns_to_ms(cycle1 - cycle0)

        warmup = i < WARMUP_TRIALS
        trial = i - WARMUP_TRIALS + 1

        if not warmup:
            latencies.append(latency_ms)
            poll_counts.append(polls)
            full_cycles.append(full_ms)
            final_ns.append(last["n_unsigned"])

        label = f"W{i+1}" if warmup else f"{trial:03d}"

        print(
            f"{label:>4}  "
            f"START->data={latency_ms:8.3f} ms   "
            f"polls={polls:5d}   "
            f"sum(ReadData time)={total_read_ms:8.3f} ms   "
            f"n={last['n_unsigned']:>10}   "
            f"gate={last['gate_num']:>8}   "
            f"stop={stop_ms:7.3f} ms"
        )

        append_row(
            rows,
            test="D_RN1_poll",
            trial=i + 1 if warmup else trial,
            warmup=int(warmup),
            start_ms=start_ms,
            read_ms=total_read_ms,
            start_to_read_return_ms=latency_ms,
            poll_count=polls,
            stop_ms=stop_ms,
            full_cycle_ms=full_ms,
            n_unsigned=last["n_unsigned"],
            n_signed=last["n_signed"],
            gate_num=last["gate_num"],
            old=last["old"],
            values=" ".join(map(str, last["values"])),
        )

    print("\nD summary:")
    print_summary("CountStart -> first data-shaped return", latencies)
    if poll_counts:
        print(
            "Poll count: "
            f"mean={statistics.mean(poll_counts):.2f}, "
            f"median={statistics.median(poll_counts):.2f}, "
            f"min={min(poll_counts)}, "
            f"max={max(poll_counts)}"
        )
    print_summary("Full test cycle", full_cycles)
    print(f"final n distribution: {dict(Counter(final_ns))}")


# =====================================================================
# CSV + MAIN
# =====================================================================

def save_csv(rows):
    path = Path(__file__).resolve().parent / CSV_NAME

    fields = [
        "test",
        "trial",
        "warmup",
        "start_ms",
        "intentional_wait_ms",
        "read_ms",
        "start_to_read_return_ms",
        "batch_interval_ms",
        "poll_count",
        "stop_ms",
        "full_cycle_ms",
        "n_unsigned",
        "n_signed",
        "gate_num",
        "old",
        "values",
    ]

    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    print(f"\nCSV saved to:\n{path}")


def main():
    print("=" * 88)
    print("HAMAMATSU H11890 — COMPLETE 1 ms TIMING TEST")
    print("=" * 88)
    print(f"Python bitness : {ctypes.sizeof(ctypes.c_void_p) * 8}-bit")
    print(f"Gate time      : {IT_MS} ms")
    print(f"HV             : {'ON' if HV_ON else 'OFF'}")
    print(f"Trials/test    : {TRIALS} (+ {WARMUP_TRIALS} warm-up)")
    print()

    dll = load_dll()

    devices = (H11890_INF * 16)()
    count = int(dll.H11890OpenDevices(devices))

    if count <= 0:
        raise RuntimeError("No H11890 device found.")

    dev = devices[0]

    serial = bytes(dev.cSerialNumber).split(b"\x00", 1)[0].decode(
        "ascii", errors="ignore"
    )

    print(f"Devices found  : {count}")
    print(f"Using serial   : {serial or '(unknown)'}")

    rows = []

    try:
        test_a_continuous(dll, dev, rows)
        test_b_single_immediate(dll, dev, rows)
        test_c_single_wait_1ms(dll, dev, rows)
        test_d_single_poll(dll, dev, rows)

    finally:
        stop_quietly(dll, dev.hDeviceHandle)

        try:
            dll.H11890CloseDevices(devices)
        except Exception:
            pass

        if rows:
            save_csv(rows)

    print("\n" + "=" * 88)
    print("WHAT TO SEND BACK")
    print("=" * 88)
    print(
        "Send me the terminal output or h11890_all_timing_results.csv.\n"
        "The key comparisons are:\n"
        "  A: continuous RN=0 batch interval and n\n"
        "  B: RN=1 immediate CountStart -> ReadData return time\n"
        "  C: RN=1 after an explicit ~1 ms wait\n"
        "  D: RN=1 time until the first data-shaped ReadData return\n"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("\nERROR:")
        print(exc)
        print()
        input("Press Enter to exit...")
        sys.exit(1)

    print()
    input("All tests finished. Press Enter to exit...")

#!/usr/bin/env python3
"""
h11890_manual_1ms_read.py

Manual single-shot test for Hamamatsu H11890api.dll.

Each time you press SPACE or ENTER:
    IT = 1 ms
    RN = 1
    H11890CountStart()
    wait just over 1 ms so the single hardware gate can finish
    H11890ReadData()
    print the measured photon count and timing

Use this to verify the PMT responds:
    1) Measure with your normal optical signal.
    2) Cover/block the PMT input.
    3) Press SPACE again.
    4) The count should change strongly if you are truly reading the PMT.

Requirements:
    - Windows
    - 32-bit Python
    - H11890api.dll next to this script

Run:
    py -3.13-32 h11890_manual_1ms_read.py

IMPORTANT:
    HV is enabled by this script.
    Do not expose the PMT to excessive/bright light while HV is ON.
"""

import ctypes
import os
import sys
import time
import msvcrt
from pathlib import Path


DLL_NAME = "H11890api.dll"

GATE_TIME_MS = 1
GATE_NUMBER = 1
HV_ON = True

# Your real test showed that calling ReadData immediately after CountStart
# can return zeros. Waiting ~1 ms first produced the real count in buf[0].
# This wait does NOT change the hardware integration time: IT remains 1 ms.
READ_DELAY_MS = 1.05


class H11890_INF(ctypes.Structure):
    _fields_ = [
        ("hDeviceHandle", ctypes.c_void_p),
        ("cSerialNumber", ctypes.c_char * 10),
        ("IT", ctypes.c_uint32),
        ("RN", ctypes.c_uint32),
        ("HVON", ctypes.c_int32),
    ]


def load_dll():
    bits = ctypes.sizeof(ctypes.c_void_p) * 8

    if bits != 32:
        raise RuntimeError(
            f"This is {bits}-bit Python, but H11890api.dll is 32-bit.\n"
            "Run with 32-bit Python, for example:\n"
            "    py -3.13-32 h11890_manual_1ms_read.py"
        )

    here = Path(__file__).resolve().parent
    dll_path = here / DLL_NAME

    if not dll_path.exists():
        raise FileNotFoundError(
            f"{DLL_NAME} was not found next to this script:\n{dll_path}"
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

    dll.H11890ReadData.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),  # GateNum
        ctypes.POINTER(ctypes.c_uint32),  # DataBuf
        ctypes.POINTER(ctypes.c_int32),   # OLD / excessive light
    ]
    dll.H11890ReadData.restype = ctypes.c_uint32

    return dll


def precise_wait_ms(delay_ms):
    """
    Busy-wait for a short sub-millisecond/millisecond interval.
    Windows time.sleep(0.001) can overshoot by several milliseconds.
    """
    target = time.perf_counter_ns() + int(delay_ms * 1_000_000)

    while time.perf_counter_ns() < target:
        pass


def configure(dll, dev):
    dev.IT = GATE_TIME_MS
    dev.RN = GATE_NUMBER
    dev.HVON = 1 if HV_ON else 0

    if not dll.H11890SetInf(ctypes.byref(dev)):
        raise RuntimeError(
            "H11890SetInf failed. If another program/test left the PMT busy, "
            "close it or unplug/replug the USB device once, then retry."
        )

    if not dll.H11890ReadInf(ctypes.byref(dev)):
        raise RuntimeError("H11890ReadInf failed.")

    if int(dev.IT) != GATE_TIME_MS:
        raise RuntimeError(f"Device IT is {dev.IT}, expected {GATE_TIME_MS}.")
    if int(dev.RN) != GATE_NUMBER:
        raise RuntimeError(f"Device RN is {dev.RN}, expected {GATE_NUMBER}.")
    if bool(dev.HVON) != HV_ON:
        raise RuntimeError(
            f"Device HV state is {bool(dev.HVON)}, expected {HV_ON}."
        )


def measure_once(dll, handle):
    # Start one finite, one-gate acquisition.
    t0 = time.perf_counter_ns()

    ok = dll.H11890CountStart(handle, 0)  # correction = FALSE

    t1 = time.perf_counter_ns()

    if not ok:
        raise RuntimeError("H11890CountStart failed.")

    # Let the requested 1 ms hardware gate complete before asking for data.
    wait0 = time.perf_counter_ns()
    precise_wait_ms(READ_DELAY_MS)
    wait1 = time.perf_counter_ns()

    gate_num = ctypes.c_uint32(0)
    buf = (ctypes.c_uint32 * 16)()
    over = ctypes.c_int32(0)

    read0 = time.perf_counter_ns()

    n = int(
        dll.H11890ReadData(
            handle,
            ctypes.byref(gate_num),
            buf,
            ctypes.byref(over),
        )
    )

    read1 = time.perf_counter_ns()

    # For RN=1 in your physical test, H11890ReadData still reports n=15,
    # but the actual single-gate result appeared in buf[0] and buf[1:15] were 0.
    values = [int(buf[i]) for i in range(15)]

    return {
        "count": values[0],
        "values": values,
        "n": n,
        "gate": int(gate_num.value),
        "over": int(over.value),
        "start_call_ms": (t1 - t0) / 1_000_000.0,
        "wait_ms": (wait1 - wait0) / 1_000_000.0,
        "read_ms": (read1 - read0) / 1_000_000.0,
        "total_ms": (read1 - t0) / 1_000_000.0,
    }


def turn_hv_off(dll, dev):
    """
    Best-effort HV OFF before closing.
    RN=1 is finite, so no CountStop() is used.
    """
    try:
        dev.IT = GATE_TIME_MS
        dev.RN = GATE_NUMBER
        dev.HVON = 0

        if dll.H11890SetInf(ctypes.byref(dev)):
            dll.H11890ReadInf(ctypes.byref(dev))
            return True
    except Exception:
        pass

    return False


def main():
    print("=" * 76)
    print("H11890 MANUAL 1 ms SINGLE-GATE READ TEST")
    print("=" * 76)
    print()
    print("WARNING: PMT HIGH VOLTAGE WILL BE ON.")
    print("Do NOT expose the PMT to excessive/bright light.")
    print()

    dll = load_dll()

    devices = (H11890_INF * 16)()
    n_devices = int(dll.H11890OpenDevices(devices))

    if n_devices <= 0:
        raise RuntimeError("No H11890 device found.")

    dev = devices[0]
    handle = dev.hDeviceHandle

    serial = bytes(dev.cSerialNumber).split(b"\x00", 1)[0].decode(
        "ascii", errors="ignore"
    )

    configure(dll, dev)

    print(f"Device serial : {serial or '(unknown)'}")
    print(f"Gate time     : {dev.IT} ms")
    print(f"Gate number   : {dev.RN}")
    print(f"HV            : {'ON' if dev.HVON else 'OFF'}")
    print(f"Read delay    : {READ_DELAY_MS:.2f} ms")
    print()
    print("SPACE / ENTER = take ONE 1-ms measurement")
    print("Q             = HV OFF and quit")
    print()
    print("Suggested check:")
    print("  1. Take a measurement with the current optical signal.")
    print("  2. Physically block/cover the PMT input.")
    print("  3. Take another measurement and compare COUNT.")
    print()
    print("-" * 76)

    shot = 0

    try:
        while True:
            print("\nReady. Press SPACE/ENTER to measure, Q to quit...", end="", flush=True)

            ch = msvcrt.getwch()

            if ch.lower() == "q":
                print(" Q")
                break

            if ch not in (" ", "\r"):
                continue

            print()
            shot += 1

            result = measure_once(dll, handle)

            print(f"Measurement #{shot}")
            print(f"  COUNT buf[0]       : {result['count']}")
            print(f"  Full DataBuf[0:15] : {result['values']}")
            print(f"  ReadData return n  : {result['n']}")
            print(f"  GateNum            : {result['gate']}")
            print(f"  Over-light flag    : {result['over']}")
            print()
            print(f"  CountStart call    : {result['start_call_ms']:.3f} ms")
            print(f"  Post-start wait    : {result['wait_ms']:.3f} ms")
            print(f"  ReadData call      : {result['read_ms']:.3f} ms")
            print(f"  START -> result    : {result['total_ms']:.3f} ms")

            if result["over"]:
                print("  !!! EXCESSIVE LIGHT DETECTED - block the input / reduce light !!!")

    finally:
        print()
        print("Turning PMT high voltage OFF...")

        hv_off = turn_hv_off(dll, dev)

        if hv_off:
            print("HV OFF confirmed.")
        else:
            print(
                "WARNING: Could not confirm HV OFF through SetInf. "
                "Use your normal hardware shutdown procedure."
            )

        try:
            dll.H11890CloseDevices(devices)
        except Exception as e:
            print(f"H11890CloseDevices warning: {e}")

    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print()
        print("ERROR:")
        print(exc)
        input("\nPress Enter to exit...")
        sys.exit(1)

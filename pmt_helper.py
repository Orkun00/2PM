#!/usr/bin/env python3
"""
pmt_helper.py
=============
32-bit H11890 helper for the two-photon scanner.

FAST SINGLE-GATE MODE
---------------------
The H11890api.dll is x86 (32-bit), while the GUI runs in 64-bit Python.
This helper stays alive as a 32-bit child process and exposes a tiny
JSON-over-stdio protocol.

The fast acquisition path is ONE RPC per pixel:

    {"cmd":"measure"}

Inside the helper that becomes:

    H11890CountStart()
    precise wait for IT + guard
    H11890ReadData()
    return DataBuf[0]

This matches the physical H11890 test performed on the microscope:
with IT=1 ms and RN=1, DataBuf[0] contained the single-gate count while
DataBuf[1:15] remained zero. The measured CountStart -> result latency was
typically about 1.6-1.7 ms.

Important:
- Fast mode requires RN=1 (one finite gate per CountStart).
- No H11890CountStop() is used in the normal path. It was observed to be
  capable of blocking, and RN=1 ends by itself after one gate.
- The helper does NOT call time.sleep(0.001) for the 1 ms gate delay because
  Windows scheduling produced multi-ms overshoots in testing.
- HV is still controlled by the GUI and is OFF unless requested.

Protocol:
  {"cmd":"ping"}
  {"cmd":"open","dll_path":"H11890api.dll","it":1,"rn":1,"hvon":0}
  {"cmd":"measure"}
  {"cmd":"reconfig","it":1,"rn":1,"hvon":0}
  {"cmd":"close"}
  {"cmd":"quit"}

Compatibility:
  "read" is treated as "measure", "start" and "stop" are harmless no-ops.
"""

import ctypes
import json
import os
import sys
import time


# Extra delay after the requested hardware gate duration before ReadData.
# The physical test used ~1.05 ms for a 1 ms gate and returned the real count.
READ_GUARD_MS = 0.05


class H11890_INF(ctypes.Structure):
    _fields_ = [
        ("hDeviceHandle", ctypes.c_void_p),
        ("cSerialNumber", ctypes.c_char * 10),
        ("IT", ctypes.c_uint32),
        ("RN", ctypes.c_uint32),
        ("HVON", ctypes.c_int32),
    ]


def load_h11890_dll(path):
    """Load the 32-bit H11890 DLL and its dependencies robustly."""
    bits = ctypes.sizeof(ctypes.c_void_p) * 8
    if bits != 32:
        raise OSError(
            f"pmt_helper.py is running under {bits}-bit Python, but "
            "H11890api.dll is 32-bit (x86) only. Launch with 32-bit Python, "
            "for example: py -3.13-32 pmt_helper.py"
        )

    if getattr(sys, "frozen", False):
        here = os.path.dirname(sys.executable)
    else:
        here = os.path.dirname(os.path.abspath(__file__))

    candidates = [path, os.path.abspath(path), os.path.join(here, path)]
    dll_file = next((c for c in candidates if os.path.isfile(c)), None)

    if dll_file is None:
        raise FileNotFoundError(
            "H11890api.dll not found. Looked in:\n  " + "\n  ".join(candidates)
        )

    dll_file = os.path.abspath(dll_file)
    dll_dir = os.path.dirname(dll_file)

    if hasattr(os, "add_dll_directory") and os.path.isdir(dll_dir):
        os.add_dll_directory(dll_dir)

    try:
        return ctypes.WinDLL(dll_file)
    except OSError as e:
        win = getattr(e, "winerror", None)
        msg = str(e)

        if win == 193:
            raise OSError(
                "WinError 193: bitness mismatch. H11890api.dll is 32-bit "
                "(x86), so the helper must run under 32-bit Python."
            ) from e

        if win == 126 or "or one of its dependencies" in msg:
            raise OSError(
                "WinError 126: a 32-bit dependency (normally libusb0.dll) "
                "could not be found. Install the H11890 x86 driver or place "
                "the correct 32-bit libusb0.dll where Windows can load it."
            ) from e

        raise


def precise_wait_ms(delay_ms):
    """
    Wait until a high-resolution deadline.

    For the ~1.05 ms fast path this is a pure busy-wait, avoiding the several-ms
    scheduling outliers observed with time.sleep(0.001) on the microscope PC.
    For longer waits, sleep coarsely first and spin only the final ~0.5 ms.
    """
    delay_ms = float(delay_ms)
    if delay_ms <= 0:
        return

    deadline = time.perf_counter_ns() + int(delay_ms * 1_000_000)

    # Keep the latency-critical 1 ms case entirely deterministic.
    if delay_ms > 2.0:
        coarse_ms = delay_ms - 0.5
        time.sleep(coarse_ms / 1000.0)

    while time.perf_counter_ns() < deadline:
        pass


class RealPMT:
    """ctypes wrapper around H11890api.dll in RN=1 single-gate mode."""

    def __init__(self, dll_path, it_ms=1, rn=1, hv_on=False):
        self.dll = load_h11890_dll(dll_path)
        self._bind()

        self.pmt = (H11890_INF * 16)()
        n = int(self.dll.H11890OpenDevices(self.pmt))
        if n <= 0:
            raise RuntimeError("Could not open H11890 PMT device (none found)")

        self.dev = self.pmt[0]
        self.handle = self.dev.hDeviceHandle
        self.measurement_index = 0

        self._apply_config(it_ms, rn, hv_on)

    def _bind(self):
        d = self.dll

        d.H11890OpenDevices.argtypes = [ctypes.POINTER(H11890_INF)]
        d.H11890OpenDevices.restype = ctypes.c_uint32

        d.H11890SetInf.argtypes = [ctypes.POINTER(H11890_INF)]
        d.H11890SetInf.restype = ctypes.c_int32

        d.H11890ReadInf.argtypes = [ctypes.POINTER(H11890_INF)]
        d.H11890ReadInf.restype = ctypes.c_int32

        d.H11890CountStart.argtypes = [ctypes.c_void_p, ctypes.c_int32]
        d.H11890CountStart.restype = ctypes.c_int32

        # Bound for completeness/recovery diagnostics, but intentionally unused
        # in the normal RN=1 path.
        d.H11890CountStop.argtypes = [ctypes.c_void_p]
        d.H11890CountStop.restype = ctypes.c_int32

        d.H11890ReadData.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_int32),
        ]
        d.H11890ReadData.restype = ctypes.c_uint32

        d.H11890CloseDevices.argtypes = [ctypes.POINTER(H11890_INF)]
        d.H11890CloseDevices.restype = None

    def _set_inf(self):
        if self.dll.H11890SetInf(ctypes.byref(self.dev)):
            return True
        # A short retry handles transient SetInf timing without CountStop().
        time.sleep(0.02)
        return bool(self.dll.H11890SetInf(ctypes.byref(self.dev)))

    def _apply_config(self, it_ms, rn, hv_on):
        it_ms = int(it_ms)
        rn = int(rn)

        if it_ms < 1 or it_ms > 10000:
            raise ValueError("H11890 gate time IT must be 1..10000 ms")

        if rn != 1:
            raise ValueError(
                "Fast single-gate scanner requires PMT gate number RN=1. "
                "Set 'PMT gate number' to 1."
            )

        self.dev.IT = it_ms
        self.dev.RN = 1
        self.dev.HVON = 1 if hv_on else 0

        if not self._set_inf():
            raise RuntimeError(
                "Could not configure H11890 (H11890SetInf). If the device was "
                "left in the old RN=0 continuous mode by a previous crashed "
                "program, unplug/replug its USB cable once and retry."
            )

        if not self.dll.H11890ReadInf(ctypes.byref(self.dev)):
            raise RuntimeError("Could not read H11890 configuration")

        if int(self.dev.IT) != it_ms or int(self.dev.RN) != 1:
            raise RuntimeError(
                f"H11890 configuration mismatch: IT={self.dev.IT}, RN={self.dev.RN}"
            )

    def serial(self):
        try:
            return self.dev.cSerialNumber.decode("ascii", "ignore").strip("\x00")
        except Exception:
            return ""

    def measure_once(self):
        """
        Acquire exactly one finite gate.

        Tested fast sequence:
            CountStart -> wait IT + 0.05 ms -> ReadData -> DataBuf[0]

        H11890ReadData may report n=15 for short gate times even with RN=1.
        On the physical unit, only DataBuf[0] contained the RN=1 measurement.
        """
        total_t0 = time.perf_counter_ns()

        start_t0 = total_t0
        if not self.dll.H11890CountStart(self.handle, 0):
            raise RuntimeError("Could not start H11890 single-gate measurement")
        start_t1 = time.perf_counter_ns()

        requested_wait_ms = float(self.dev.IT) + READ_GUARD_MS
        wait_t0 = time.perf_counter_ns()
        precise_wait_ms(requested_wait_ms)
        wait_t1 = time.perf_counter_ns()

        gate = ctypes.c_uint32(0)
        buf = (ctypes.c_uint32 * 16)()
        over = ctypes.c_int32(0)

        read_t0 = time.perf_counter_ns()
        n_raw = self.dll.H11890ReadData(
            self.handle,
            ctypes.byref(gate),
            buf,
            ctypes.byref(over),
        )
        read_t1 = time.perf_counter_ns()

        n = ctypes.c_int32(n_raw).value
        if n not in (1, 15):
            raise RuntimeError(f"H11890ReadData failed/unexpected return value: {n}")

        self.measurement_index += 1

        # Empirically verified for IT=1 ms, RN=1 on serial 30430560:
        # buf[0] is the actual single-gate photon count; remaining slots are 0.
        count = int(buf[0])

        return {
            "count": count,
            "over": int(over.value),
            "hw_gate": int(gate.value),
            "measurement": self.measurement_index,
            "n": int(n),
            "start_ms": (start_t1 - start_t0) / 1_000_000.0,
            "wait_ms": (wait_t1 - wait_t0) / 1_000_000.0,
            "read_ms": (read_t1 - read_t0) / 1_000_000.0,
            "elapsed_ms": (read_t1 - total_t0) / 1_000_000.0,
        }

    # Compatibility methods. The scanner now uses measure_once directly.
    def start(self):
        # RN=1 is finite and must be started separately for every pixel.
        return

    def read(self):
        return self.measure_once()

    def stop_counting(self):
        # Nothing to stop in RN=1 mode; avoiding CountStop also avoids the
        # blocking behavior observed during testing.
        return

    def reconfigure(self, it_ms, rn, hv_on):
        # stdin commands are processed sequentially; a measure command is fully
        # complete before reconfiguration can run, so no CountStop is needed.
        self._apply_config(it_ms, rn, hv_on)

    def set_hv_off_best_effort(self):
        try:
            self.dev.RN = 1
            self.dev.HVON = 0
            return bool(self._set_inf())
        except Exception:
            return False

    def close(self):
        # Normal shutdown is safe because every RN=1 measurement is finite.
        # Try to turn HV off before releasing the device. Never call CountStop().
        self.set_hv_off_best_effort()
        self.dll.H11890CloseDevices(self.pmt)


def reply(obj):
    """Write exactly one JSON reply line and flush immediately."""
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main():
    pmt = None
    bits = ctypes.sizeof(ctypes.c_void_p) * 8

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except Exception as e:
            reply({"ok": False, "error": f"bad json: {e}"})
            continue

        cmd = msg.get("cmd")

        try:
            if cmd == "ping":
                reply({
                    "ok": True,
                    "bits": bits,
                    "mode": "RN1-single-gate",
                    "guard_ms": READ_GUARD_MS,
                })

            elif cmd == "open":
                if pmt is not None:
                    try:
                        pmt.close()
                    except Exception:
                        pass
                    pmt = None

                pmt = RealPMT(
                    dll_path=msg.get("dll_path", "H11890api.dll"),
                    it_ms=int(msg.get("it", 1)),
                    rn=int(msg.get("rn", 1)),
                    hv_on=bool(msg.get("hvon", 0)),
                )
                reply({
                    "ok": True,
                    "serial": pmt.serial(),
                    "it": int(pmt.dev.IT),
                    "rn": int(pmt.dev.RN),
                    "hvon": bool(pmt.dev.HVON),
                })

            elif cmd in ("measure", "read"):
                if pmt is None:
                    reply({"ok": False, "error": "device not open"})
                else:
                    r = pmt.measure_once()
                    reply({"ok": True, **r})

            elif cmd == "start":
                # Compatibility no-op: every RN=1 pixel starts inside measure.
                if pmt is None:
                    reply({"ok": False, "error": "device not open"})
                else:
                    reply({"ok": True, "mode": "single-gate"})

            elif cmd == "stop":
                # Compatibility no-op: RN=1 acquisition is already finite.
                reply({"ok": True})

            elif cmd == "reconfig":
                if pmt is None:
                    reply({"ok": False, "error": "device not open"})
                else:
                    pmt.reconfigure(
                        int(msg.get("it", 1)),
                        int(msg.get("rn", 1)),
                        bool(msg.get("hvon", 0)),
                    )
                    reply({
                        "ok": True,
                        "it": int(pmt.dev.IT),
                        "rn": int(pmt.dev.RN),
                        "hvon": bool(pmt.dev.HVON),
                    })

            elif cmd == "close":
                if pmt is not None:
                    pmt.close()
                    pmt = None
                reply({"ok": True})

            elif cmd == "quit":
                break

            else:
                reply({"ok": False, "error": f"unknown cmd: {cmd!r}"})

        except Exception as e:
            reply({"ok": False, "error": str(e)})

    if pmt is not None:
        try:
            pmt.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()

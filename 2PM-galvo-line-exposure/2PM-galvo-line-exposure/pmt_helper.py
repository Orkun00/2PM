#!/usr/bin/env python3
"""
pmt_helper.py
=============
32-BIT side of the two-photon scanner.

H11890api.dll is x86 (32-bit) only, so it can only be loaded by a 32-bit
Python via ctypes. matplotlib no longer ships 32-bit Windows wheels, so the
GUI (two_photon_scanner.py) runs in 64-bit Python instead. This little program
is the bridge: the 64-bit GUI launches it with 32-bit Python as a child
process and talks to it over stdin/stdout using one JSON object per line.

It speaks NOTHING else on stdout -- only protocol replies -- so the parent can
parse every line. Anything informational goes to stderr.

Protocol (one JSON object per line, request -> reply):
  {"cmd":"ping"}                         -> {"ok":true,"bits":32}
  {"cmd":"open","dll_path":..,"it":1,
        "rn":0,"hvon":1}                 -> {"ok":true,"serial":".."} | {"ok":false,"error":..}
  {"cmd":"start"}                        -> {"ok":true} | {"ok":false,"error":..}
  {"cmd":"read"}                         -> {"ok":true,"count":N,"over":0,"gate":G}
  {"cmd":"close"}                        -> {"ok":true}
  {"cmd":"quit"}                         -> (process exits)

Run it directly to self-test the bitness:
  py -3.13-32 pmt_helper.py        (then type  {"cmd":"ping"}  + Enter)
"""

import os
import sys
import json
import ctypes


# ---------------------------------------------------------------------------
# H11890 ctypes binding -- matched to H11890api.h.
#   struct _H11890_INF { HANDLE hDeviceHandle; CHAR cSerialNumber[10];
#                        DWORD IT; DWORD RN; BOOL HVON; }
#   All exports are extern "C" __stdcall  -> use WinDLL.
#   H11890SetInf / H11890ReadInf take H11890_INF& -> pass ctypes.byref().
# ---------------------------------------------------------------------------
class H11890_INF(ctypes.Structure):
    _fields_ = [
        ("hDeviceHandle", ctypes.c_void_p),     # HANDLE
        ("cSerialNumber", ctypes.c_char * 10),  # CHAR cSerialNumber[10]
        ("IT", ctypes.c_uint32),                # DWORD: gate time, 1..10000 ms
        ("RN", ctypes.c_uint32),                # DWORD: gate number, 0 = continuous
        ("HVON", ctypes.c_int32),               # BOOL: high voltage on/off
    ]


def load_h11890_dll(path):
    """
    Load H11890api.dll robustly.

    Handles the two classic Windows failures:
      * Python 3.8+ does NOT add the DLL's own folder to the dependency search
        path, so sibling dependency DLLs (USB/FTDI driver, etc.) aren't found
        even when they sit right next to H11890api.dll. We add it explicitly.
      * Distinguishes 'missing dependency' (WinError 126) from 'wrong bitness'
        (WinError 193) so the error message is actually useful.
    """
    # H11890api.dll is 32-bit (x86) only -> this Python must also be 32-bit.
    bits = ctypes.sizeof(ctypes.c_void_p) * 8
    if bits != 32:
        raise OSError(
            f"pmt_helper.py is running under {bits}-bit Python, but "
            "H11890api.dll is 32-bit (x86) only. Launch the helper with 32-bit "
            "Python, e.g.  py -3.13-32 pmt_helper.py  (the GUI does this for "
            "you via the 'Helper Python' field).")

    # Resolve the path: as given, then relative to CWD, then next to this script.
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [path, os.path.abspath(path), os.path.join(here, path)]
    dll_file = next((c for c in candidates if os.path.isfile(c)), None)
    if dll_file is None:
        raise FileNotFoundError(
            "H11890api.dll not found. Looked in:\n  " + "\n  ".join(candidates))

    # Use the ABSOLUTE path. A bare/relative name (which happens when the
    # working dir already is the DLL's folder) leaves dll_dir == "" and skips
    # add_dll_directory below -> Windows then can't find libusb0.dll -> 126.
    dll_file = os.path.abspath(dll_file)
    dll_dir = os.path.dirname(dll_file)
    if hasattr(os, "add_dll_directory") and os.path.isdir(dll_dir):
        os.add_dll_directory(dll_dir)   # let Windows find the dependency DLLs

    try:
        return ctypes.WinDLL(dll_file)   # extern "C" __stdcall
    except OSError as e:
        win = getattr(e, "winerror", None)
        msg = str(e)
        if win == 193:
            raise OSError(
                "WinError 193: bitness mismatch. H11890api.dll is 32-bit (x86) "
                "ONLY, so the helper MUST run under 32-bit Python.") from e
        # Python 3.8+ sometimes reports the dependency failure as a
        # FileNotFoundError with no .winerror set, so match the text too.
        if win == 126 or "or one of its dependencies" in msg:
            raise OSError(
                "WinError 126: H11890api.dll loaded but its dependency "
                "'libusb0.dll' (32-bit) was not found. Install the H11890 USB "
                "driver (RS SampleSoftware\\driver\\UPDATE_x86.exe) -- it puts the "
                "32-bit libusb0.dll in C:\\Windows\\SysWOW64. Or copy that "
                "SysWOW64\\libusb0.dll next to this script. Do NOT use the 64-bit "
                "libusb0.dll from driver\\x64.") from e
        raise


class RealPMT:
    """ctypes wrapper around H11890api.dll (32-bit, Windows only)."""
    def __init__(self, dll_path, it_ms, rn, hv_on):
        self.dll = load_h11890_dll(dll_path)   # stdcall; CDLL if it were cdecl
        self._bind()

        self.pmt = (H11890_INF * 16)()
        n = self.dll.H11890OpenDevices(self.pmt)
        if n <= 0:
            raise RuntimeError("Could not open H11890 PMT device (none found)")

        self.dev = self.pmt[0]
        self.handle = self.dev.hDeviceHandle

        self.dev.IT = it_ms
        self.dev.RN = rn
        self.dev.HVON = 1 if hv_on else 0

        if not self.dll.H11890SetInf(ctypes.byref(self.dev)):
            raise RuntimeError("Could not configure PMT (H11890SetInf)")
        if not self.dll.H11890ReadInf(ctypes.byref(self.dev)):
            raise RuntimeError("Could not read PMT config (H11890ReadInf)")

    def serial(self):
        try:
            return self.dev.cSerialNumber.decode("ascii", "ignore").strip("\x00")
        except Exception:
            return ""

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

        d.H11890CountStop.argtypes = [ctypes.c_void_p]
        d.H11890CountStop.restype = ctypes.c_int32

        d.H11890ReadData.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),       # gateNum
            ctypes.POINTER(ctypes.c_uint32),       # dataBuf[16]
            ctypes.POINTER(ctypes.c_int32),        # overLight (BOOL)
        ]
        d.H11890ReadData.restype = ctypes.c_uint32

        d.H11890CloseDevices.argtypes = [ctypes.POINTER(H11890_INF)]
        d.H11890CloseDevices.restype = None

    def start(self):
        if not self.dll.H11890CountStart(self.handle, 0):  # FALSE
            raise RuntimeError("Could not start PMT counting")

    def read(self):
        import time as _time
        gate = ctypes.c_uint32(0)
        buf = (ctypes.c_uint32 * 16)()
        over = ctypes.c_int32(0)
        _t0 = _time.perf_counter()
        n = self.dll.H11890ReadData(self.handle, ctypes.byref(gate), buf,
                                    ctypes.byref(over))
        _usb_ms = 1e3 * (_time.perf_counter() - _t0)
        # DIAGNOSTIC: time the raw USB/DLL call only. stdout is protocol-only,
        # so log to a sibling file. Remove this block once the bottleneck is found.
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "pmt_timing.log"), "a") as _lf:
                _lf.write(f"H11890ReadData {_usb_ms:.2f} ms\n")
        except Exception:
            pass
        if ctypes.c_int32(n).value < 0:
            return -1, 0, gate.value
        # one pixel per galvo position -> take the first gate's count
        return int(buf[0]), int(over.value), int(gate.value)

    def stop_counting(self):
        """Stop counting but leave the device OPEN for the next scan."""
        self.dll.H11890CountStop(self.handle)

    def reconfigure(self, it_ms, rn, hv_on):
        """Re-apply gate time / gate number / HV without reopening the device."""
        self.dev.IT = it_ms
        self.dev.RN = rn
        self.dev.HVON = 1 if hv_on else 0
        if not self.dll.H11890SetInf(ctypes.byref(self.dev)):
            raise RuntimeError("Could not reconfigure PMT (H11890SetInf)")
        if not self.dll.H11890ReadInf(ctypes.byref(self.dev)):
            raise RuntimeError("Could not read PMT config (H11890ReadInf)")

    def close(self):
        try:
            self.dll.H11890CountStop(self.handle)
        finally:
            self.dll.H11890CloseDevices(self.pmt)


# ---------------------------------------------------------------------------
# JSON-over-stdio command loop
# ---------------------------------------------------------------------------
def reply(obj):
    """Write exactly one JSON line to stdout and flush it."""
    sys.stdout.write(json.dumps(obj) + "\n")
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
                reply({"ok": True, "bits": bits})

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
                    rn=int(msg.get("rn", 0)),
                    hv_on=bool(msg.get("hvon", 1)),
                )
                reply({"ok": True, "serial": pmt.serial()})

            elif cmd == "start":
                if pmt is None:
                    reply({"ok": False, "error": "device not open"})
                else:
                    pmt.start()
                    reply({"ok": True})

            elif cmd == "stop":
                if pmt is None:
                    reply({"ok": False, "error": "device not open"})
                else:
                    pmt.stop_counting()
                    reply({"ok": True})

            elif cmd == "reconfig":
                if pmt is None:
                    reply({"ok": False, "error": "device not open"})
                else:
                    pmt.reconfigure(int(msg.get("it", 1)), int(msg.get("rn", 0)),
                                    bool(msg.get("hvon", 1)))
                    reply({"ok": True})

            elif cmd == "read":
                if pmt is None:
                    reply({"ok": False, "error": "device not open"})
                else:
                    count, over, gate = pmt.read()
                    reply({"ok": True, "count": count, "over": over, "gate": gate})

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

    # final cleanup on quit / EOF
    if pmt is not None:
        try:
            pmt.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()

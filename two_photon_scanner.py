#!/usr/bin/env python3
"""
two_photon_scanner.py
=====================
Single-file GUI for a two-photon (2PM) microscope raster scan.

It combines the two programs you had:
  * thisWorks.cpp  -> galvo (NI-DAQ AO) + Hamamatsu H11890 PMT control
  * pmtPlot.py     -> PMT data plotting (step plot, scatter, over-light, heatmap)

into ONE Tkinter window.

What changed vs the originals
-----------------------------
* NO MORE input.csv / WeirdShape.csv. The scan path is generated on the fly as a
  centered RASTER grid (see generate_raster / pixel_to_voltage).
* The "box" is fully parametric. You set FOV X, FOV Y (the total scan angle in
  degrees for each axis) and step_size_deg in the GUI. Pixels are derived:
      pixels = round(FOV / step_size_deg) + 1
  Want a bigger area? raise the FOV. Want finer resolution? lower the step size.
* Output voltage is HARD-CLAMPED to +/- voltage_limit (default 5 V) in code, and
  the NI-DAQ AO channels are also created with that same +/- range. On top of
  that, a scan whose corners would exceed the galvo's +/-22.5 deg (+/-5 V) limit
  is REJECTED before it starts, so the requested FOV can never burn the galvo.
* Everything runs from one GUI: set params -> Start -> live heatmap -> the
  original PMT plots -> save CSV (same columns as before).
* If nidaqmx / H11890api.dll are missing (e.g. on macOS), the app drops into
  SIMULATION mode so you can build/test the GUI off the rig. A fake Gaussian
  blob is generated so the heatmap shows something.

Two-process design (why)
------------------------
H11890api.dll is 32-bit (x86) ONLY, so it can only be loaded by 32-bit Python.
But matplotlib stopped shipping 32-bit Windows wheels (last was 3.7.5 / Python
3.11), so the GUI here runs in 64-bit Python. To bridge the two:

    [ 64-bit Python ]  this GUI: Tkinter + matplotlib + galvo (nidaqmx)
            |  launches as a child process, talks JSON over stdin/stdout
    [ 32-bit Python ]  pmt_helper.py: loads H11890api.dll, drives the PMT

Only the PMT goes through the helper. The galvo (NI-DAQ) runs in-process in the
64-bit GUI just fine. See SubprocessPMT below and pmt_helper.py.

Run on the Windows rig:
    GUI side  (64-bit):  pip install nidaqmx numpy matplotlib
    PMT side  (32-bit):  nothing to install -- pmt_helper.py uses only ctypes
    Start it with:       run.bat   (or:  py -3.14 two_photon_scanner.py)
"""

import os
import sys
import csv
import json
import time
import queue
import shlex
import subprocess
import threading
from dataclasses import dataclass

import numpy as np

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

# ---------------------------------------------------------------------------
# Optional hardware libs. If they are missing we run in simulation mode.
# ---------------------------------------------------------------------------
try:
    import nidaqmx
    # The nidaqmx PACKAGE installs anywhere, but the NI-DAQmx driver does not
    # support macOS -- creating a Task would always fail. Treat that as "no
    # hardware" so the app defaults to simulation off the rig, as intended.
    HAVE_NIDAQMX = sys.platform != "darwin"
except Exception:
    HAVE_NIDAQMX = False


# ===========================================================================
# Configuration
# ===========================================================================
@dataclass
class ScanConfig:
    # --- the variables you'll tune: field of view + resolution ---
    # You give the FIELD OF VIEW in degrees (total angular width the beam
    # sweeps) for each axis, plus the step size. Pixels are derived from these:
    #     pixels = round(FOV / step) + 1
    # so the scan is a centered raster covering +/- FOV/2 (around the offset).
    fov_x_deg: float = 3.15      # total field of view along X (degrees)
    fov_y_deg: float = 3.15      # total field of view along Y (degrees)
    step_size_deg: float = 0.05  # degrees per pixel step
    x_offset_deg: float = 0.0   # shift the box center in X (degrees)
    y_offset_deg: float = 0.0   # shift the box center in Y (degrees)

    # --- galvo <-> voltage mapping (same idea as thisWorks.cpp) ---
    # SAFETY: the galvo burns past +/- voltage_limit. degree_range degrees map to
    # voltage_limit volts, so the beam must stay within +/- degree_range degrees.
    voltage_limit: float = 5.0  # +/- output limit (V). Hard clamp AND DAQ channel range.
    degree_range: float = 22.5  # degrees that correspond to voltage_limit (+/-5 V <-> +/-22.5 deg)

    @property
    def pixels_x(self) -> int:
        """Number of pixels along X, derived from FOV and step size."""
        return max(1, int(round(self.fov_x_deg / self.step_size_deg)) + 1)

    @property
    def pixels_y(self) -> int:
        """Number of pixels along Y, derived from FOV and step size."""
        return max(1, int(round(self.fov_y_deg / self.step_size_deg)) + 1)

    # --- timing ---
    settle_ms: int = 1          # wait after galvo move, before PMT read
    pmt_gate_time_ms: int = 1   # H11890 IT (gate/integration time), 1 ms minimum
    pmt_gate_number: int = 0    # H11890 RN (0 = continuous)

    # --- laser watchdog ---
    # If a pixel's count falls below this, assume the laser stopped: pause the
    # scan (hold position) and resume automatically when the signal recovers.
    # 0 disables the watchdog. NOTE: counts are 15-gate sums, so set this well
    # below your typical per-pixel signal but above the dark count (~1-15).
    laser_min_count: int = 0

    # --- scan shape ---
    # Unidirectional by default: every row scans left->right so any fixed
    # galvo/PMT time-lag becomes a uniform sub-pixel shift of the whole image
    # instead of an alternating 1-pixel row-to-row "comb" artifact. Serpentine
    # (snake) only saves galvo flyback, which is negligible here since the scan
    # is PMT-time-limited (~15 ms/pixel), not flyback-limited.
    serpentine: bool = False    # snake scan (reverse every other row) to cut flyback

    # --- hardware addressing ---
    channel_x: str = "Dev1/ao0"
    channel_y: str = "Dev1/ao1"
    dll_path: str = "H11890api.dll"
    # SAFETY: high voltage starts OFF. The device opens (and the live monitor
    # runs) with HV off; the user must tick "PMT high voltage ON" to energize
    # the PMT -- prevents accidentally burning it with room light at startup.
    pmt_hv_on: bool = False

    # --- 32-bit PMT helper (loads H11890api.dll out-of-process) ---
    # Command used to launch pmt_helper.py with a 32-BIT Python interpreter.
    # "py -3.13-32" uses the Windows launcher; or give a full python.exe path,
    # e.g. r"C:\...\Python313-32\python.exe". No spaces in the path, please
    # (or it can't be split into launcher arguments).
    helper_python: str = "py -3.13-32"

    # --- output ---
    output_csv: str = "pmt_output.csv"
    simulate: bool = not HAVE_NIDAQMX


def clamp(v, limit):
    """Restrict v to [-limit, +limit]."""
    if v > limit:
        return limit
    if v < -limit:
        return -limit
    return v


def pixel_to_degree(cfg: ScanConfig, ix: int, iy: int):
    """
    Map a pixel index (ix, iy) -> optical scan angle in DEGREES.

    Centers the grid on (x_offset, y_offset). This is thisWorks.cpp's
    pointsToDegree(). Degrees are what the user thinks in (and what the plots
    show); voltage is just the hardware command derived from this.
    """
    cx = (cfg.pixels_x - 1) / 2.0
    cy = (cfg.pixels_y - 1) / 2.0

    deg_x = (ix - cx) * cfg.step_size_deg + cfg.x_offset_deg
    deg_y = (iy - cy) * cfg.step_size_deg + cfg.y_offset_deg
    return deg_x, deg_y


def pixel_to_voltage(cfg: ScanConfig, ix: int, iy: int):
    """
    Map a pixel index (ix, iy) -> degrees -> clamped voltage.

    The raster equivalent of thisWorks.cpp's pointsToDegree() +
    calculate_voltage_for_degree().
    """
    deg_x, deg_y = pixel_to_degree(cfg, ix, iy)
    vx = clamp(cfg.voltage_limit * deg_x / cfg.degree_range, cfg.voltage_limit)
    vy = clamp(cfg.voltage_limit * deg_y / cfg.degree_range, cfg.voltage_limit)
    return vx, vy


def degree_to_voltage(cfg: ScanConfig, deg):
    """Convert a single scan angle (deg) to a clamped command voltage."""
    return clamp(cfg.voltage_limit * deg / cfg.degree_range, cfg.voltage_limit)


def generate_raster(cfg: ScanConfig):
    """Yield (step, ix, iy, vx, vy) for a centered raster scan."""
    step = 0
    for iy in range(cfg.pixels_y):
        xs = range(cfg.pixels_x)
        if cfg.serpentine and (iy % 2 == 1):
            xs = reversed(range(cfg.pixels_x))
        for ix in xs:
            vx, vy = pixel_to_voltage(cfg, ix, iy)
            yield step, ix, iy, vx, vy
            step += 1


def degree_extent(cfg: ScanConfig):
    """[xmin, xmax, ymin, ymax] in DEGREES, for imshow extent (axes in deg)."""
    x0, y0 = pixel_to_degree(cfg, 0, 0)
    x1, y1 = pixel_to_degree(cfg, cfg.pixels_x - 1, cfg.pixels_y - 1)
    return [x0, x1, y0, y1]


# ===========================================================================
# Galvo backends (NI-DAQ analog output)
# ===========================================================================
class SimGalvo:
    """Fake galvo, just remembers the last commanded voltage."""
    def __init__(self, cfg: ScanConfig):
        self.last = (0.0, 0.0)

    def write(self, vx, vy):
        self.last = (vx, vy)

    def recenter(self):
        """Park the beam at 0,0 between scans (device stays open)."""
        self.last = (0.0, 0.0)

    def close(self):
        self.last = (0.0, 0.0)


class RealGalvo:
    """
    NI-DAQ analog output, two channels (X = ao0, Y = ao1).

    Mirrors thisWorks.cpp: on-demand (software-timed) single-sample updates,
    channels created with +/- voltage_limit range.
    """
    def __init__(self, cfg: ScanConfig):
        self.task = None
        self._device = cfg.channel_x.split("/")[0]  # "Dev1/ao0" -> "Dev1"
        try:
            self._open(cfg)
        except nidaqmx.DaqError as e:
            # -50103: resource reserved. Usually a leftover task from a crashed
            # run (or the old VS program / NI MAX) still holding the AO channels.
            # Reset the device to free it, then try once more.
            if e.error_code == -50103:
                nidaqmx.system.Device(self._device).reset_device()
                self._open(cfg)
            else:
                raise

    def _open(self, cfg: ScanConfig):
        task = nidaqmx.Task()
        try:
            task.ao_channels.add_ao_voltage_chan(
                cfg.channel_x, min_val=-cfg.voltage_limit, max_val=cfg.voltage_limit)
            task.ao_channels.add_ao_voltage_chan(
                cfg.channel_y, min_val=-cfg.voltage_limit, max_val=cfg.voltage_limit)
            # No sample clock configured => on-demand. start() once, then write().
            task.start()
        except Exception:
            task.close()   # don't leave a half-built task holding the device
            raise
        self.task = task

    def write(self, vx, vy):
        # one sample per channel
        self.task.write([float(vx), float(vy)], auto_start=False)

    def recenter(self):
        """Park the beam at 0,0 between scans. Task stays open/started."""
        if self.task is not None:
            self.write(0.0, 0.0)

    def close(self):
        if self.task is None:
            return
        try:
            self.write(0.0, 0.0)   # recenter the beam
            self.task.stop()
        finally:
            self.task.close()
            self.task = None


# ===========================================================================
# PMT backends (Hamamatsu H11890)
# ===========================================================================
class SimPMT:
    """
    Fake PMT: a Gaussian blob centered in the field of view + Poisson noise, so
    the live heatmap shows a recognizable pattern when developing off the rig.
    """
    def __init__(self, cfg: ScanConfig):
        self.gate = 0
        self.rng = np.random.default_rng()

    def start(self):
        pass

    def stop(self):
        """Stop counting between scans (device stays open)."""
        pass

    def reconfigure(self, cfg: ScanConfig):
        """Apply changed gate/HV settings without reopening (no-op in sim)."""
        pass

    def kill(self):
        """Hard-stop counterpart of SubprocessPMT.kill (no-op in sim)."""
        pass

    def reopen(self):
        """Relaunch after a device drop (no-op in sim)."""
        pass

    def set_hv(self, on):
        """Emergency HV control (no-op in sim)."""
        pass

    def read(self, vx, vy):
        r2 = vx * vx + vy * vy
        base = 5000.0 * np.exp(-r2 / (2.0 * 0.8 ** 2))
        # mimic the real helper: each read is the SUM of a 15 x 1 ms gate batch
        count = int(self.rng.poisson((base + 50) * 15))
        over = 1 if count > 60000 * 15 else 0
        self.gate += 15
        return count, over, self.gate

    def close(self):
        pass


# ---------------------------------------------------------------------------
# SubprocessPMT -- talk to the 32-bit pmt_helper.py over a pipe.
#
# H11890api.dll is 32-bit only and this GUI runs in 64-bit Python, so we can't
# load the DLL in-process. Instead we launch pmt_helper.py with a 32-bit Python
# (cfg.helper_python) and exchange one JSON object per line on its stdin/stdout.
# The helper holds all the ctypes / H11890 logic (see pmt_helper.py).
# ---------------------------------------------------------------------------
# Windows: don't pop a console window for the helper child process.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class SubprocessPMT:
    """Drives the Hamamatsu H11890 via the out-of-process 32-bit helper."""

    def __init__(self, cfg: ScanConfig):
        self.cfg = cfg
        self.proc = None
        # One lock serializes all RPCs on the helper pipe: the scan thread
        # reads constantly, and the GUI may interleave an emergency HV-off or
        # reconfigure on the same pipe -- two unserialized commands would
        # desync request/reply pairing.
        self._lock = threading.Lock()
        self._launch(cfg)

    def _launch(self, cfg: ScanConfig):
        """(Re)start the 32-bit helper process and open+configure the device."""
        # When frozen by PyInstaller, __file__ points inside the bundle; the
        # helper + DLL are shipped next to the .exe instead.
        if getattr(sys, "frozen", False):
            here = os.path.dirname(sys.executable)
        else:
            here = os.path.dirname(os.path.abspath(__file__))

        helper_exe = os.path.join(here, "pmt_helper.exe")
        helper_py = os.path.join(here, "pmt_helper.py")

        if os.path.isfile(helper_exe):
            # Standalone 32-bit helper exe (built by build_exe.bat): users
            # don't need any Python installed.
            cmd = [helper_exe]
        elif os.path.isfile(helper_py):
            # Script mode: run pmt_helper.py with a 32-bit Python.
            # "py -3.13-32"  ->  ["py", "-3.13-32"];  full exe path also works.
            try:
                launcher = shlex.split(cfg.helper_python, posix=False)
            except Exception:
                launcher = cfg.helper_python.split()
            if not launcher:
                raise ValueError("Helper Python command is empty")
            # shlex(posix=False) keeps surrounding quotes -> strip them.
            launcher = [tok.strip('"') for tok in launcher]

            # Resolve to the REAL python.exe. The 'py' launcher (py -3.13-32) is a
            # separate process that spawns python.exe as a CHILD -- if we Popen the
            # launcher, self.proc is py.exe and killing it ORPHANS the real helper,
            # which keeps holding the USB device (a zombie that blocks re-opening).
            # Launching the interpreter directly makes self.proc the helper itself.
            interp = self._resolve_interpreter(launcher)
            cmd = interp + ["-u", helper_py]
        else:
            raise FileNotFoundError(
                f"Neither pmt_helper.exe nor pmt_helper.py found next to the GUI: {here}")

        try:
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=here, text=True, bufsize=1, creationflags=_NO_WINDOW,
            )
        except (FileNotFoundError, OSError) as e:
            raise RuntimeError(
                f"Could not launch the 32-bit PMT helper ({cmd[0]!r}). "
                "If running from source, check the 'Helper Python' field -- it "
                "must point at a 32-bit Python (e.g. 'py -3.13-32' or a full "
                "python.exe path).") from e

        # Sanity check: the helper must really be 32-bit, or the DLL won't load.
        r = self._rpc({"cmd": "ping"})
        if r.get("bits") != 32:
            self.close()
            raise RuntimeError(
                f"Helper Python is {r.get('bits')}-bit, but H11890api.dll needs "
                "32-bit. Point 'Helper Python' at a 32-bit interpreter.")

        # Open + configure the device.
        r = self._rpc({"cmd": "open", "dll_path": cfg.dll_path,
                       "it": cfg.pmt_gate_time_ms, "rn": cfg.pmt_gate_number,
                       "hvon": 1 if cfg.pmt_hv_on else 0})
        if not r.get("ok"):
            self.close()
            raise RuntimeError("PMT open failed: " + r.get("error", "unknown"))

    @staticmethod
    def _resolve_interpreter(launcher):
        """Turn a launcher command (e.g. ['py','-3.13-32']) into the actual
        python.exe path, so our child process IS the interpreter rather than the
        py.exe launcher. Falls back to the launcher as-given if resolution fails.
        """
        try:
            out = subprocess.run(
                launcher + ["-c", "import sys;print(sys.executable)"],
                capture_output=True, text=True, timeout=15,
                creationflags=_NO_WINDOW,
            )
            lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
            if lines and os.path.isfile(lines[-1]):
                return [lines[-1]]
        except Exception:
            pass
        return launcher

    def _rpc(self, obj):
        """Send one request line, read one reply line. Raise on a dead helper.

        We snapshot self.proc into a local first: another thread (the Stop
        watchdog / recovery path) may call kill() and set self.proc = None at
        any moment, and we must not dereference None ('NoneType has no attribute
        poll'). Using the local also keeps the pipe handles valid for the calls
        below even if self.proc is cleared mid-way.
        """
        with self._lock:
            p = self.proc
            if p is None or p.poll() is not None:
                raise RuntimeError("PMT helper is not running.\n" + self._drain_stderr(p))
            try:
                p.stdin.write(json.dumps(obj) + "\n")
                p.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                raise RuntimeError("PMT helper pipe closed.\n" + self._drain_stderr(p))

            line = p.stdout.readline()
            if not line:   # EOF -> the helper died; its traceback is on stderr
                raise RuntimeError("PMT helper sent no reply.\n" + self._drain_stderr(p))
            try:
                return json.loads(line)
            except Exception:
                return {"ok": False, "error": f"bad reply from helper: {line!r}"}

    def _drain_stderr(self, p=None):
        """Read whatever the (now-exited) helper wrote to stderr, for the error."""
        p = p or self.proc
        if p is None:
            return ""
        try:
            return (p.stderr.read() or "").strip()
        except Exception:
            return ""

    def start(self):
        r = self._rpc({"cmd": "start"})
        if not r.get("ok"):
            raise RuntimeError("PMT start failed: " + r.get("error", "unknown"))

    def stop(self):
        """Stop counting between scans; the device stays open for reuse."""
        try:
            self._rpc({"cmd": "stop"})   # best-effort
        except Exception:
            pass

    def reconfigure(self, cfg: ScanConfig):
        """Re-apply gate time / gate number / HV without reopening the device."""
        self.cfg = cfg
        r = self._rpc({"cmd": "reconfig", "it": cfg.pmt_gate_time_ms,
                       "rn": cfg.pmt_gate_number,
                       "hvon": 1 if cfg.pmt_hv_on else 0})
        if not r.get("ok"):
            raise RuntimeError("PMT reconfigure failed: " + r.get("error", "unknown"))

    def reopen(self):
        """Hard-restart the helper + device after a USB drop / driver wedge.

        kill() also makes Windows release the USB handle, so the fresh helper
        can re-open the device.
        """
        self.kill()
        self._launch(self.cfg)

    def set_hv(self, on: bool):
        """Set the PMT high voltage NOW. Safe mid-scan: the RPC lock serializes
        this with the scan thread's reads on the shared pipe."""
        self.cfg.pmt_hv_on = bool(on)
        r = self._rpc({"cmd": "reconfig", "it": self.cfg.pmt_gate_time_ms,
                       "rn": self.cfg.pmt_gate_number, "hvon": 1 if on else 0})
        if not r.get("ok"):
            raise RuntimeError("PMT HV change failed: " + r.get("error", "unknown"))

    def read(self, vx, vy):
        r = self._rpc({"cmd": "read"})
        if not r.get("ok"):
            return -1, 0, 0
        return int(r["count"]), int(r["over"]), int(r["gate"])

    def close(self):
        p = self.proc          # snapshot: kill() on another thread may null it
        self.proc = None
        if p is None:
            return
        if p.poll() is None:
            # Ask the helper to close the device cleanly, then quit. We do NOT
            # block on a reply here (a wedged CloseDevices could hang the read
            # forever) -- we just wait for the process to exit, and kill it if
            # it doesn't. Killing also makes Windows release the USB handle, so
            # the device can be opened again next time.
            try:
                p.stdin.write(json.dumps({"cmd": "close"}) + "\n")
                p.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
                p.stdin.flush()
            except Exception:
                pass
            try:
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                    p.wait(timeout=2)
                except Exception:
                    pass

    def kill(self):
        """Hard-kill the helper immediately (no graceful handshake).

        Used to recover when the helper is wedged inside a blocking H11890 DLL
        call: terminating the process unblocks our pipe read AND makes Windows
        release the USB handle, so the device can be re-opened next time.
        """
        p = getattr(self, "proc", None)
        self.proc = None
        if p is None:
            return
        try:
            p.kill()
        except Exception:
            pass
        try:
            p.wait(timeout=2)
        except Exception:
            pass


# ===========================================================================
# Scan engine (runs in a background thread)
# ===========================================================================
class ScanEngine(threading.Thread):
    """
    Drives one full raster: write galvo -> settle -> read PMT -> push result.
    Results go to out_queue as ("pixel", step, ix, iy, vx, vy, count, over, gate).
    On finish/abort -> ("done", reason). On failure -> ("error", message).
    Also streams rows to the output CSV (same columns as pmt_output.csv).
    """
    def __init__(self, cfg: ScanConfig, galvo, pmt, out_queue, stop_event):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.galvo = galvo
        self.pmt = pmt
        self.q = out_queue
        self.stop_event = stop_event

    def _read_recover(self, vx, vy):
        """pmt.read with automatic reconnect after a USB/driver drop.

        The H11890 driver occasionally wedges or the device drops off USB.
        Instead of killing the whole scan, restart the helper + device (up to
        3 tries) and re-read this pixel; the galvo hasn't moved, so nothing
        is lost.
        """
        try:
            count, over, gate = self.pmt.read(vx, vy)
            if count >= 0:
                return count, over, gate
            err = "device returned a read error"
        except Exception as e:
            err = str(e)
        for attempt in (1, 2, 3):
            if self.stop_event.is_set():
                break
            self.q.put(("status",
                        f"PMT problem ({err}) - reconnecting {attempt}/3..."))
            try:
                self.pmt.reopen()
                self.pmt.start()
                count, over, gate = self.pmt.read(vx, vy)
                if count >= 0:
                    self.q.put(("status", "PMT reconnected - scan continues."))
                    return count, over, gate
                err = "device returned a read error"
            except Exception as e:
                err = str(e)
            time.sleep(2.0)
        raise RuntimeError("PMT unrecoverable after 3 reconnect attempts: " + err)

    def _read_pixel(self, vx, vy):
        """Read one pixel. If the count is below the laser-off threshold, hold
        this galvo position and keep polling until the signal comes back, then
        resume the scan exactly where it left off."""
        count, over, gate = self._read_recover(vx, vy)
        thr = self.cfg.laser_min_count
        if thr > 0 and count < thr:
            self.q.put(("status",
                        f"Signal {count} < {thr}: laser off? Scan PAUSED, waiting..."))
            while not self.stop_event.is_set():
                time.sleep(0.5)
                count, over, gate = self._read_recover(vx, vy)
                if count >= thr:
                    self.q.put(("status", "Signal recovered - scan resumed."))
                    break
        return count, over, gate

    def run(self):
        writer = None
        f = None
        try:
            if self.cfg.output_csv:
                # Stream to a .part file; when the scan ends the GUI asks
                # "save or skip?" and renames or deletes it.
                f = open(self.cfg.output_csv + ".part", "w", newline="")
                writer = csv.writer(f)
                writer.writerow(["step", "x_deg", "y_deg", "x_voltage",
                                 "y_voltage", "gate_number", "pmt_count",
                                 "over_light"])

            self.pmt.start()

            for step, ix, iy, vx, vy in generate_raster(self.cfg):
                if self.stop_event.is_set():
                    self.q.put(("done", "stopped"))
                    return

                self.galvo.write(vx, vy)
                time.sleep(self.cfg.settle_ms / 1000.0)

                count, over, gate = self._read_pixel(vx, vy)
                dx, dy = pixel_to_degree(self.cfg, ix, iy)

                if writer is not None:
                    writer.writerow([step, dx, dy, vx, vy, gate, count, over])

                self.q.put(("pixel", step, ix, iy, vx, vy, count, over, gate))

            self.q.put(("done", "finished"))

        except Exception as e:
            self.q.put(("error", str(e)))
        finally:
            # End of scan: stop counting and park the beam, but KEEP the devices
            # open so the next Start reuses them. (Re-opening the H11890 USB
            # device every scan is what made a second scan fail.) Full teardown
            # happens on app exit / hardware-setting change, in the GUI.
            try:
                self.pmt.stop()
            except Exception:
                pass
            try:
                self.galvo.recenter()
            except Exception:
                pass
            if f is not None:
                f.close()


# ===========================================================================
# GUI
# ===========================================================================
class ScannerGUI:
    def __init__(self, root):
        self.root = root
        root.title("2PM Raster Scanner  -  galvo + H11890 PMT")

        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.engine = None
        self._stop_deadline = 0.0

        # --- always-on live PMT monitor state ---
        # Plain attributes (not tk vars) because the monitor thread reads them.
        self._scan_active = False   # True while a scan owns the PMT
        self._mon_on = True         # mirrors the "Live PMT monitor" checkbox
        self._mon_idle = threading.Event()  # set = monitor parked, scan may start
        self._opening = False       # an async auto-connect is in flight
        self._closing = False

        # Persistent hardware, kept open ACROSS scans (see on_start). Re-opened
        # only when a device-level setting changes; closed on app exit.
        self.galvo = None
        self.pmt = None
        self._hw_sig = None   # identity of the currently-open hardware

        # data buffers for plotting (the pmtPlot.py side)
        self.cfg = None
        self.image = None
        self.records = []   # dicts: step, x, y, count, over

        self._build_widgets()
        self._poll_queue()
        # Live PMT readout: runs for the whole app lifetime, no button needed.
        threading.Thread(target=self._monitor_loop, daemon=True).start()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ----- layout -----
    def _build_widgets(self):
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)

        # ---- left: parameters ----
        params = ttk.LabelFrame(main, text="Scan parameters", padding=8)
        params.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        self.vars = {}

        def field(row, label, key, default, width=10):
            ttk.Label(params, text=label).grid(row=row, column=0, sticky="w", pady=2)
            var = tk.StringVar(value=str(default))
            ttk.Entry(params, textvariable=var, width=width).grid(
                row=row, column=1, sticky="w", pady=2)
            self.vars[key] = var
            return var

        d = ScanConfig()
        r = 0
        field(r, "FOV X (deg)", "fov_x_deg", d.fov_x_deg); r += 1
        field(r, "FOV Y (deg)", "fov_y_deg", d.fov_y_deg); r += 1
        field(r, "Step size (deg/px)", "step_size_deg", d.step_size_deg); r += 1
        field(r, "X offset (deg)", "x_offset_deg", d.x_offset_deg); r += 1
        field(r, "Y offset (deg)", "y_offset_deg", d.y_offset_deg); r += 1
        field(r, "Voltage limit (+/- V)", "voltage_limit", d.voltage_limit); r += 1
        field(r, "Degree range (full scale)", "degree_range", d.degree_range); r += 1
        field(r, "Settle (ms)", "settle_ms", d.settle_ms); r += 1
        field(r, "PMT gate time (ms)", "pmt_gate_time_ms", d.pmt_gate_time_ms); r += 1
        field(r, "PMT gate number", "pmt_gate_number", d.pmt_gate_number); r += 1
        field(r, "Laser-off threshold (0=off)", "laser_min_count",
              d.laser_min_count); r += 1
        field(r, "Scale min (manual)", "scale_min", 0); r += 1
        field(r, "Scale max (manual)", "scale_max", 1000); r += 1
        field(r, "X channel", "channel_x", d.channel_x, width=14); r += 1
        field(r, "Y channel", "channel_y", d.channel_y, width=14); r += 1
        field(r, "PMT DLL path", "dll_path", d.dll_path, width=14); r += 1
        field(r, "Helper Python (32-bit)", "helper_python", d.helper_python, width=14); r += 1
        field(r, "Output CSV", "output_csv", d.output_csv, width=14); r += 1

        self.serp_var = tk.BooleanVar(value=d.serpentine)
        ttk.Checkbutton(params, text="Serpentine (snake) scan",
                        variable=self.serp_var).grid(row=r, column=0, columnspan=2,
                                                     sticky="w", pady=2); r += 1

        self.hv_var = tk.BooleanVar(value=d.pmt_hv_on)
        ttk.Checkbutton(params, text="PMT high voltage ON",
                        variable=self.hv_var,
                        command=self.on_hv_toggle).grid(
                            row=r, column=0, columnspan=2,
                            sticky="w", pady=2); r += 1

        self.sim_var = tk.BooleanVar(value=d.simulate)
        ttk.Checkbutton(params, text="Simulation mode (no hardware)",
                        variable=self.sim_var).grid(row=r, column=0, columnspan=2,
                                                    sticky="w", pady=2); r += 1

        self.autoscale_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(params, text="Auto colour scale",
                        variable=self.autoscale_var,
                        command=self._refresh_image).grid(
                            row=r, column=0, columnspan=2, sticky="w", pady=2); r += 1

        # ---- buttons ----
        btns = ttk.Frame(params)
        btns.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(8, 0)); r += 1
        self.start_btn = ttk.Button(btns, text="Start", command=self.on_start)
        self.start_btn.pack(side="left", padx=2)
        self.stop_btn = ttk.Button(btns, text="Stop", command=self.on_stop,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=2)
        ttk.Button(btns, text="Show plots", command=self.show_plots).pack(
            side="left", padx=2)
        ttk.Button(btns, text="Save CSV as...", command=self.save_csv_as).pack(
            side="left", padx=2)

        btns2 = ttk.Frame(params)
        btns2.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(4, 0)); r += 1
        # Emergency HV kill: plain tk.Button so it can be red and unmissable.
        tk.Button(btns2, text="HV OFF", command=self.on_hv_off,
                  bg="#b00020", fg="white",
                  activebackground="#8a0018", activeforeground="white").pack(
            side="left", padx=2)
        self.monitor_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(btns2, text="Live PMT monitor",
                        variable=self.monitor_var,
                        command=self._on_monitor_toggle).pack(side="left", padx=6)

        # ---- status ----
        self.status = tk.StringVar(
            value="Ready" + ("" if HAVE_NIDAQMX else "  (nidaqmx not found -> simulation)"))
        ttk.Label(params, textvariable=self.status, wraplength=240,
                  foreground="#0a6").grid(row=r, column=0, columnspan=2,
                                          sticky="w", pady=(8, 0)); r += 1
        self.live_var = tk.StringVar(value="PMT live: -")
        ttk.Label(params, textvariable=self.live_var,
                  font=("TkDefaultFont", 10, "bold")).grid(
                      row=r, column=0, columnspan=2, sticky="w"); r += 1
        self.progress = ttk.Progressbar(params, length=240, mode="determinate")
        self.progress.grid(row=r, column=0, columnspan=2, sticky="ew", pady=4)

        # ---- right: live heatmap ----
        plot_frame = ttk.LabelFrame(main, text="Live PMT heatmap", padding=4)
        plot_frame.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        self.fig = Figure(figsize=(6, 5))
        self.ax = self.fig.add_subplot(111)
        self.im = None
        self.cbar = None
        self.ax.set_xlabel("X angle (deg)")
        self.ax.set_ylabel("Y angle (deg)")
        self.ax.set_title("PMT intensity")

        self.canvas = FigureCanvasTkAgg(self.fig, master=plot_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(self.canvas, plot_frame).update()

    # ----- read params from the GUI -----
    def _read_config(self):
        v = self.vars
        cfg = ScanConfig(
            fov_x_deg=float(v["fov_x_deg"].get()),
            fov_y_deg=float(v["fov_y_deg"].get()),
            step_size_deg=float(v["step_size_deg"].get()),
            x_offset_deg=float(v["x_offset_deg"].get()),
            y_offset_deg=float(v["y_offset_deg"].get()),
            voltage_limit=float(v["voltage_limit"].get()),
            degree_range=float(v["degree_range"].get()),
            settle_ms=int(v["settle_ms"].get()),
            pmt_gate_time_ms=int(v["pmt_gate_time_ms"].get()),
            pmt_gate_number=int(v["pmt_gate_number"].get()),
            laser_min_count=int(v["laser_min_count"].get()),
            serpentine=self.serp_var.get(),
            channel_x=v["channel_x"].get().strip(),
            channel_y=v["channel_y"].get().strip(),
            dll_path=v["dll_path"].get().strip(),
            helper_python=v["helper_python"].get().strip(),
            pmt_hv_on=self.hv_var.get(),
            output_csv=v["output_csv"].get().strip(),
            simulate=self.sim_var.get(),
        )

        # --- basic sanity ---
        if cfg.step_size_deg <= 0:
            raise ValueError("Step size must be > 0")
        if cfg.fov_x_deg < 0 or cfg.fov_y_deg < 0:
            raise ValueError("FOV must be >= 0")
        if cfg.degree_range <= 0:
            raise ValueError("Degree range must be > 0")
        if cfg.pixels_x < 1 or cfg.pixels_y < 1:
            raise ValueError("FOV / step size produce fewer than 1 pixel")

        # --- GALVO SAFETY ---
        # The beam must never be commanded past +/- voltage_limit or it burns the
        # galvo. voltage_limit is itself capped at 5 V. The furthest the beam
        # reaches on each axis is half the (rounded) FOV plus the offset; convert
        # that worst-case angle to a voltage and refuse the scan if it exceeds the
        # limit -- rather than silently clamping and flattening the image edges.
        if cfg.voltage_limit > 5.0:
            raise ValueError("Voltage limit is capped at 5 V for galvo safety")

        half_x = (cfg.pixels_x - 1) / 2.0 * cfg.step_size_deg
        half_y = (cfg.pixels_y - 1) / 2.0 * cfg.step_size_deg
        max_deg_x = half_x + abs(cfg.x_offset_deg)
        max_deg_y = half_y + abs(cfg.y_offset_deg)
        max_deg = max(max_deg_x, max_deg_y)
        max_volt = cfg.voltage_limit * max_deg / cfg.degree_range

        if max_deg > cfg.degree_range + 1e-9:
            raise ValueError(
                f"Scan reaches +/-{max_deg:.2f} deg ({max_volt:.2f} V), which "
                f"exceeds the galvo limit of +/-{cfg.degree_range:.1f} deg "
                f"(+/-{cfg.voltage_limit:.1f} V). Reduce FOV or offset to protect "
                "the galvo.")
        return cfg

    # ----- hardware session (kept open across scans) -----
    @staticmethod
    def _hw_identity(cfg: ScanConfig):
        """Settings that require physically re-opening the devices when changed.
        (Gate time / gate number / HV are applied via reconfigure instead.)"""
        return (cfg.simulate, cfg.channel_x, cfg.channel_y,
                round(cfg.voltage_limit, 6), cfg.dll_path, cfg.helper_python)

    def _open_hardware(self, cfg: ScanConfig):
        if cfg.simulate:
            self.galvo, self.pmt = SimGalvo(cfg), SimPMT(cfg)
        else:
            if not HAVE_NIDAQMX:
                raise RuntimeError("nidaqmx not installed; enable Simulation mode")
            galvo = RealGalvo(cfg)
            try:
                pmt = SubprocessPMT(cfg)   # 32-bit helper loads H11890api.dll
            except Exception:
                galvo.close()   # release the AO task so Dev1 isn't left reserved
                raise
            self.galvo, self.pmt = galvo, pmt
        self._hw_sig = self._hw_identity(cfg)

    def _close_hardware(self, force=False):
        # force=True hard-kills the PMT helper (used to recover from a wedged
        # DLL call); otherwise the device is closed gracefully.
        if self.pmt is not None:
            try:
                self.pmt.kill() if force else self.pmt.close()
            except Exception:
                pass
        if self.galvo is not None:
            try:
                self.galvo.close()
            except Exception:
                pass
        self.pmt = None
        self.galvo = None
        self._hw_sig = None

    # ----- start / stop -----
    def on_start(self):
        if self._opening:
            self.status.set("PMT is auto-connecting - try again in a moment.")
            return
        # Make sure the PREVIOUS scan thread is completely finished before we
        # touch the PMT pipe again -- its cleanup runs a PMT command on the same
        # pipe, and overlapping two commands would desync it. If that thread is
        # wedged in a blocking H11890 DLL call, recover by dropping the hardware
        # (this kills the 32-bit helper, which unblocks the stuck thread); the
        # next open below starts fresh.
        if self.engine is not None and self.engine.is_alive():
            self.engine.join(timeout=3)
            if self.engine.is_alive():
                self._close_hardware(force=True)
                self.engine.join(timeout=3)
        self.engine = None

        try:
            cfg = self._read_config()
        except Exception as e:
            messagebox.showerror("Bad parameters", str(e))
            return

        # Park the live monitor: the scan owns the PMT from here (otherwise the
        # monitor would steal 15-gate batches meant for scan pixels).
        self._scan_active = True
        self._mon_idle.wait(2.0)

        # Open hardware once and reuse it. Only re-open when a device-level
        # setting changed; otherwise just re-apply the PMT gate/HV settings.
        try:
            if self.galvo is None or self.pmt is None \
                    or self._hw_identity(cfg) != self._hw_sig:
                self._close_hardware()
                self._open_hardware(cfg)
            else:
                self.pmt.reconfigure(cfg)
        except Exception as e:
            self._scan_active = False   # un-park the monitor
            self._close_hardware()
            messagebox.showerror("Hardware init failed", str(e))
            return

        # reset data + display
        self.cfg = cfg
        self.records = []
        self.image = np.full((cfg.pixels_y, cfg.pixels_x), np.nan)
        self._init_image()
        self.progress.configure(maximum=cfg.pixels_x * cfg.pixels_y, value=0)

        # H11890 delivers 15 gates per read when IT < 10 ms (else 1), so each
        # pixel really costs settle + 15*IT.
        gates_per_read = 15 if cfg.pmt_gate_time_ms < 10 else 1
        est = cfg.pixels_x * cfg.pixels_y \
            * (cfg.settle_ms + gates_per_read * cfg.pmt_gate_time_ms) / 1000.0
        mode = "SIM" if cfg.simulate else "HARDWARE"
        self.status.set(
            f"Scanning [{mode}]  {cfg.pixels_x}x{cfg.pixels_y} px "
            f"({cfg.fov_x_deg:g}x{cfg.fov_y_deg:g} deg)  ~{est:.1f}s estimated...")

        self.stop_event.clear()
        self.engine = ScanEngine(cfg, self.galvo, self.pmt, self.q, self.stop_event)
        self.engine.start()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")

    def on_stop(self):
        self.stop_event.set()
        self.status.set("Stopping...")
        self.stop_btn.configure(state="disabled")
        # If the scan stops gracefully, the "done" message re-enables Start and
        # the device stays open. But a read/CountStop may be blocked inside the
        # H11890 DLL; then the thread can't see the stop flag. Watch for that and
        # force-recover so the UI never gets stuck with Start disabled.
        gate_s = self.cfg.pmt_gate_time_ms / 1000.0 if self.cfg else 0.0
        self._stop_deadline = time.time() + max(3.0, gate_s * 3 + 2.0)
        self.root.after(300, self._stop_watchdog)

    def _stop_watchdog(self):
        if self.engine is None or not self.engine.is_alive():
            self.start_btn.configure(state="normal")   # stopped cleanly
            return
        if time.time() >= self._stop_deadline:
            # Wedged in a blocking H11890 call. Killing the helper unblocks the
            # thread; the device is dropped, so the next Start re-opens it fresh.
            self.status.set("PMT was busy - force-stopped. Press Start to rescan.")
            self._close_hardware(force=True)
            self.engine.join(timeout=3)
            self.engine = None
            self._scan_active = False   # let the monitor auto-reconnect
            self.start_btn.configure(state="normal")
            return
        self.root.after(300, self._stop_watchdog)

    def on_hv_off(self):
        """EMERGENCY: force the PMT high voltage OFF immediately.

        Works mid-scan too -- SubprocessPMT's RPC lock serializes this with the
        scan thread's reads. Runs in a worker thread so a wedged helper can
        never freeze the GUI (the wedge-recovery watchdogs handle that case).
        """
        self.hv_var.set(False)   # next scan/reconfigure also keeps HV off
        pmt = self.pmt
        if pmt is None:
            self.status.set("HV OFF: no device open - will apply on next open.")
            return
        def worker():
            try:
                pmt.set_hv(False)
                self.q.put(("status", "PMT HIGH VOLTAGE IS OFF."))
            except Exception as e:
                self.q.put(("status", f"HV OFF failed: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    def on_hv_toggle(self):
        """Apply the HV checkbox IMMEDIATELY (not only at the next scan).

        HV starts OFF for safety; ticking the box energizes the PMT right away
        so the live monitor starts showing real counts. Runs in a worker thread
        so a wedged helper can't freeze the GUI.
        """
        want = self.hv_var.get()
        pmt = self.pmt
        if pmt is None:
            self.status.set(f"HV {'ON' if want else 'OFF'}: will apply when "
                            "the device opens.")
            return
        def worker():
            try:
                pmt.set_hv(want)
                self.q.put(("status", f"PMT high voltage {'ON' if want else 'OFF'}."))
            except Exception as e:
                self.q.put(("status", f"HV change failed: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    # ----- always-on live PMT monitor -----
    def _on_monitor_toggle(self):
        self._mon_on = self.monitor_var.get()
        if not self._mon_on:
            self.live_var.set("PMT live: (monitor off)")

    def _monitor_loop(self):
        """Background thread: keep reading the PMT and showing it live.

        Runs whenever no scan is active, so the readout is always on screen
        without pressing anything. If no device is open yet it asks the GUI
        thread to auto-connect (and silently retries). It PARKS during scans
        (signalled via _mon_idle) so it can never steal 15-gate batches that
        belong to scan pixels -- during a scan the per-pixel messages update
        the same label instead. Never touches tk widgets directly; everything
        goes through the queue.
        """
        need_start = True
        last_open_req = 0.0
        while not self._closing:
            if not self._mon_on or self._scan_active:
                self._mon_idle.set()
                need_start = True   # scan's cleanup calls pmt.stop()
                time.sleep(0.3)
                continue
            pmt = self.pmt
            if pmt is None:
                self._mon_idle.set()
                need_start = True
                now = time.time()
                if not self._opening and now - last_open_req > 5.0:
                    last_open_req = now
                    self.q.put(("need_open",))
                time.sleep(0.5)
                continue
            self._mon_idle.clear()
            try:
                if need_start:
                    pmt.start()
                    need_start = False
                count, over, gate = pmt.read(0.0, 0.0)
                self.q.put(("live", count, over))
            except Exception as e:
                need_start = True
                self.q.put(("live_err", f"read failed ({e})"))
                time.sleep(1.0)
            time.sleep(0.05)   # ~15 Hz; the real read also blocks ~15 ms
        self._mon_idle.set()

    def _auto_open_async(self):
        """Open the hardware in a worker thread so live monitoring starts by
        itself. Failures are silent (label shows 'not connected', retries)."""
        if self._opening or self._scan_active or self.pmt is not None:
            return
        try:
            cfg = self._read_config()
        except Exception:
            return   # half-edited parameters -> just try again later
        self._opening = True
        self.live_var.set("PMT live: connecting...")
        def worker():
            try:
                self._open_hardware(cfg)
                self.q.put(("status", "Device connected - live PMT monitor on."))
            except Exception:
                self.q.put(("live_err", "not connected (retrying...)"))
            finally:
                self._opening = False
        threading.Thread(target=worker, daemon=True).start()

    def on_close(self):
        """Stop any running scan and fully release hardware before quitting."""
        self._closing = True          # stop the live monitor thread
        self._scan_active = True      # keep it from touching the PMT meanwhile
        self.stop_event.set()
        if self.engine is not None:
            self.engine.join(timeout=3)
            # if it's wedged in a blocking DLL call, hard-kill so we don't hang
            force = self.engine.is_alive()
        else:
            force = False
        self._close_hardware(force=force)
        self.root.destroy()

    # ----- live image -----
    def _init_image(self):
        self.ax.clear()
        self.ax.set_xlabel("X angle (deg)")
        self.ax.set_ylabel("Y angle (deg)")
        self.ax.set_title("PMT intensity")
        cmap = plt.get_cmap("viridis").copy()
        cmap.set_bad(color="#202020")  # unscanned pixels
        ext = degree_extent(self.cfg)
        # origin="upper" + swapped y-extent: row 0 (first scanned row) draws at
        # the TOP-left like a normal photo, so the PMT image compares 1:1 with
        # a camera view of the sample.
        self.im = self.ax.imshow(
            self.image, origin="upper", aspect="equal", cmap=cmap,
            extent=[ext[0], ext[1], ext[3], ext[2]],
        )
        # Placeholder clim: an all-NaN image must not autoscale to a bogus
        # (e.g. -0.1..0.1) colorbar before the first pixel arrives.
        self.im.set_clim(0.0, 1.0)
        if self.cbar is None:
            self.cbar = self.fig.colorbar(self.im, ax=self.ax, label="PMT count")
        else:
            self.cbar.update_normal(self.im)
        self.canvas.draw_idle()

    def _refresh_image(self):
        if self.im is None:
            return
        self.im.set_data(self.image)
        vmin = vmax = None
        if self.autoscale_var.get():
            finite = self.image[np.isfinite(self.image)]
            if finite.size:
                vmin = max(0.0, float(finite.min()))   # counts are never negative
                vmax = float(finite.max())
        else:
            # Manual scale: one hot pixel can't blow out the whole display.
            try:
                vmin = float(self.vars["scale_min"].get())
                vmax = float(self.vars["scale_max"].get())
            except (ValueError, KeyError):
                pass   # bad manual entry -> keep the last scale
        if vmin is not None and vmax is not None:
            if vmax <= vmin:
                # A degenerate clim (all pixels equal, e.g. all 0) makes
                # matplotlib expand it to +/-0.1 -> the "negative PMT count"
                # scale bug. Force a real range instead.
                vmax = vmin + 1.0
            self.im.set_clim(vmin=vmin, vmax=vmax)
            if self.cbar is not None:
                self.cbar.update_normal(self.im)   # keep colorbar in sync
        self.canvas.draw_idle()

    # ----- queue pump (GUI thread) -----
    def _poll_queue(self):
        dirty = False
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "pixel":
                    _, step, ix, iy, vx, vy, count, over, gate = msg
                    if count >= 0:
                        self.image[iy, ix] = count
                    dx, dy = pixel_to_degree(self.cfg, ix, iy)
                    self.records.append(dict(step=step, x=vx, y=vy,
                                             dx=dx, dy=dy,
                                             count=count, over=over))
                    self.progress.configure(value=step + 1)
                    self.live_var.set(f"PMT live: {count}"
                                      + ("  OVER LIGHT!" if over else ""))
                    dirty = True
                elif kind == "status":
                    self.status.set(msg[1])
                elif kind == "live":
                    # only from the monitor thread; drop stale ones queued
                    # just before the monitor was toggled off
                    if self._mon_on:
                        _, count, over = msg
                        self.live_var.set(f"PMT live: {count}"
                                          + ("  OVER LIGHT!" if over else ""))
                elif kind == "live_err":
                    if self._mon_on:
                        self.live_var.set(f"PMT live: {msg[1]}")
                elif kind == "need_open":
                    self._auto_open_async()
                elif kind == "done":
                    self._refresh_image()
                    self.status.set(f"Done ({msg[1]}). {len(self.records)} points.")
                    self.start_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                    self._scan_active = False   # hand the PMT back to the monitor
                    self._ask_save(msg[1])
                elif kind == "error":
                    self._refresh_image()
                    self.start_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                    self._scan_active = False   # hand the PMT back to the monitor
                    # The device may be in a bad state (e.g. helper crashed);
                    # drop it so the next Start re-opens cleanly.
                    if self.engine is not None:
                        self.engine.join(timeout=3)
                    self._close_hardware(force=True)
                    self.engine = None
                    if self.stop_event.is_set():
                        # The "error" is just a side effect of the user stopping
                        # (we killed the helper to unblock it) -- not a failure.
                        self.status.set("Stopped.")
                    else:
                        self.status.set("Error.")
                        messagebox.showerror("Scan error", msg[1])
        except queue.Empty:
            pass
        if dirty:
            self._refresh_image()
        self.root.after(80, self._poll_queue)

    # ----- post-scan save/skip -----
    def _ask_save(self, reason):
        """The scan streamed its rows to <output_csv>.part; ask whether to keep
        them. The data also stays in memory, so 'Save CSV as...' still works
        even after choosing skip."""
        if self.cfg is None or not self.cfg.output_csv:
            return
        tmp = self.cfg.output_csv + ".part"
        if not os.path.isfile(tmp):
            return
        if self.records and messagebox.askyesno(
                "Save scan?",
                f"Scan {reason}: {len(self.records)} points.\n\n"
                f"Save to {self.cfg.output_csv}?\n"
                "(Skip keeps the data in memory - you can still use "
                "'Save CSV as...')"):
            os.replace(tmp, self.cfg.output_csv)
            self.status.set(f"Saved {len(self.records)} points -> "
                            f"{os.path.basename(self.cfg.output_csv)}")
        else:
            try:
                os.remove(tmp)
            except OSError:
                pass
            self.status.set(f"Done ({reason}) - CSV skipped "
                            "(data still in memory).")

    # ----- the pmtPlot.py plots, from in-memory data -----
    def show_plots(self):
        if not self.records:
            messagebox.showinfo("No data", "Run a scan first.")
            return

        steps = np.array([r["step"] for r in self.records])
        xs = np.array([r["dx"] for r in self.records])   # degrees (user-facing)
        ys = np.array([r["dy"] for r in self.records])
        counts = np.array([r["count"] for r in self.records])
        overs = np.array([r["over"] for r in self.records])

        # 1) PMT vs step
        plt.figure(figsize=(12, 5))
        plt.plot(steps, counts)
        plt.xlabel("Step"); plt.ylabel("PMT Count")
        plt.title("PMT Signal Over Scan"); plt.grid(True)

        # 2) 2D scatter intensity map (y inverted -> top-left origin, like the
        #    live heatmap / a camera photo)
        plt.figure(figsize=(8, 7))
        sc = plt.scatter(xs, ys, c=counts, s=40)
        plt.colorbar(sc, label="PMT Count")
        plt.xlabel("X angle (deg)"); plt.ylabel("Y angle (deg)")
        plt.title("2D PMT Intensity Map")
        plt.axis("equal"); plt.grid(True)
        plt.gca().invert_yaxis()

        # 3) over-light map
        if np.any(overs == 1):
            normal = overs == 0
            over = overs == 1
            plt.figure(figsize=(8, 7))
            plt.scatter(xs[normal], ys[normal], c=counts[normal], s=40, label="Normal")
            plt.scatter(xs[over], ys[over], marker="x", s=80, label="OverLight")
            plt.xlabel("X angle (deg)"); plt.ylabel("Y angle (deg)")
            plt.title("PMT Map with OverLight Detection")
            plt.legend(); plt.axis("equal"); plt.grid(True)
            plt.gca().invert_yaxis()

        # 4) heatmap (top-left origin, like a photo)
        plt.figure(figsize=(8, 7))
        img = np.nan_to_num(self.image, nan=0.0)
        ext = degree_extent(self.cfg)
        plt.imshow(img, origin="upper", aspect="auto",
                   extent=[ext[0], ext[1], ext[3], ext[2]])
        plt.colorbar(label="PMT Count")
        plt.title("PMT Heatmap")
        plt.xlabel("X angle (deg)"); plt.ylabel("Y angle (deg)")

        plt.show(block=False)

    def save_csv_as(self):
        if not self.records:
            messagebox.showinfo("No data", "Run a scan first.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
            initialfile="pmt_output.csv")
        if not path:
            return
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["step", "x_deg", "y_deg", "x_voltage", "y_voltage",
                        "gate_number", "pmt_count", "over_light"])
            for r in self.records:
                w.writerow([r["step"], r["dx"], r["dy"], r["x"], r["y"],
                            "", r["count"], r["over"]])
        self.status.set(f"Saved {len(self.records)} points -> {os.path.basename(path)}")


def main():
    root = tk.Tk()
    ScannerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()

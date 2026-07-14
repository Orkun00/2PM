@echo off
REM ============================================================
REM Build a standalone EXE for the 2PM scanner.  Run this ONCE on
REM the Windows rig (or any Windows PC with both Pythons installed).
REM
REM What it produces:  dist\TwoPhotonScanner\
REM   TwoPhotonScanner.exe   <- users double-click THIS (no Python needed)
REM   pmt_helper.exe         <- 32-bit PMT helper, launched automatically
REM   H11890api.dll          <- Hamamatsu API DLL
REM   _internal\             <- bundled Python runtime + libraries
REM
REM Ship (copy) that whole folder to any rig PC. The PC still needs the
REM DRIVERS installed once (they cannot be bundled into an exe):
REM   * NI-DAQmx driver (for the galvo)
REM   * H11890 USB driver (RS SampleSoftware\driver\UPDATE_x86.exe ->
REM     installs the 32-bit libusb0.dll)
REM
REM Build requirements (this PC only, not the users'):
REM   * 64-bit Python  (py -3-64)  with: nidaqmx numpy matplotlib
REM   * 32-bit Python  (py -3.13-32) -- same one used by run.bat today
REM ============================================================

cd /d "%~dp0"

REM The running GUI locks its files in dist\ -- PyInstaller would fail
REM half-way and leave a BROKEN exe ("Failed to load Python DLL").
tasklist /FI "IMAGENAME eq TwoPhotonScanner.exe" | find /I "TwoPhotonScanner.exe" >nul
if not errorlevel 1 (
    echo.
    echo ERROR: TwoPhotonScanner.exe is still running. Close it, then re-run.
    goto :err
)
REM A leftover PMT helper (its GUI already gone) locks dist\ the same way.
taskkill /F /IM pmt_helper.exe >nul 2>&1

echo.
echo ==== [1/4] Building 32-bit PMT helper (pmt_helper.exe) ====
py -3.13-32 -m pip install --upgrade pyinstaller || goto :err
py -3.13-32 -m PyInstaller --noconfirm --onefile --name pmt_helper ^
    --distpath dist_helper --workpath build_helper pmt_helper.py || goto :err

echo.
echo ==== [2/4] Building 64-bit GUI (TwoPhotonScanner.exe) ====
REM --recursive-copy-metadata nidaqmx: nidaqmx (and its deps, e.g. nitypes)
REM read their version from package metadata at import time; without the
REM metadata bundled the frozen exe fails with "nidaqmx not installed".
py -3-64 -m pip install --upgrade pyinstaller || goto :err
py -3-64 -m PyInstaller --noconfirm --windowed --name TwoPhotonScanner ^
    --recursive-copy-metadata nidaqmx ^
    two_photon_scanner.py || goto :err

echo.
echo ==== [3/4] Assembling the final folder ====
copy /Y dist_helper\pmt_helper.exe dist\TwoPhotonScanner\ || goto :err
copy /Y H11890api.dll dist\TwoPhotonScanner\ || goto :err

echo.
echo ==== [4/4] Removing intermediate build folders ====
REM The exe inside build\TwoPhotonScanner\ is a PyInstaller WORK FILE -- it
REM cannot run ("Failed to load Python DLL"). Delete the work folders so the
REM only exe left is the real one in dist\TwoPhotonScanner\.
rmdir /s /q build 2>nul
rmdir /s /q build_helper 2>nul
rmdir /s /q dist_helper 2>nul

echo.
echo ============================================================
echo DONE.  Ship this folder:   dist\TwoPhotonScanner\
echo Users double-click:        dist\TwoPhotonScanner\TwoPhotonScanner.exe
echo (Each rig PC needs the NI-DAQmx + H11890 USB drivers installed once.)
echo ============================================================
goto :eof

:err
echo.
echo ******** BUILD FAILED - see the error above ********
exit /b 1

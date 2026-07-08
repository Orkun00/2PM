@echo off
REM ============================================================
REM Launch the 2PM scanner.
REM
REM TWO-PROCESS design:
REM   * This GUI runs in 64-BIT Python (Tkinter + matplotlib + galvo).
REM   * It launches pmt_helper.py in 32-BIT Python to load H11890api.dll
REM     (the DLL is x86-only). See the "Helper Python" field in the GUI.
REM
REM %~dp0 = the folder this .bat lives in, so paths work from anywhere.
REM ============================================================

py -3-64 "%~dp0two_photon_scanner.py"

if errorlevel 1 (
  echo.
  echo -------------------------------------------------------------
  echo Could not start with 64-bit Python via the "py" launcher.
  echo Fix one of these, then run again:
  echo   1^) Install 64-bit Python ^(python.org -^> Windows x86-64 installer^)
  echo   2^) Install GUI packages into it:
  echo        py -3-64 -m pip install nidaqmx numpy matplotlib
  echo   3^) The PMT helper needs a 32-bit Python on this PC too:
  echo        it is launched automatically using the command in the GUI's
  echo        "Helper Python" field ^(default: py -3.13-32^).
  echo   4^) Or edit this file to use your 64-bit python.exe full path, e.g.:
  echo        "C:\Users\You\AppData\Local\Programs\Python\Python314\python.exe" "%~dp0two_photon_scanner.py"
  echo -------------------------------------------------------------
)
pause

@echo off
REM Build the statusline shim (needs MinGW gcc in PATH, e.g. MSYS2 / mingw-w64).
setlocal
set SRC=%~dp0scripts\dspg-status.c
set OUT=%~dp0scripts\dspg-status.exe
gcc -O2 -o "%OUT%" "%SRC%" -lws2_32
if errorlevel 1 (
  echo [FAIL] build failed - is gcc in PATH?
  exit /b 1
)
echo [OK] %OUT%
echo Now point your statusLine command at it, e.g.:
echo   "command": "\"%OUT:\=/%\""
endlocal

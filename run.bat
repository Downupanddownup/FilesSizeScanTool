@echo off
setlocal
cd /d "%~dp0"

set "TARGET=%~1"
if not "%TARGET%"=="" goto normalize

echo.
echo   diskmap - scan a path and build an HTML report
echo.
set /p "TARGET=Scan path (Enter = C:\ ) : "

:normalize
if "%TARGET:~-1%"=="\" set "TARGET=%TARGET:~0,-1%"
if "%TARGET%"=="" set "TARGET=C:"

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  echo.
  echo [ERROR] Python 3 not found in PATH.
  echo Install Python 3.8+ and make sure "python" runs in cmd.
  echo.
  pause
  exit /b 1
)

echo.
echo Target : %TARGET%\
echo Working ...
echo.
%PY% "%~dp0diskmap.py" "%TARGET%\\"
if errorlevel 1 (
  echo.
  echo [ERROR] scan failed, see messages above.
  echo.
  pause
  exit /b 1
)

set "NEWEST="
for /f "delims=" %%i in ('dir /b /ad /o-d "reports" 2^>nul') do (
  if not defined NEWEST set "NEWEST=%%i"
)
if defined NEWEST start "" "%~dp0reports\%NEWEST%\index.html"
echo.
pause

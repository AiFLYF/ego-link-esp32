@echo off
REM ============================================================
REM  Clean-env flash + bounded telemetry capture for the rw1 device.
REM  Arg 1 = COM port (default COM10, override via RW1_PORT)
REM  Arg 2 = capture seconds (default 30).
REM ============================================================

set "MSYSTEM="
set "MSYS2_PATH_TYPE="
set "MINGW_PREFIX="
set "MINGW_CHOST="
set "PYTHONPATH="
set "PYTHONHOME="

set "IDF_PATH=D:\Espressif\frameworks\esp-idf-v5.4.3"
set "IDF_TOOLS_PATH=D:\Espressif"
set "IDF_PYTHON_DIR=D:\Espressif\python_env\idf5.4_py3.12_env\Scripts"
set "IDF_GIT_DIR=D:\Espressif\tools\idf-git\2.44.0\cmd"

set "PATH=%IDF_PYTHON_DIR%;%IDF_GIT_DIR%;C:\Windows\System32;C:\Windows;C:\Windows\System32\Wbem;C:\Windows\System32\WindowsPowerShell\v1.0"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=%RW1_PORT%"
if "%PORT%"=="" set "PORT=COM10"
set "CAPSECS=%~2"
if "%CAPSECS%"=="" set "CAPSECS=30"

call "%IDF_PATH%\export.bat"
if errorlevel 1 (
    echo [flash_device] export.bat failed
    exit /b 1
)

cd /d "%~dp0device"

python "%IDF_PATH%\tools\idf.py" -p %PORT% -D SDKCONFIG_DEFAULTS=sdkconfig.bsp.esp32_s3_eye flash
if errorlevel 1 (
    echo [flash_device] flash failed
    exit /b 1
)

echo [flash_device] capturing %CAPSECS%s of serial on %PORT%...
python "%~dp0tools\tools_serial_capture.py" %PORT% %CAPSECS%
exit /b %errorlevel%

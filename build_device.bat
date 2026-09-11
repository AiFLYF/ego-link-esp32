@echo off
REM ============================================================
REM  Clean-environment build for the rw1 device firmware.
REM  Resets the MSYS/MinGW-polluted PATH, activates ESP-IDF,
REM  then builds. Works from any checkout location (%~dp0).
REM  Adjust IDF_PATH to your own install if different.
REM ============================================================

set "MSYSTEM="
set "MSYS2_PATH_TYPE="
set "MINGW_PREFIX="
set "MINGW_CHOST="
set "PYTHONPATH="
set "PYTHONHOME="

if defined RW1_IDF_PATH ( set "IDF_PATH=%RW1_IDF_PATH%" ) else ( set "IDF_PATH=D:\Espressif\frameworks\esp-idf-v5.4.3" )
if defined RW1_IDF_TOOLS ( set "IDF_TOOLS_PATH=%RW1_IDF_TOOLS%" ) else ( set "IDF_TOOLS_PATH=D:\Espressif" )
set "IDF_PYTHON_DIR=D:\Espressif\python_env\idf5.4_py3.12_env\Scripts"
set "IDF_GIT_DIR=D:\Espressif\tools\idf-git\2.44.0\cmd"

set "PATH=%IDF_PYTHON_DIR%;%IDF_GIT_DIR%;C:\Windows\System32;C:\Windows;C:\Windows\System32\Wbem;C:\Windows\System32\WindowsPowerShell\v1.0"

call "%IDF_PATH%\export.bat"
if errorlevel 1 (
    echo.
    echo [build_device] export.bat failed - check IDF_PATH
    exit /b 1
)

cd /d "%~dp0device"

python "%IDF_PATH%\tools\idf.py" -D SDKCONFIG_DEFAULTS=sdkconfig.bsp.esp32_s3_eye build
exit /b %errorlevel%

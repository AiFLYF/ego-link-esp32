"""在 Git Bash 里调用 idf.py 的包装器。

ESP-IDF 5.4 的 tools/idf.py 结尾是这样写的：

    if 'MSYSTEM' in os.environ:
        print_warning('MSys/Mingw is no longer supported. ... continue at your own risk.')
    elif ...:
        ...
    else:
        main()

也就是说只要环境里有 MSYSTEM（Git for Windows 会强制注入，连 `env -u MSYSTEM`
都删不掉），它只打印一句警告就**直接结束，根本不执行 main()**。那句
"continue at your own risk" 是假的。

build_device.bat 用 `set "MSYSTEM="` 能在 cmd.exe 里真正删掉这个变量，所以正常
构建路径没问题；这个包装器只是给 bash/agent 用的绕行方案：在 Python 进程内先
pop 掉 MSYSTEM，再以 __main__ 身份执行 idf.py。

用法：
    python tools/idf_build.py -D SDKCONFIG_DEFAULTS=sdkconfig.bsp.esp32_s3_eye build
"""

import os
import runpy
import sys

for key in ("MSYSTEM", "MSYS", "MSYS2_ARG_CONV_EXCL", "MSYS_NO_PATHCONV",
            "MINGW_PREFIX", "MINGW_CHOST", "PYTHONHOME"):
    os.environ.pop(key, None)

idf_path = os.environ.get("IDF_PATH", r"D:\Espressif\frameworks\esp-idf-v5.4.3")
idf_py = os.path.join(idf_path, "tools", "idf.py")
if not os.path.isfile(idf_py):
    sys.exit("idf.py not found at %s (set IDF_PATH)" % idf_py)

# export.bat 会把 IDF_PATH/tools 放进 PYTHONPATH（python_version_checker 等模块
# 就在那里）。注意：进程启动后改 os.environ 不会回灌到 sys.path，必须直接插。
tools_dir = os.path.join(idf_path, "tools")
if tools_dir not in sys.path:
    sys.path.insert(0, tools_dir)

sys.argv = [idf_py] + sys.argv[1:]
runpy.run_path(idf_py, run_name="__main__")

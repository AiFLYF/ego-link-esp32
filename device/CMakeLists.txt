# AI交互课 第1周 · 开发板端工程（ESP32-S3-EYE ⇄ PC服务器）
cmake_minimum_required(VERSION 3.5)

# Trim the build: only "main" and the components it actually depends on.
set(COMPONENTS main)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(rw1_device)

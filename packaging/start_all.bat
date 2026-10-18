@echo off
chcp 65001 >nul
title 安卓App智能测试Agent系统 - 一键启动
cd /d "%~dp0"

echo ============================================================
echo          安卓App智能测试Agent系统 - 一键启动
echo ============================================================
echo.

REM ---- 0. 环境自检 ----
if not exist "test_agent\test_agent.exe" (
    echo [错误] 未找到 test_agent\test_agent.exe，请确认解压完整
    pause
    exit /b 1
)
if not exist "TestAgentDesktop\TestAgentDesktop.exe" (
    echo [错误] 未找到 TestAgentDesktop\TestAgentDesktop.exe，请确认解压完整
    pause
    exit /b 1
)
if not exist "test_agent\.env" (
    echo [警告] test_agent\.env 不存在！请在 test_agent 目录创建 .env 并配置密钥，
    echo        或启动后在界面中直接填写 API Key（仅内存使用）
    echo.
)

REM ---- 1. 检查安卓设备 ----
echo [1/4] 检查安卓设备（模拟器/真机）...
where adb >nul 2>nul
if errorlevel 1 (
    echo [警告] 未检测到 adb 命令，请确认已安装 Android platform-tools
) else (
    adb devices | findstr /R "emulator- device$"
)
echo        提示：请确保模拟器已启动，或真机已连接并开启 USB 调试
echo.

REM ---- 2. 启动 Appium ----
echo [2/4] 启动 Appium 服务（新窗口，运行期间请勿关闭）...
where appium >nul 2>nul
if errorlevel 1 (
    echo [警告] 未检测到 appium 命令，请先安装: npm install -g appium
    echo        并安装驱动: appium driver install uiautomator2
) else (
    start "Appium服务(勿关)" cmd /k "appium --address 127.0.0.1 --port 4723"
)
echo.

REM ---- 3. 启动后端服务 ----
echo [3/4] 启动后端服务 test_agent.exe（新窗口，运行期间请勿关闭）...
start "测试Agent后端(勿关)" "%~dp0test_agent\test_agent.exe"
echo        等待后端就绪（首次启动加载模型约需 10~30 秒）...
timeout /t 10 /nobreak >nul
echo.

REM ---- 4. 启动桌面客户端 ----
echo [4/4] 启动桌面客户端...
start "" "%~dp0TestAgentDesktop\TestAgentDesktop.exe"
echo.
echo ============================================================
echo   全部启动完成！
echo   桌面客户端默认连接 http://127.0.0.1:8000
echo   也可用浏览器直接访问 http://127.0.0.1:8000
echo   密钥：可在界面中填写（仅内存使用），或写入 test_agent\.env
echo   详细说明请阅读【使用说明.txt】
echo ============================================================
pause

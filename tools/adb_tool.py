# -*- coding: utf-8 -*-
"""
ADB 工具模块
通过 subprocess 调用 adb 命令，提供：
- 抓取安卓设备 logcat 日志并保存到本地文件
- 对设备屏幕截图并拉取保存到本地
- 读取本地日志文件内容（供 LLM 分析使用）

前置条件（重要）：
1. 需预先启动安卓模拟器，或连接真机并开启 USB 调试
2. adb 路径由 settings.adb_path 自动探测（项目内 SDK 或系统 PATH）
"""

import subprocess
import time
from pathlib import Path

from config.settings import settings
from utils.logger import logger


def _run_adb_command(args: list, timeout: int = 60) -> subprocess.CompletedProcess:
    """执行 adb 命令的通用封装。

    自动拼接 adb 路径与目标设备序列号（adb -s <serial>）。

    Args:
        args: adb 子命令参数列表（不含 adb 与 -s 设备号）。
        timeout: 命令超时时间（秒）。

    Returns:
        subprocess.CompletedProcess: 命令执行结果对象。

    Raises:
        subprocess.TimeoutExpired: 命令执行超时。
        FileNotFoundError: adb 可执行文件不存在。
    """
    cmd = [settings.adb_path, "-s", settings.adb_device_serial] + args
    logger.debug(f"执行 adb 命令: {' '.join(cmd)}")
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="ignore",
    )


def capture_logcat(tag: str = "test_run") -> str:
    """抓取设备 logcat 日志并保存到本地文件。

    先清空历史日志缓冲，再抓取当前缓冲内容，保证日志与本轮测试强相关。

    Args:
        tag: 日志文件标识，用于区分不同测试轮次。

    Returns:
        str: 保存成功的本地日志文件路径；失败返回空字符串。
    """
    try:
        # 清空 logcat 缓冲区，仅保留本轮测试期间的日志
        _run_adb_command(["logcat", "-c"], timeout=15)
        # 抓取当前缓冲区的全部日志（动作执行窗口由 execute_test 节点控制）
        result = _run_adb_command(["logcat", "-d", "-v", "threadtime"], timeout=60)

        if result.returncode != 0:
            logger.error(f"抓取 logcat 失败: {result.stderr}")
            return ""

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        log_file = Path(settings.log_output_dir) / f"logcat_{tag}_{timestamp}.log"
        log_file.write_text(result.stdout, encoding="utf-8")
        logger.info(f"logcat 日志已保存: {log_file}")
        return str(log_file)
    except subprocess.TimeoutExpired:
        logger.error("抓取 logcat 命令超时", exc_info=True)
        return ""
    except FileNotFoundError:
        logger.error(f"未找到 adb 命令（adb_path={settings.adb_path}），请检查 SDK 配置")
        return ""
    except Exception as e:
        logger.error(f"抓取 logcat 出现未知异常: {e}", exc_info=True)
        return ""


def capture_screenshot(tag: str = "test_run") -> str:
    """对设备当前屏幕截图，并拉取保存到本地目录。

    Args:
        tag: 截图文件标识，用于区分不同测试轮次。

    Returns:
        str: 保存成功的本地截图路径；失败返回空字符串。
    """
    device_screenshot_path = "/sdcard/screen_agent.png"
    try:
        # 在设备端执行截屏
        screencap_result = _run_adb_command(
            ["shell", "screencap", "-p", device_screenshot_path], timeout=30
        )
        if screencap_result.returncode != 0:
            logger.error(f"设备端截屏失败: {screencap_result.stderr}")
            return ""

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        local_file = Path(settings.screenshot_output_dir) / f"screenshot_{tag}_{timestamp}.png"

        # 将截图从设备拉取到本地
        pull_result = _run_adb_command(["pull", device_screenshot_path, str(local_file)], timeout=30)
        if pull_result.returncode != 0:
            logger.error(f"拉取截图到本地失败: {pull_result.stderr}")
            return ""

        logger.info(f"截图已保存: {local_file}")
        return str(local_file)
    except subprocess.TimeoutExpired:
        logger.error("截图命令执行超时", exc_info=True)
        return ""
    except FileNotFoundError:
        logger.error(f"未找到 adb 命令（adb_path={settings.adb_path}），请检查 SDK 配置")
        return ""
    except Exception as e:
        logger.error(f"截图出现未知异常: {e}", exc_info=True)
        return ""


def read_log_file(log_path: str, max_chars: int = 15000) -> str:
    """读取本地 logcat 日志文件内容（供 LLM 分析使用）。

    为避免超出 LLM 上下文长度，仅截取文件末尾 max_chars 个字符。

    Args:
        log_path: 本地日志文件路径。
        max_chars: 最大读取字符数。

    Returns:
        str: 日志文本内容；失败返回空字符串。
    """
    try:
        content = Path(log_path).read_text(encoding="utf-8", errors="ignore")
        if len(content) > max_chars:
            content = content[-max_chars:]
        return content
    except Exception as e:
        logger.error(f"读取日志文件失败 {log_path}: {e}", exc_info=True)
        return ""

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
import uuid
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from config.settings import settings
from utils.logger import logger

# 缺陷分析时优先保留的日志级别（应用进程的 W/E/F 才可能包含崩溃/ANR 证据）
_RELEVANT_LOG_LEVELS = {"W", "E", "F", "A"}


def _unique_suffix() -> str:
    """生成“秒级时间戳 + 随机后缀”，避免同一秒内多次采集互相覆盖。"""
    return f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


def _run_adb_command(
    args: list, timeout: int = 60, device_serial: Optional[str] = None
) -> subprocess.CompletedProcess:
    """执行 adb 命令的通用封装。

    自动拼接 adb 路径与目标设备序列号（adb -s <serial>）。
    多设备在线时必须显式传入本次任务的目标设备序列号，
    避免误操作到 .env 默认设备（如模拟器已下线时报 device not found）。

    Args:
        args: adb 子命令参数列表（不含 adb 与 -s 设备号）。
        timeout: 命令超时时间（秒）。
        device_serial: 目标设备序列号覆盖值；None 时使用 settings 默认设备。

    Returns:
        subprocess.CompletedProcess: 命令执行结果对象。

    Raises:
        subprocess.TimeoutExpired: 命令执行超时。
        FileNotFoundError: adb 可执行文件不存在。
    """
    serial = (device_serial or "").strip() or settings.adb_device_serial
    cmd = [settings.adb_path, "-s", serial] + args
    logger.debug(f"执行 adb 命令: {' '.join(cmd)}")
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="ignore",
    )


def clear_logcat(device_serial: Optional[str] = None) -> bool:
    """清空设备 logcat 缓冲区，标记本轮测试的日志起点。

    必须在执行测试动作之前调用，否则会把本轮日志一起清掉。

    Args:
        device_serial: 目标设备序列号覆盖值；None 使用默认设备。

    Returns:
        bool: 清空成功返回 True；失败返回 False（不阻断测试主流程）。
    """
    try:
        result = _run_adb_command(
            ["logcat", "-c"], timeout=15, device_serial=device_serial
        )
        if result.returncode != 0:
            logger.warning(f"清空 logcat 失败: {result.stderr}")
            return False
        logger.info(f"已清空 logcat 缓冲区（设备: {device_serial or settings.adb_device_serial}）")
        return True
    except subprocess.TimeoutExpired:
        logger.warning("清空 logcat 命令超时")
        return False
    except FileNotFoundError:
        logger.warning(f"未找到 adb 命令（adb_path={settings.adb_path}），跳过日志清空")
        return False
    except Exception as e:
        logger.warning(f"清空 logcat 出现未知异常: {e}")
        return False


def prepare_device(device_serial: Optional[str] = None) -> Tuple[bool, str]:
    """唤醒设备并检查锁屏/连接状态，避免自动化动作落在锁屏界面。

    仅执行唤醒与 `wm dismiss-keyguard`，不会修改、保存或猜测任何锁屏密码。
    若设备仍处于安全锁屏，返回可读错误，提示用户手动解锁。

    Args:
        device_serial: 目标设备序列号覆盖值；None 使用默认设备。

    Returns:
        Tuple[bool, str]: (设备是否可用, 说明文本)。
    """
    serial = (device_serial or "").strip() or settings.adb_device_serial
    try:
        state = _run_adb_command(["get-state"], timeout=10, device_serial=serial)
        if state.returncode != 0 or state.stdout.strip() != "device":
            detail = (state.stderr or state.stdout or "adb 未返回 device 状态").strip()
            return False, f"设备未就绪（{serial}）: {detail}"

        # 唤醒并尝试关闭非安全锁屏；安全锁屏仍需用户手动输入密码
        _run_adb_command(
            ["shell", "input", "keyevent", "KEYCODE_WAKEUP"],
            timeout=10,
            device_serial=serial,
        )
        _run_adb_command(
            ["shell", "wm", "dismiss-keyguard"], timeout=10, device_serial=serial
        )

        window = _run_adb_command(
            ["shell", "dumpsys", "window"], timeout=20, device_serial=serial
        )
        window_text = window.stdout or ""
        locked_flags = (
            "mDreamingLockscreen=true",
            "mShowingLockscreen=true",
            "mKeyguardShowing=true",
        )
        if window.returncode == 0 and any(flag in window_text for flag in locked_flags):
            return False, "设备处于锁屏状态，请先手动解锁（程序不会修改或保存锁屏密码）"
        return True, f"设备已就绪（{serial}）"
    except subprocess.TimeoutExpired:
        logger.warning("设备就绪检查超时")
        return False, "设备就绪检查超时，请确认 adb 连接正常"
    except FileNotFoundError:
        logger.warning(f"未找到 adb 命令（adb_path={settings.adb_path}）")
        return False, f"未找到 adb 命令（adb_path={settings.adb_path}）"
    except Exception as e:
        logger.warning(f"设备就绪检查异常: {e}")
        return False, f"设备就绪检查异常: {e}"


def tap(x: int, y: int, device_serial: Optional[str] = None) -> bool:
    """通过 ADB 在屏幕坐标处执行一次点击。

    部分第三方 App 的自定义控件不响应 UiAutomator2 的无障碍点击，
    但可以响应系统级触摸注入；Appium 后端在点击元素时优先使用本函数。

    Args:
        x: 屏幕横坐标（像素）。
        y: 屏幕纵坐标（像素）。
        device_serial: 目标设备序列号覆盖值；None 使用默认设备。

    Returns:
        bool: adb 命令执行成功返回 True。
    """
    try:
        result = _run_adb_command(
            ["shell", "input", "tap", str(int(x)), str(int(y))],
            timeout=10,
            device_serial=device_serial,
        )
        if result.returncode != 0:
            logger.warning(f"ADB 点击失败 ({x}, {y}): {result.stderr.strip()}")
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.warning(f"ADB 点击超时 ({x}, {y})")
        return False
    except Exception as e:
        logger.warning(f"ADB 点击异常 ({x}, {y}): {e}")
        return False


def swipe(
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    duration_ms: int = 300,
    device_serial: Optional[str] = None,
) -> bool:
    """通过 ADB 执行一次滑动。

    Args:
        start_x: 起点横坐标（像素）。
        start_y: 起点纵坐标（像素）。
        end_x: 终点横坐标（像素）。
        end_y: 终点纵坐标（像素）。
        duration_ms: 滑动时长（毫秒）。
        device_serial: 目标设备序列号覆盖值；None 使用默认设备。

    Returns:
        bool: adb 命令执行成功返回 True。
    """
    try:
        result = _run_adb_command(
            [
                "shell",
                "input",
                "swipe",
                str(int(start_x)),
                str(int(start_y)),
                str(int(end_x)),
                str(int(end_y)),
                str(max(50, int(duration_ms))),
            ],
            timeout=12,
            device_serial=device_serial,
        )
        if result.returncode != 0:
            logger.warning(f"ADB 滑动失败: {result.stderr.strip()}")
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.warning("ADB 滑动超时")
        return False
    except Exception as e:
        logger.warning(f"ADB 滑动异常: {e}")
        return False


def keyevent(keycode: str | int, device_serial: Optional[str] = None) -> bool:
    """向设备发送一次按键事件（如 KEYCODE_BACK / KEYCODE_ENTER）。

    逐步 UI 驱动 Agent 用返回键关闭弹窗或广告；普通静态动作不会调用本函数。

    Args:
        keycode: Android 键名（KEYCODE_BACK）或键值（4）。
        device_serial: 目标设备序列号覆盖值；None 使用默认设备。

    Returns:
        bool: adb 命令执行成功返回 True。
    """
    code = str(keycode or "").strip()
    if not code:
        return False
    try:
        result = _run_adb_command(
            ["shell", "input", "keyevent", code],
            timeout=10,
            device_serial=device_serial,
        )
        if result.returncode != 0:
            logger.warning(f"ADB 按键失败 ({code}): {result.stderr.strip()}")
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.warning(f"ADB 按键超时 ({code})")
        return False
    except Exception as e:
        logger.warning(f"ADB 按键异常 ({code}): {e}")
        return False


def kill_processes_by_pattern(
    pattern: str, device_serial: Optional[str] = None
) -> int:
    """按进程命令行关键字结束设备端进程（用于清理跨后端残留）。

    仅匹配 `ps -A` 输出中包含完整 pattern 的进程，pattern 必须足够具体，
    避免误杀其他应用。无匹配时返回 0。

    Args:
        pattern: 进程命令行关键字（如 com.mobilenext.mobilecli.DeviceServer）。
        device_serial: 目标设备序列号覆盖值；None 使用默认设备。

    Returns:
        int: 成功结束的进程数量。
    """
    pattern = (pattern or "").strip()
    if not pattern:
        return 0
    try:
        result = _run_adb_command(
            ["shell", "ps", "-A"], timeout=20, device_serial=device_serial
        )
        if result.returncode != 0:
            logger.debug(f"读取设备进程列表失败，跳过清理 {pattern}: {result.stderr}")
            return 0

        killed = 0
        for line in (result.stdout or "").splitlines():
            if pattern not in line:
                continue
            fields = line.split()
            if len(fields) < 2 or not fields[1].isdigit():
                continue
            pid = fields[1]
            kill_result = _run_adb_command(
                ["shell", "kill", "-9", pid], timeout=10, device_serial=device_serial
            )
            if kill_result.returncode == 0:
                killed += 1
                logger.info(f"已清理设备端残留进程: {pattern} (PID {pid})")
        return killed
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
        logger.debug(f"清理设备端进程 {pattern} 失败（忽略）: {e}")
        return 0


def stop_mobile_mcp_device_server(device_serial: Optional[str] = None) -> int:
    """清理 mobile-mcp 在设备端遗留的 DeviceServer（会占用 UiAutomation）。

    mobile-mcp 与 Appium 共用 Android UiAutomation；若 DeviceServer 未退出，
    Appium 会报 `UiAutomationService ... already registered`。切换后端前调用
    本函数可避免该冲突。

    Args:
        device_serial: 目标设备序列号覆盖值；None 使用默认设备。

    Returns:
        int: 成功结束的进程数量。
    """
    return kill_processes_by_pattern(
        "com.mobilenext.mobilecli.DeviceServer", device_serial=device_serial
    )


def clear_focused_text(
    device_serial: Optional[str] = None, char_count: int = 100
) -> bool:
    """通过 ADB 按键事件清空当前焦点输入框（供 mobile-mcp 等无 clear API 的后端使用）。

    先移动光标到末尾，再发送 DEL 键。Android 的 `input keyevent` 支持一次传入
    多个键值，因此按 50 个一批发送，避免逐键启动 adb 进程。

    Args:
        device_serial: 目标设备序列号覆盖值；None 使用默认设备。
        char_count: 预期删除的字符数；至少 1，最多 200。

    Returns:
        bool: 全部按键事件发送成功返回 True。
    """
    serial = (device_serial or "").strip() or settings.adb_device_serial
    try:
        count = max(1, min(int(char_count), 200))
        _run_adb_command(
            ["shell", "input", "keyevent", "KEYCODE_MOVE_END"],
            timeout=10,
            device_serial=serial,
        )
        for start in range(0, count, 50):
            keycodes = ["KEYCODE_DEL"] * min(50, count - start)
            result = _run_adb_command(
                ["shell", "input", "keyevent", *keycodes],
                timeout=30,
                device_serial=serial,
            )
            if result.returncode != 0:
                # 部分旧版本 input 不支持一次传多个键值，退化为逐键删除
                logger.debug("批量 DEL 失败，回退为逐键删除")
                for _ in range(len(keycodes)):
                    single = _run_adb_command(
                        ["shell", "input", "keyevent", "KEYCODE_DEL"],
                        timeout=10,
                        device_serial=serial,
                    )
                    if single.returncode != 0:
                        logger.warning(f"清空输入框按键失败: {single.stderr}")
                        return False
        logger.info(f"已通过 ADB 清空焦点输入框（设备: {serial}）")
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError) as e:
        logger.warning(f"清空输入框失败: {e}")
        return False
    except Exception as e:
        logger.warning(f"清空输入框出现未知异常: {e}")
        return False


def force_stop_app(package: str, device_serial: Optional[str] = None) -> bool:
    """强制停止指定应用（用于用例间重置状态）。"""
    package = (package or "").strip()
    if not package:
        return False
    try:
        result = _run_adb_command(
            ["shell", "am", "force-stop", package],
            timeout=15,
            device_serial=device_serial,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
        logger.warning(f"强制停止应用 {package} 失败: {e}")
        return False


def start_app(
    package: str, activity: str = "", device_serial: Optional[str] = None
) -> bool:
    """启动指定应用（用于用例间重置状态或重启失败兜底）。"""
    package = (package or "").strip()
    activity = (activity or "").strip()
    if not package:
        return False
    try:
        if activity:
            component = f"{package}/{activity}"
            result = _run_adb_command(
                ["shell", "am", "start", "-n", component],
                timeout=20,
                device_serial=device_serial,
            )
        else:
            result = _run_adb_command(
                [
                    "shell",
                    "monkey",
                    "-p",
                    package,
                    "-c",
                    "android.intent.category.LAUNCHER",
                    "1",
                ],
                timeout=20,
                device_serial=device_serial,
            )
        if result.returncode != 0:
            logger.warning(f"启动应用 {package} 失败: {result.stderr}")
            return False
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
        logger.warning(f"启动应用 {package} 出现异常: {e}")
        return False


def get_app_pids(
    package: str, device_serial: Optional[str] = None
) -> List[int]:
    """获取被测应用及其子进程的 PID 列表。

    优先解析 ``ps -A`` 的进程名，兼容 ``package:remote`` 子进程；
    ``ps -A`` 不可用时回退到 ``pidof <package>``（只覆盖主进程）。

    Args:
        package: 被测应用包名。
        device_serial: 目标设备序列号覆盖值；None 使用默认设备。

    Returns:
        List[int]: 去重并升序排列的 PID；无法获取时返回空列表。
    """
    package = (package or "").strip()
    if not package:
        return []

    pids: set = set()
    try:
        result = _run_adb_command(
            ["shell", "ps", "-A"], timeout=20, device_serial=device_serial
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                fields = line.split()
                # Android/toybox ps 常见格式：USER PID ... NAME（NAME 在最后一列）
                if len(fields) < 2 or not fields[1].isdigit():
                    continue
                name = fields[-1]
                if name == package or name.startswith(f"{package}:"):
                    pids.add(int(fields[1]))
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
        logger.debug(f"通过 ps 获取应用 PID 失败，尝试 pidof: {e}")

    if not pids:
        try:
            result = _run_adb_command(
                ["shell", "pidof", package], timeout=15, device_serial=device_serial
            )
            if result.returncode == 0:
                for token in result.stdout.split():
                    if token.isdigit():
                        pids.add(int(token))
        except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
            logger.debug(f"通过 pidof 获取应用 PID 失败: {e}")

    ordered = sorted(pids)
    if ordered:
        logger.info(f"被测应用 {package} 的进程 PID: {ordered}")
    else:
        logger.warning(f"未获取到被测应用 {package} 的 PID，日志过滤将回退到包名匹配")
    return ordered


def capture_logcat(
    tag: str = "test_run",
    device_serial: Optional[str] = None,
    clear_first: bool = True,
    output_dir: Optional[str] = None,
    buffers: Optional[str] = None,
) -> str:
    """抓取设备 logcat 日志并保存到本地文件。

    默认先清空历史日志缓冲再抓取；若调用方已在测试动作开始前调用
    clear_logcat()，应传入 clear_first=False，保留本轮测试期间产生的日志。

    Args:
        tag: 日志文件标识，用于区分不同测试轮次。
        device_serial: 目标设备序列号覆盖值（多设备在线时必传）；None 使用默认设备。
        clear_first: 抓取前是否清空 logcat 缓冲区。
        output_dir: 日志保存目录；为空时使用 settings.log_output_dir。
        buffers: logcat 缓冲区，如 ``main,system,crash``；为空时使用 adb 默认值。

    Returns:
        str: 保存成功的本地日志文件路径；失败返回空字符串。
    """
    try:
        if clear_first:
            # 未预先标记起点时保持旧行为：仅保留抓取前的短时间日志
            _run_adb_command(["logcat", "-c"], timeout=15, device_serial=device_serial)
        # 抓取当前缓冲区的全部日志（动作执行窗口由 execute_test 节点控制）
        logcat_args = ["logcat"]
        if buffers:
            logcat_args.extend(["-b", buffers])
        logcat_args.extend(["-d", "-v", "threadtime"])
        result = _run_adb_command(
            logcat_args, timeout=60, device_serial=device_serial
        )
        if result.returncode != 0 and buffers:
            # 旧版本 adb/logcat 可能不支持逗号分隔的 -b 参数，回退默认缓冲区
            logger.warning(
                f"指定 logcat 缓冲区失败，回退默认缓冲区: {result.stderr.strip()}"
            )
            result = _run_adb_command(
                ["logcat", "-d", "-v", "threadtime"],
                timeout=60,
                device_serial=device_serial,
            )

        if result.returncode != 0:
            logger.error(f"抓取 logcat 失败: {result.stderr}")
            return ""

        log_dir = Path(output_dir or settings.log_output_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"logcat_{tag}_{_unique_suffix()}.log"
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


def capture_screenshot(
    tag: str = "test_run",
    device_serial: Optional[str] = None,
    output_dir: Optional[str] = None,
) -> str:
    """对设备当前屏幕截图，并拉取保存到本地目录。

    Args:
        tag: 截图文件标识，用于区分不同测试轮次。
        device_serial: 目标设备序列号覆盖值（多设备在线时必传）；None 使用默认设备。
        output_dir: 截图保存目录；为空时使用 settings.screenshot_output_dir。

    Returns:
        str: 保存成功的本地截图路径；失败返回空字符串。
    """
    device_screenshot_path = "/sdcard/screen_agent.png"
    try:
        # 在设备端执行截屏
        screencap_result = _run_adb_command(
            ["shell", "screencap", "-p", device_screenshot_path],
            timeout=30,
            device_serial=device_serial,
        )
        if screencap_result.returncode != 0:
            logger.error(f"设备端截屏失败: {screencap_result.stderr}")
            return ""

        screenshot_dir = Path(output_dir or settings.screenshot_output_dir)
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        local_file = screenshot_dir / f"screenshot_{tag}_{_unique_suffix()}.png"

        # 将截图从设备拉取到本地
        pull_result = _run_adb_command(
            ["pull", device_screenshot_path, str(local_file)],
            timeout=30,
            device_serial=device_serial,
        )
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


def _parse_threadtime_pid_level(log_line: str) -> Tuple[Optional[int], str]:
    """解析 threadtime 格式日志行的 PID 与级别。

    Args:
        log_line: 形如 ``10-31 14:25:30.123  1234  5678 E AndroidRuntime: ...`` 的行。

    Returns:
        Tuple[Optional[int], str]: (PID, 级别字母)；无法解析时为 (None, "")。
    """
    tokens = log_line.split()
    if len(tokens) >= 5 and tokens[2].isdigit() and len(tokens[4]) == 1:
        return int(tokens[2]), tokens[4].upper()
    return None, ""


def extract_relevant_log(
    log_path: str,
    app_package: str = "",
    app_pids: Optional[Iterable[int]] = None,
    keywords: Iterable[str] = (),
    max_chars: int = 15000,
) -> str:
    """从完整日志中提取与缺陷分析相关的行。

    保留规则：
    1. 命中 Crash/ANR 等关键字的行（不论 PID，保留系统进程对被测 App 的
       ANR/Force finishing 记录）；
    2. 被测应用 PID 或包名相关的 W/E/F/A 级别日志；
    3. 过滤后内容过少时回退到完整日志尾部，避免漏掉关键上下文。

    Args:
        log_path: 本地 logcat 日志文件路径。
        app_package: 被测应用包名，用于包名兜底匹配。
        app_pids: 被测应用 PID 集合（执行前后采集的并集）。
        keywords: 缺陷关键字列表。
        max_chars: 返回内容的最大字符数（从末尾截取）。

    Returns:
        str: 过滤后的日志文本；读取失败返回空字符串。
    """
    try:
        content = Path(log_path).read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        logger.error(f"读取日志文件失败 {log_path}: {e}", exc_info=True)
        return ""

    pids = {int(pid) for pid in (app_pids or []) if str(pid).isdigit()}
    package = (app_package or "").strip()
    keyword_list = [str(keyword).lower() for keyword in keywords if str(keyword).strip()]

    kept: List[str] = []
    for line in content.splitlines():
        stripped = line.rstrip()
        if not stripped:
            continue
        pid, level = _parse_threadtime_pid_level(stripped)
        lower = stripped.lower()
        if any(keyword in lower for keyword in keyword_list):
            kept.append(stripped)
            continue
        is_app_line = (pid is not None and pid in pids) or (
            bool(package) and package in stripped
        )
        if is_app_line and (level in _RELEVANT_LOG_LEVELS or not level):
            kept.append(stripped)

    filtered = "\n".join(kept)
    # 完全没有命中（例如 PID 采集失败且日志中没有关键字）时回退到完整日志
    # 尾部，避免缺陷分析拿到空字符串；只要有证据行就不再混入无关日志。
    if not kept:
        logger.warning("按 PID/级别过滤后没有命中任何行，回退到完整日志尾部")
        filtered = content
    if max_chars > 0 and len(filtered) > max_chars:
        filtered = filtered[-max_chars:]

    logger.info(
        f"日志过滤完成: 原始 {len(content)} 字符 -> 输出 {len(filtered)} 字符，"
        f"应用 PID={sorted(pids) or '未知'}，包名={package or '未提供'}"
    )
    return filtered

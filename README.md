# 面向安卓App的智能测试Agent

基于 **LangGraph + RAG 知识库 + Appium / mobile-mcp + ADB** 的安卓 App 自动化测试原型系统（本科毕业设计，对标小米安卓 App 测试场景）。

> **当前版本：V1.3.0**（2026-10-31）｜[版本迭代与变更记录](docs/项目版本迭代记录.md)

输入 App 测试需求，Agent 自动完成完整测试链路：

```
RAG检索测试知识库 → LLM生成测试用例 → 规划执行动作 → Appium 或 mobile-mcp 操作模拟器/真机App
→ adb抓取日志+截图 → LLM+关键字扫描分析缺陷（Crash/ANR/UI异常） → 输出Markdown缺陷测试报告
```

## 功能特性

- **Web 可视化操作页面**：纯原生 HTML+JS 单页（templates/index.html），支持知识库上传、配置展示、一键启动测试、结果展示，无需 Postman 调试接口
- **前端临时密钥**：页面可直接填写 qwen / DeepSeek API Key，密钥仅在本次任务内存中使用，不写入任何文件，刷新即失效；也可使用 .env 中的密钥作为备选，两种方式兼容
- **多模型一键切换**：`LLM_PROVIDER=qwen/deepseek`，均为 OpenAI 兼容接口，密钥外置于 .env
- **执行后端可切换（方案 B）**：默认 `DEVICE_BACKEND=appium`；可切换为 `mobile_mcp`，通过 MCP 协议调用 mobile-next/mobile-mcp，页面/桌面端也可按单次任务覆盖，便于做 Appium 与 MCP 后端的对比实验
- **真机/模拟器一键切换**：`IS_REAL_DEVICE=false/true`，真机自动注入 UDID capability；页面也可对单次任务临时覆盖设备模式与 UDID
- **RAG 知识库**：txt 文档上传 → 文本切分 → Chroma 向量入库 → top3 相似度检索
- **执行失败自动重试**：LangGraph 条件分支，全部动作执行失败自动重新规划（最多 1 次）；每次尝试的结果都会保留，不会用后一轮覆盖前一轮
- **任务产物隔离**：每次任务生成 `run_id`，日志、截图、报告统一写入 `outputs/runs/<run_id>/`，多次测试互不混杂
- **敏感输入脱敏**：`input_text` 内容在日志、动作结果、重规划提示词与 Markdown 报告中统一替换为长度占位符；静态模式同样拒绝向密码字段输入内容、拒绝点击购买/订阅/支付/分享控件
- **缺陷识别增强**：LLM 分析 + logcat Crash/ANR 关键字预扫描双重保障；日志先按被测应用 PID/包名与 W/E/F 级别过滤，再交给 LLM，降低无关日志干扰
- **LLM 调用韧性**：显式超时 + 指数退避重试（认证/参数错误不重试）；动作与缺陷输出经过 Pydantic/参数校验，非法条目不进入执行与报告
- **Markdown 测试报告**：按 run_id 命名，自动写入测试用例、全部执行尝试、缺陷列表、异常诊断、日志与截图路径

## 版本迭代

| 版本 | 迭代主题 | 关键变化 |
| --- | --- | --- |
| V1.0.0 | 基础链路版 | RAG + 六节点 Agent + Appium/ADB + 缺陷分析 + Markdown 报告 + Web/桌面端 |
| V1.1.0 | 真机安全增强版 | USB 真机/UDID、UI 树包名隔离、密码字段隐藏与拒输、敏感控件识别 |
| V1.2.0 | 双后端与逐步 UI 驱动版 | DeviceDriver 协议、mobile-mcp 后端、逐步 UI 闭环、多用例逐条执行 |
| **V1.3.0** | **工程化增强版（当前）** | **输入脱敏、静态安全闸门、run_id 产物隔离、执行尝试合并、日志过滤、LLM 重试与结构化校验** |

### V1.3.0 本次变更点

- **安全**：`input_text` 内容在日志、结果、重规划提示词和报告中统一脱敏；密码字段拒输、敏感控件拒点；坐标点击默认要求命中已解析节点。
- **可追溯**：每次任务生成 `run_id`，日志/截图/报告写入 `outputs/runs/<run_id>/`；重规划前后的每次执行尝试都保留，报告按轮次展示。
- **稳定性**：logcat 按被测应用 PID/包名与 W/E/F/A 级别过滤；LLM 增加超时、指数退避重试和动作/缺陷结构化校验。
- **错误语义**：向量库重建失败抛异常并由 API 返回 500，空目录与真实失败不再混淆。
- **兼容性**：`execute_results` 路由语义不变；新增参数均可选，旧调用方式仍可用。

完整变更、涉及文件和验证记录见 [docs/项目版本迭代记录.md](docs/项目版本迭代记录.md)。

## 技术栈

Python 3.10+，FastAPI，LangGraph，LangChain，Chroma，sentence-transformers（all-MiniLM-L6-v2），Appium-Python-Client（UiAutomator2），MCP Python SDK（mobile-mcp 可选后端），pydantic v2，uvicorn，Jinja2。

## 目录结构

```
test_agent/
├── config/settings.py        # 全局配置（pydantic BaseSettings，读取 .env）
├── templates/index.html      # Web 操作首页（纯原生 HTML+JS）
├── tools/                    # device_driver / appium_tool / mobile_mcp_tool / adb_tool / report_tool
├── rag/                      # document_loader / vector_store
├── agent/                    # state / schemas / nodes（6节点）/ graph（含重试分支）
├── api/server.py             # FastAPI 接口 + 页面路由
├── utils/                    # logger / resource_path / redaction / run_context / ui_tree
├── docs/                     # 开发实施文档、教学文稿、版本迭代记录
├── outputs/runs/<run_id>/    # 单次任务产物：logs / screenshots / reports
├── build_exe.py              # PyInstaller 打包脚本
├── main.py                   # 项目入口
├── .env / .env.example       # 密钥与设备配置（.env 不入库）
└── requirements.txt
```

## 执行后端切换（Appium / mobile-mcp）

`execute_test` 节点依赖统一的 `DeviceDriver` 协议（`tools/device_driver.py`），当前提供两个实现：

| 后端 | 实现 | 定位 |
| --- | --- | --- |
| `appium`（默认） | `tools/appium_tool.py`，Appium + UiAutomator2 | 稳定基线，现有流程不变 |
| `mobile_mcp` | `tools/mobile_mcp_tool.py`，MCP + mobile-next/mobile-mcp | 可选实验后端，用于对比 MCP 工具调用与固定动作接口 |

### 切换方式

1. **全局配置**：在 `.env` 中设置 `DEVICE_BACKEND=mobile_mcp`，重启后端；
2. **单次任务覆盖**：Web 首页“执行后端”下拉框，或桌面客户端右侧“执行后端”下拉框，仅对本次任务生效。

无论使用哪个后端，**日志与截图仍由 adb 采集**（`clear_logcat` 在动作执行前标记起点），因此缺陷分析与报告链路保持一致，便于控制变量。

### mobile-mcp 前置条件

- Node.js 20+ 与 `npx` 可用（Windows 下程序自动使用 `npx.cmd`）；
- 安卓设备已连接，`adb devices` 可见；
- Python 依赖中包含 `mcp`（已在 `requirements.txt` 中，已兼容 1.x / 2.x 的客户端 API 差异）；
- 首次启动会自动下载 npm 包；默认固定为已验证的 `@mobilenext/mobile-mcp@1.0.15`，保证论文实验可复现。如需升级可改为 `@latest`，但应重新验证工具 schema。

```powershell
# 仅检查 mobile-mcp 连接与元素识别，不调用 LLM/Appium
.\.venv\Scripts\python -m tools.mobile_mcp_smoke --device emulator-5554
```

### 动作映射

| Agent 动作 | Appium 后端 | mobile-mcp 后端 |
| --- | --- | --- |
| 会话启动 | capability 中 `forceAppLaunch` 自动拉起被测 App | 连接后调用 `mobile_launch_app` 拉起被测 App |
| `click_by_id` | 定位元素 → 优先 ADB 坐标点击（兼容不响应无障碍点击的第三方控件），并自动跳过“跳过 N/Skip”开屏广告 | `mobile_list_elements_on_screen` 定位元素 → `mobile_click_on_screen_at_coordinates`（优先 ref，回退坐标） |
| `input_text` | `clear + send_keys` | 点击输入框聚焦 → `mobile_type_keys`（追加输入） |
| `clear_text` | `find_element(By.ID).clear()` | 聚焦输入框 → ADB `KEYCODE_MOVE_END + KEYCODE_DEL` 清空（mobile-mcp 1.0.x 无 clear 工具） |
| `restart_app` | `terminate_app + activate_app`，失败回退 ADB | `mobile_terminate_app + mobile_launch_app`，失败回退 ADB |
| `sleep` | 本地 `time.sleep`（0~30 秒校验） | 同左（与后端无关） |

> 多条测试用例会逐条规划，并在用例之间自动插入 `restart_app` 复位 App，
> 避免上一条用例的页面状态污染下一条。若 LLM 仍输出空字符串 `input_text`，
> 执行层会自动转换为 `clear_text`，因此两个后端都支持清空输入框。

### 配置项

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DEVICE_BACKEND` | `appium` | 默认执行后端 |
| `MOBILE_MCP_TRANSPORT` | `stdio` | `stdio` 或 `http` |
| `MOBILE_MCP_COMMAND` | Windows `npx.cmd` / 其他 `npx` | stdio 启动命令 |
| `MOBILE_MCP_ARGS` | `-y @mobilenext/mobile-mcp@1.0.15` | stdio 启动参数（已验证版本） |
| `MOBILE_MCP_URL` | `http://127.0.0.1:3000/mcp` | http 模式端点 |
| `MOBILE_MCP_DEVICE` | 空 | mobile-mcp 设备标识，空则用当前模拟器/真机序列号 |
| `MOBILE_MCP_STARTUP_TIMEOUT` | `90` | 首次启动等待秒数 |
| `MOBILE_MCP_CALL_TIMEOUT` | `60` | 单次工具调用超时秒数 |
| `MOBILE_MCP_CLOSE_TIMEOUT` | `10` | 关闭会话等待秒数 |
| `MOBILE_MCP_CLEANUP_DAEMON` | `true` | 关闭会话后停止宿主 mobilecli daemon，避免长期驻留 |
| `LLM_REQUEST_TIMEOUT` | `60` | 单次 LLM 请求超时秒数 |
| `LLM_MAX_RETRIES` | `2` | 网络/限流类错误最大重试次数（认证/参数错误不重试） |
| `LLM_RETRY_BACKOFF_SECONDS` | `1.5` | 重试初始等待秒数，按 2 倍指数退避 |
| `RUN_OUTPUT_DIR` | `outputs/runs` | 单次任务产物根目录（其下按 run_id 隔离） |

## 逐步 UI 驱动模式（动态页面 / 无 resource-id）

默认的静态动作规划只根据测试用例文本推断 `resource-id`。遇到多邻国、部分 Compose/Flutter 页面时，可点击容器往往没有 `resource-id`，文字又位于不可点击的子 `TextView`，静态规划会大量点空。逐步模式改为闭环执行：

```text
读取实时 UI 树 → 裁剪/标记可点击节点 → LLM 选择一步动作
→ Appium/ADB 执行 → 等待页面稳定 → 再次读取 UI 树 → ...
```

### 启用方式

```bash
# .env（仅 Appium 后端生效）
STEPWISE_AGENT_ENABLED=true
STEPWISE_MAX_STEPS=80
STEPWISE_MAX_SECONDS=900
STEPWISE_UI_MAX_NODES=80
STEPWISE_UI_MAX_TEXT_LENGTH=80
STEPWISE_ACTION_WAIT_SECONDS=1.0
STEPWISE_DONE_REQUIRE_EVIDENCE=true
```

也可在 `POST /run_test` 请求体中传 `"stepwise_agent": true`，仅对本次任务生效。`mobile_mcp` 后端暂不支持逐步模式，会自动回退到静态动作规划。

### 支持的动作

| 动作 | 参数 | 说明 |
| --- | --- | --- |
| `tap_by_ref` | `ref` | 点击当前 UI 树中编号为 ref 的节点中心，优先使用 |
| `tap_by_text` | `text` / `index` / `exact` | 按可见文本或 content-desc 点击，适合无 id 的“继续/检查”按钮 |
| `tap_by_coordinates` | `x` / `y` | 按屏幕坐标点击，作为 ref/文本都失效时的兜底 |
| `input_text` | `ref`（可选）/ `text` | 点击 ref 输入框后向焦点输入框输入文本 |
| `press_back` | 无 | 关闭弹窗/广告或返回 |
| `scroll` | `direction` | `down` / `up` |
| `sleep` | `seconds` | 等待页面加载（0~10 秒） |
| `done` | `success` / `evidence` | 结束并给出当前 UI 树中可见的达成/阻断证据 |

> `tap_by_coordinates` 默认要求坐标命中当前 UI 树中已解析的可点击节点；未命中时拒绝执行，避免误触未识别控件。确需放宽时设置 `STEPWISE_ALLOW_UNTARGETED_TAP=true`。

### 安全与边界

- 只保留被测应用包内的节点，其他应用/系统 UI 节点会被过滤；
- 密码字段文本强制显示为 `[密码字段已隐藏]`，执行层拒绝向密码字段输入内容；
- 购买、订阅、支付、分享、发送消息等敏感控件会标记 `danger=true`，执行层默认拒绝点击；
- `done success=true` 要求 `evidence` 真实出现在当前 UI 树中；课程类目标还要求包含“完成/连击/经验”等结算字样，防止把反馈页的“继续”误判为整节课完成；
- 报告新增“执行结论”区，明确区分“目标达成”与“仅未发现 Crash/ANR”。

> 安全提示：逐步模式会把**裁剪后的 UI 文本**发送给所配置的大模型，用于决策。涉密在研设备测试请保持 `STEPWISE_AGENT_ENABLED=false`，或仅在允许外发该页面文本的环境中使用。

## 桌面客户端（PySide6，纯 Qt 原生 GUI）

项目另提供 Windows 原生桌面客户端（desktop/ 目录，PySide6 实现，**不嵌入浏览器内核**），对接本地 FastAPI 后端：

- **三栏主窗口**：左侧任务管理（新建/删除/查看历史）｜中间彩色实时日志（INFO/DEBUG/WARN/ERROR 四级着色、自动滚动、导出 txt）｜右侧参数配置
- **参数配置**：LLM 选择（通义千问/DeepSeek）+ API 密钥（仅内存保存，不落地存文件）、执行后端（Appium/mobile-mcp）、真机/模拟器与设备 ID、被测应用包名/启动 Activity、RAG 知识库上传/向量库重建、开始/终止任务
- **底部状态**：任务进度条 + 状态文本（空闲/规划中/工具调用中/执行完成/失败），由后台状态监听线程轮询后端 `/api/logs` 增量日志解析 LangGraph 节点驱动
- **报告预览**：任务完成自动弹出 Markdown 测试报告预览（QTextDocument 原生渲染），支持导出 Markdown 到本地
- **异常弹窗**：网络失败 / Appium 连接失败 / mobile-mcp 连接失败 / 大模型调用异常分类提示
- **自定义图标**：QPainter 代码绘制"测试机器人"图标（desktop/core/icon_maker.py），窗口与 exe 图标均支持自定义

### 启动桌面客户端（开发环境）

```powershell
cd test_agent
.\.venv\Scripts\python -m desktop.main
```

### 桌面客户端打包为 exe

```powershell
cd test_agent
.\.venv\Scripts\python -m desktop.build_desktop
# 产物: dist/TestAgentDesktop/TestAgentDesktop.exe（无控制台窗口程序）
```

桌面客户端 exe 需与后端配合使用：目标机器需同时运行后端 `test_agent.exe`（或 `python main.py`），客户端默认连接 `http://127.0.0.1:8000`，可在"设置 → 后端地址"中修改。

### 桌面客户端相关后端接口（增量新增，不影响原有功能）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/logs?after_seq=N` | 增量拉取运行日志（内存环形缓冲，供实时展示） |
| POST | `/rebuild_kb` | 重建 RAG 向量库（重新入库 uploads 目录全部 txt） |

## 密钥使用说明

系统提供两种密钥配置方式，向后兼容：

1. **前端页面临时密钥（推荐演示使用）**：在 Web 首页"LLM API 密钥"区域填写通义千问或 DeepSeek 密钥，随 `/run_test` 请求提交；
   - 密钥仅在本次测试任务运行期间保存在内存中（注入 LangGraph 状态），任务结束即丢弃；
   - **绝不写入** .env、JSON、数据库或日志文件（日志中仅显示密钥前 4 位脱敏值）；
   - 页面输入框为 `type="password"` 且关闭 `autocomplete`，浏览器不自动保存，刷新页面即丢失；
2. **.env 配置密钥（备选）**：在 .env 中配置 `LLM_API_KEY` / `DEEPSEEK_API_KEY`，前端留空时自动回退使用，适合固定环境快速演示。

同理，页面上的模型选择、执行后端（Appium/mobile-mcp）、设备模式（模拟器/真机 + UDID）
以及被测应用包名 / 启动 Activity，也会对本次任务覆盖 .env 配置。这样切换被测 App
时不需要重启后端；未填写时仍回退 `APP_PACKAGE` / `APP_ACTIVITY`。

## 开发环境运行

1. 安装依赖并配置密钥：

```powershell
cd test_agent
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env   # 编辑 .env，填入 LLM_API_KEY（或 DEEPSEEK_API_KEY）
```

2. 预先启动安卓模拟器（或连接真机开启 USB 调试）。
   - 使用默认 Appium 后端：再启动 Appium 服务（独立终端）：

```powershell
appium --address 127.0.0.1 --port 4723
```

   - 使用 `mobile_mcp` 后端：无需启动 Appium，但需安装 Node.js 20+；程序会通过 `npx` 自动拉起 mobile-mcp。

3. 启动服务并访问：

```powershell
.\.venv\Scripts\python main.py
# 浏览器打开 http://127.0.0.1:8000
```

## exe 打包（Windows 独立分发）

### 论文配套文案

> 本系统支持使用 PyInstaller 打包为 Windows 平台独立可执行程序，打包后目标主机无需预先安装 Python 解释器，双击 exe 即可启动 Web 后端服务，浏览器访问内置前端页面进行测试任务操作，适合答辩现场演示。打包采用目录式分发，配置文件 .env 独立外置，方便修改大模型密钥与设备参数，不重新打包即可切换模型与真机/模拟器。

### 打包约束

- 打包产物为**文件夹**（单目录模式，onefile=False），稳定性高、启动快、降低杀毒误报，内含 `test_agent.exe`；
- 打包保留**控制台窗口**（console=True），运行日志直接可见，方便答辩排查错误；
- templates 网页模板通过 `--add-data` 随包分发，代码通过 `utils.resource_path` 的 `resource_path()` 自动定位（开发环境=项目目录，打包环境=`sys._MEIPASS` 解压目录）；
- **all-MiniLM-L6-v2 模型、chroma 向量库、outputs 输出目录不打包进 exe**：首次运行 exe 自动下载模型（约 90MB，需联网一次），模型缓存、向量库、测试报告均存放在 **exe 同级目录**；
- `.env` 不打包，需手动复制到 exe 同目录；
- 目标电脑**不需要安装 Python**，但仍需手动准备：安卓模拟器/真机（adb 可用）；使用默认 Appium 后端需安装并启动 Appium 服务，使用 `mobile_mcp` 后端需安装 Node.js 20+（无需 Appium）。

### 打包步骤（开发机执行）

```powershell
cd test_agent
# 安装 PyInstaller（一次性）
.\.venv\Scripts\pip install pyinstaller
# 执行打包（自动清理旧 build/dist 缓存，输出到 dist/test_agent）
.\.venv\Scripts\python build_exe.py
```

打包脚本自动完成：清理缓存 → 单目录打包（含 templates 与全部 hidden-import）→ 输出部署提示。

### 打包后部署使用说明（目标电脑）

1. 将 `dist/test_agent` 整个文件夹拷贝到目标电脑（**不需要安装 Python**）；
2. **手动把项目根目录的 `.env` 复制到 `dist/test_agent` 目录**（与 test_agent.exe 同级），
   在 .env 中修改 `LLM_API_KEY`/`DEEPSEEK_API_KEY`、`LLM_PROVIDER`、`IS_REAL_DEVICE`、`REAL_PHONE_UDID` 等配置，**改配置无需重新打包**；
3. 启动安卓模拟器（或连接真机开启 USB 调试）：
   - Appium 后端：启动 `appium --address 127.0.0.1 --port 4723`；
   - mobile-mcp 后端：安装 Node.js 20+ 即可，无需 Appium；
4. 双击 `test_agent.exe` 启动服务（保留控制台窗口查看日志）；
5. 浏览器打开 **http://127.0.0.1:8000**，即可上传知识库、输入需求、一键启动自动化测试；
6. 首次运行自动下载 all-MiniLM-L6-v2 模型到 exe 同级 `models_cache/` 目录（需联网一次）。

## 接口清单

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | Web 操作首页 |
| POST | `/upload_doc` | 上传 txt 知识文档入库（form-data，字段 file） |
| POST | `/run_test` | 启动 Agent 测试任务（JSON，字段 requirement，可选 device_backend 覆盖；响应含 run_id） |
| GET | `/health` | 健康检查 |
| GET | `/api/config` | 查询当前生效配置（provider/设备模式/UDID/执行后端） |
| GET | `/api/last_result` | 查询最近一次任务完整结果 |

## 常见问题

- **exe 启动闪退**：确认 `.env` 已复制到 exe 同目录；控制台日志可查看具体报错；
- **LLM 调用 401**：.env 中 API Key 未配置或仍是占位符；
- **Appium 连接失败**：先启动模拟器/真机，再启动 Appium 服务；
- **mobile-mcp 连接失败**：确认 Node.js 20+、`npx` 可用、设备已连接；首次运行需联网下载 npm 包；可先执行 `python -m tools.mobile_mcp_smoke` 单独排查；
- **首次运行卡在模型下载**：需联网，模型约 90MB，下载完成后存于 `models_cache/`；
- **切换模型/设备模式**：修改 exe 同目录 `.env` 后重启 exe 即可，无需重新打包。

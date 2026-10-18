# 面向安卓App的智能测试Agent

基于 **LangGraph + RAG 知识库 + Appium + ADB** 的安卓 App 自动化测试原型系统（本科毕业设计，对标小米安卓 App 测试场景）。

输入 App 测试需求，Agent 自动完成完整测试链路：

```
RAG检索测试知识库 → LLM生成测试用例 → 规划Appium执行动作 → Appium操作模拟器/真机App
→ adb抓取日志+截图 → LLM+关键字扫描分析缺陷（Crash/ANR/UI异常） → 输出Markdown缺陷测试报告
```

## 功能特性

- **Web 可视化操作页面**：纯原生 HTML+JS 单页（templates/index.html），支持知识库上传、配置展示、一键启动测试、结果展示，无需 Postman 调试接口
- **前端临时密钥**：页面可直接填写 qwen / DeepSeek API Key，密钥仅在本次任务内存中使用，不写入任何文件，刷新即失效；也可使用 .env 中的密钥作为备选，两种方式兼容
- **多模型一键切换**：`LLM_PROVIDER=qwen/deepseek`，均为 OpenAI 兼容接口，密钥外置于 .env
- **真机/模拟器一键切换**：`IS_REAL_DEVICE=false/true`，真机自动注入 UDID capability；页面也可对单次任务临时覆盖设备模式与 UDID
- **RAG 知识库**：txt 文档上传 → 文本切分 → Chroma 向量入库 → top3 相似度检索
- **执行失败自动重试**：LangGraph 条件分支，全部动作执行失败自动重新规划（最多 1 次）
- **缺陷识别增强**：LLM 分析 + logcat Crash/ANR 关键字预扫描双重保障，降低幻觉
- **Markdown 测试报告**：时间戳命名，自动写入测试用例、动作执行结果、缺陷列表、日志与截图路径

## 技术栈

Python 3.10+，FastAPI，LangGraph，LangChain，Chroma，sentence-transformers（all-MiniLM-L6-v2），Appium-Python-Client（UiAutomator2），pydantic v2，uvicorn，Jinja2。

## 目录结构

```
test_agent/
├── config/settings.py        # 全局配置（pydantic BaseSettings，读取 .env）
├── templates/index.html      # Web 操作首页（纯原生 HTML+JS）
├── tools/                    # appium_tool / adb_tool / report_tool
├── rag/                      # document_loader / vector_store
├── agent/                    # state / nodes（6节点）/ graph（含重试分支）
├── api/server.py             # FastAPI 接口 + 页面路由
├── utils/                    # logger / resource_path（打包资源路径适配）
├── build_exe.py              # PyInstaller 打包脚本
├── main.py                   # 项目入口
├── .env / .env.example       # 密钥与设备配置（.env 不入库）
└── requirements.txt
```

## 桌面客户端（PySide6，纯 Qt 原生 GUI）

项目另提供 Windows 原生桌面客户端（desktop/ 目录，PySide6 实现，**不嵌入浏览器内核**），对接本地 FastAPI 后端：

- **三栏主窗口**：左侧任务管理（新建/删除/查看历史）｜中间彩色实时日志（INFO/DEBUG/WARN/ERROR 四级着色、自动滚动、导出 txt）｜右侧参数配置
- **参数配置**：LLM 选择（通义千问/DeepSeek）+ API 密钥（仅内存保存，不落地存文件）、真机/模拟器与设备 ID、RAG 知识库上传/向量库重建、开始/终止任务
- **底部状态**：任务进度条 + 状态文本（空闲/规划中/工具调用中/执行完成/失败），由后台状态监听线程轮询后端 `/api/logs` 增量日志解析 LangGraph 节点驱动
- **报告预览**：任务完成自动弹出 Markdown 测试报告预览（QTextDocument 原生渲染），支持导出 Markdown 到本地
- **异常弹窗**：网络失败 / Appium 连接失败 / 大模型调用异常分类提示
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

同理，页面上的模型选择与设备模式（模拟器/真机 + UDID）也会对本次任务覆盖 .env 配置。

## 开发环境运行

1. 安装依赖并配置密钥：

```powershell
cd test_agent
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env   # 编辑 .env，填入 LLM_API_KEY（或 DEEPSEEK_API_KEY）
```

2. 预先启动安卓模拟器（或连接真机开启 USB 调试），再启动 Appium 服务（独立终端）：

```powershell
appium --address 127.0.0.1 --port 4723
```

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
- 目标电脑**不需要安装 Python**，但仍需手动准备：Appium 服务、安卓模拟器/真机（adb 可用）。

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
3. 启动安卓模拟器（或连接真机开启 USB 调试），并启动 Appium 服务：
   `appium --address 127.0.0.1 --port 4723`；
4. 双击 `test_agent.exe` 启动服务（保留控制台窗口查看日志）；
5. 浏览器打开 **http://127.0.0.1:8000**，即可上传知识库、输入需求、一键启动自动化测试；
6. 首次运行自动下载 all-MiniLM-L6-v2 模型到 exe 同级 `models_cache/` 目录（需联网一次）。

## 接口清单

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | Web 操作首页 |
| POST | `/upload_doc` | 上传 txt 知识文档入库（form-data，字段 file） |
| POST | `/run_test` | 启动 Agent 测试任务（JSON，字段 requirement） |
| GET | `/health` | 健康检查 |
| GET | `/api/config` | 查询当前生效配置（provider/设备模式/UDID） |
| GET | `/api/last_result` | 查询最近一次任务完整结果 |

## 常见问题

- **exe 启动闪退**：确认 `.env` 已复制到 exe 同目录；控制台日志可查看具体报错；
- **LLM 调用 401**：.env 中 API Key 未配置或仍是占位符；
- **Appium 连接失败**：先启动模拟器/真机，再启动 Appium 服务；
- **首次运行卡在模型下载**：需联网，模型约 90MB，下载完成后存于 `models_cache/`；
- **切换模型/设备模式**：修改 exe 同目录 `.env` 后重启 exe 即可，无需重新打包。

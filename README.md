# FracMan MCP（wmw-fracman-mcp）

让 AI 直接连接并操作本机 **FracMan 8.00 Beta** 的本地 MCP 服务器。FracMan 没有公开
API、COM 接口或 Python 模块，因此本服务器采用与 Abaqus MCP 类似的“控制面 + 多通道”
思路：

| 通道 | 用途 | 机制 |
| --- | --- | --- |
| 实时 GUI 桥 | 改动用户正在看的工程 | 按 PID/HWND 附着窗口，UIA 运行宏、读状态栏/日志、截图 |
| 隐藏批处理 | 大批量、可重复的生成任务 | 独立 `FracMan.exe` 进程，`LOGTOFILE` + `EXITONERROR`，以 `.fml` 判错 |
| 文件智能 | 读取结果与宏日志 | 解析 `.fab/.ffb/.f2d/.sab/.fml/.fmf/.frd` |

服务器仅使用本地 stdio，不监听端口，无 TLS/认证。

## 架构

```
MCP client (Qoder / any stdio client)
        │  JSON-RPC over stdio
        ▼
mcp_server.py  ── FastMCP tools + resources
        │
        ├── sessions.py ── session registry, single-flight request ids
        │        └── gui.py ── UIA/Win32: status bar, log pane, dialogs,
        │                      file dialog, screenshots, project launch/close
        ├── runner.py ── hidden batch processes, timeout/cancel, artifact scan
        ├── macro.py ── validation, LOGTOFILE/EXITONERROR wrapping, DFN template
        └── parsers/ ── FAB FFB F2D SAB FML FMF FRD
```

关键事实（实测）：

- 批处理 FracMan 的退出码**始终为 0**，唯一可靠判错来源是 `.fml` 中的
  `#Macro Error:` 块。
- UIA 日志面板最多保留 4096 字符，读取增量需要做重叠匹配。
- 首次启动会出现 “beta test build” 模态提示；服务器只自动点“确定”。
- 文件对话框路径必须用反斜杠；宏内路径必须用正斜杠。

## 环境要求

- Windows 10/11，已安装 FracMan 8.0（默认 `D:\software\fracman\FracMan.exe`）
- Python 3.10+，依赖：`mcp`、`pywinauto`、`pywin32`、`psutil`、`Pillow`
- 推荐使用 ASCII 工作区（默认 `%LOCALAPPDATA%\FracManMCP`）

```powershell
py -3 -m pip install mcp pywinauto pywin32 psutil Pillow
py -3 mcp_server.py          # 直接以 stdio 启动
```

## 注册到 Qoder

编辑 `%USERPROFILE%\.qoder\mcp.json`，在 `mcpServers` 中加入（示例见
`examples/mcp_config.example.json`）：

```json
{
  "mcpServers": {
    "fracman-mcp-server": {
      "command": "C:\\Users\\Dell\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
      "args": [
        "d:\\智能体-260424\\qoder\\CAE-Agent-Hub\\MCP\\FracMan\\mcp_server.py"
      ],
      "cwd": "d:\\智能体-260424\\qoder\\CAE-Agent-Hub\\MCP\\FracMan",
      "env": {
        "FRACMAN_EXE": "D:\\software\\fracman\\FracMan.exe",
        "FRACMAN_MCP_WORKSPACE": "C:\\Users\\Dell\\AppData\\Local\\FracManMCP",
        "FRACMAN_MCP_MAX_BATCH": "2"
      }
    }
  }
}
```

重载 MCP 后应先调用 `discover_sessions`，确认能看到目标 FracMan 窗口。

## 工具

**配置与会话**

| 工具 | 说明 |
| --- | --- |
| `get_config` | 版本、可执行文件、工作区、并发/超时限制 |
| `set_workspace` | 修改工作区（宏、截图、工程副本） |
| `discover_sessions` | 枚举 FracMan 窗口，读取状态栏与日志长度 |
| `list_sessions` | 列出缓存的会话 |
| `set_active_session` | 选择默认实时会话 |
| `get_session_info` | 状态、工程名、对话框、窗口/视图矩形、日志尾部 |
| `ping` | 读取状态栏验证桥接可用 |

**实时执行（单飞）**

| 工具 | 说明 |
| --- | --- |
| `run_macro` | 在用户会话中执行宏；返回日志增量、错误、对话框 |
| `get_request_status` | 查询 `request_id`，可等待完成 |

**批处理**

| 工具 | 说明 |
| --- | --- |
| `submit_macro` | 启动隐藏 FracMan 进程；自动加 `LOGTOFILE`/`EXITONERROR`/`EXIT` |
| `monitor_run` | 状态、产物、错误，可等待 |
| `cancel_run` | 只终止 MCP 自己启动的批次进程 |
| `get_run_log` | 宏日志、stdout/stderr、解析后的错误 |
| `list_runs` | 最近的批次，新的在前 |

**文件与视图**

| 工具 | 说明 |
| --- | --- |
| `inspect_file` | 解析 FracMan 结果/宏文件 |
| `list_files` | 只读目录浏览 |
| `capture_window` | 整窗或 3D 视图 PNG，返回尺寸与裁剪信息 |
| `get_viewport_image` | 直接返回 PNG data URI，便于多模态查看 |
| `get_macro_reference` | 已实测宏命令参考 |

**工程生命周期**

| 工具 | 说明 |
| --- | --- |
| `open_project` | 在 MCP 自有窗口中打开工程；默认先复制到工作区 |
| `close_session` | 关闭 MCP 自有窗口；“保存？”自动答“否” |

**资源**：`fracman://sessions`、`fracman://runs`、`fracman://macro-reference`、
`fracman://agent-instructions`。

## 安全边界

- 绝不关闭/终止用户自有 FracMan 会话（如 PID 48136）。`close_session` 仅对
  `owned` 会话生效，否则需要显式 `force=true`。
- 实时宏默认拒绝 `EXIT` 与 `DeleteAll`，除非调用方传
  `allow_exit=true` / `allow_destructive=true`。
- 实时宏禁止自带 `LOGTOFILE`/`EXITONERROR`；服务器负责日志包装。
- 批次只终止自己记录的 PID；`run_id` 是幂等键，超时返回 `timeout_pending`，
  **绝不自动重放**。
- 测试工程一律使用副本；`D:\BaiduNetdiskDownload\Fracman` 培训资料只读。
- 写文件（截图等）限制在工作区内。

## 文件解析

| 后缀 | 内容 |
| --- | --- |
| `.fab` / `.ffb` | 裂缝数、集、属性、面积/半径统计、包围盒、平均极点、前 N 条预览 |
| `.f2d` | 迹线条数、段数、总长度、长度统计、包围盒、段/迹预览 |
| `.sab` | 井型、点列、MD、井长、半径统计、包围盒 |
| `.fml` | `#Macro Error:` 块、宏文件/行号、宏栈、已执行块、日志尾部 |
| `.fmf` | 命令列表、行数、预览 |
| `.frd` | UTF-16 版本横幅与 ASCII 对象名（启发式，不解码数值） |

## 工作区

```
%LOCALAPPDATA%\FracManMCP\
  runs\         live_* / batch_* 每次运行的宏、日志、stdout/stderr
  projects\     open_project(copy=True) 的工程副本
  screenshots\  截图
  exports\      默认导出位置
```

## 验收记录（2026-10-31 实测）

| 项目 | 结果 |
| --- | --- |
| 会话发现 | 识别 PID 48136，状态“准备好” |
| 实时宏成功 | `SetSeed 424242` 完成，返回日志增量 |
| 实时宏错误 | `Units = "bogusunit"` → `macro_error`，自动关闭“确定”框，`ping` 恢复 |
| 批次 DFN | 60 条裂缝 8.58 s，产物 23,959 B |
| 批次并发 | 2 实例 13.35 s 成功；第 3 个被 `BatchBusy (2/2)` 拒绝 |
| 批次错误 | 6.79 s 结束，`status=failed`，`.fml` 报 line 12 |
| 取消/超时 | `cancel_run` 2 s 内终止；`timeout=4` 返回 `timeout` |
| 解析器 | 200 裂缝 FAB（57,841.9 m²）、158 裂缝 FFB、12 段 F2D（16,537.854）、386 点 SAB（8,942.8） |
| 工程生命周期 | Exercise 12e 副本打开 → 截图 → 关闭（“保存？”答“否”）；PID 48136 不受影响 |
| 截图 | 整窗 2880×1523；3D 视图裁剪 2198×1224 |
| 单元测试 | `38 passed` |

## 已知限制

- 实时宏一次调用约 15–19 s，主要耗时为 UIA 控件树扫描与日志读取。
- UIA 日志面板上限 4096 字符；服务器用重叠匹配还原增量，但更早的历史不可见。
- `.frd` 是未公开的二进制容器，只提供横幅/对象名启发式信息；数值请通过宏导出或在
  GUI 中查看。
- 错误行号来自 FracMan 实际执行的包装后宏，可能比用户原文多 5 行左右。
- 不提供 `generate_dfn` / `run_welltest` 之类高层封装；Phase 1 交付宏模板与底层工具。
- 仅支持 Windows GUI 会话；无远程 HTTP 模式。

## 目录

```
MCP/FracMan/
  mcp_server.py
  fracman_mcp/
    config.py models.py requests.py sessions.py gui.py macro.py runner.py
    parsers/ (common fab f2d sab fml frd)
    resources/ (agent_instructions.md macro_reference.md templates/dfn_generate.fmf)
  tests/
  examples/mcp_config.example.json
  docs/superpowers/specs/2026-10-31-fracman-mcp-design.md
  README.md  pyproject.toml
```

## 测试

```powershell
py -3 -m pytest tests -q
```

单元测试不启动 FracMan；训练样例存在时会额外做只读解析回归。

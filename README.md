# FracMan MCP（wmw-fracman-mcp）

让 AI 直接连接并操作本机 **FracMan 8.00 Beta** 的本地 MCP 服务器。FracMan 没有公开
API、COM 接口或 Python 模块，因此本服务器采用与 Abaqus MCP 类似的“控制面 + 多通道”
思路：

| 通道 | 用途 | 机制 |
| --- | --- | --- |
| 实时 GUI 桥 | 改动用户正在看的工程 | 按 PID/HWND 附着窗口，UIA 运行宏、读状态栏/日志、截图 |
| 隐藏批处理 | 大批量、可重复的生成任务 | 独立 `FracMan.exe` 进程，`LOGTOFILE` + `EXITONERROR`，以 `.fml` 判错 |
| 文件智能 | 读取结果与宏日志 | 解析 `.fab/.ffb/.f2d/.sab/.fml/.fmf/.frd` |
| dfnWorks（WSL） | 开源 DFN 生成、共形网格、连通性图、与 FracMan 对比 | 生成作业规格 → 写自带驱动 → `wsl.exe` 调 `/opt/dfnworks-venv` → 读 `result.json` |

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
        ├── dfnworks/ ── spec.py (校验) driver_template.py (Linux 驱动)
        │                 runner.py (WSL 作业) compare.py (FracMan 对比) paths.py
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

**dfnWorks 通道（可选）** 另外需要：

- WSL2 Ubuntu，装好 dfnWorks 2.12+ 与其 venv（默认 `/opt/dfnworks-venv`）
- `~/.dfnworksrc` 设置 `dfnworks_PATH` 与 `LAGRIT_EXE`
- PFLOTRAN/FEHM 可以不装：本服务器不暴露流动求解工具

自检用 `dfnworks_check`；冷启动导入 `pydfnworks` 约需 1 分钟，默认超时 300 s。

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

## 配套 Skill

仓库内的 [`skills/fracman-skill`](skills/fracman-skill/SKILL.md) 是配合本 MCP 使用的
操作技能，整理了实测的宏调试经验、DFN 配方、GUI 自动化细节与排错决策树。把整个
`skills/fracman-skill/` 目录复制到 `%USERPROFILE%\.qoder\skills\`（或项目的
`.qoder/skills/`）后，重启会话即可通过 `/fracman-skill` 调用。

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

**dfnWorks（WSL，生成/网格/连通性/对比）**

| 工具 | 说明 |
| --- | --- |
| `dfnworks_check` | 自检 WSL、`pydfnworks`、仓库、LaGriT、DFNGen |
| `dfnworks_generate` | 生成 DFN；返回裂缝数、P32、面积/半径/极点、交点、产物 |
| `dfnworks_mesh` | 对已有作业做 LaGriT 共形网格；`full_mesh.inp` + 节点/三角形统计 |
| `dfnworks_graph` | 裂缝/交线/二部图，s-t 最短路与 k 条骨干路径 |
| `dfnworks_compare` | `.fab/.ffb` 与 dfnWorks 统计对比：计数、面积、P32、包围盒 IoU、平均极点夹角 |
| `dfnworks_job_status` | 状态、产物、错误、日志尾部，可等待 |
| `dfnworks_job_cancel` | 只终止本作业的 WSL 驱动进程 |
| `dfnworks_job_log` | stdout/stderr 与 dfnWorks 自身 `run.log` |
| `dfnworks_list_jobs` | 最近的 dfnWorks 作业 |

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

**资源**：`fracman://sessions`、`fracman://runs`、`fracman://dfnworks-jobs`、
`fracman://dfnworks-guide`、`fracman://macro-reference`、`fracman://agent-instructions`。

## dfnWorks 通道

FracMan 侧负责建模、宏与结果解读；dfnWorks 侧补上开源、可复现的 DFN 生成、
LaGriT 共形网格与连通性分析。两者同在一个 stdio 服务器中，`discover_sessions`、
`inspect_file`、`dfnworks_*` 可混用。

```
gen = dfnworks_generate(domain=[100,100,100], seed=20261101)
dfnworks_mesh(gen["job_id"])                     # LaGriT 共形网格
dfnworks_graph(gen["job_id"], k=10)              # 连通图 + k 条主干
dfnworks_compare(r"C:\path\model.fab", job_id=gen["job_id"])
```

`generate` / `mesh` / `graph` 三个阶段都用同一 seed 重建同一 DFN，`job_id` 是幂等键。
默认几何按域尺寸 `L` 缩放：`h = L/50`、裂缝半径 `= L/5`、三组斜交 constant family、
`n_poly = 100`、网格 `min_dist = h/4`、`max_dist = 5h`。该组合在 20 m / 100 m / 1 km
立方域上均实测得到 25 条裂缝、左右边界连通（网格 18,873 节点 / 35,214 三角形，
耗时约 10 s）。

调参时的两条硬约束：

- `h` 相对裂缝过粗会被 DFNGen 的 FRAM 判掉交点（`h/半径 ≥ 0.2` 时直接报
  “no intersecting fractures”），生成阶段失败；
- 矩形 family 的 4 点轮廓最短边必须 ≥ `3h`，否则 `dfnworks_generate` 在提交前
  就以 `3h meshing limit` 拒绝。

本通道只覆盖几何、网格与连通性；PFLOTRAN/FEHM 流动与 dfnTrans 输运不在范围内，
需要时应在 WSL 中直接使用 dfnWorks 原始工作流。参数、产物与排错细节见
[`fracman_mcp/resources/dfnworks_guide.md`](fracman_mcp/resources/dfnworks_guide.md)。


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
  dfnworks\
    jobs\<job_id>\  spec.json、driver.py、run\（DFNGen/LaGriT 工作目录）、
                    result.json、dfn_stats.json、fractures.json/.csv、
                    mesh_stats.json、graph.json、graph_metrics.json、backbone.json
```

dfnWorks 作业目录必须在带盘符的本地路径下（WSL 通过 `/mnt/<盘符>` 访问，UNC 不支持）。

## 验收记录（2026-10-31 实测）

| 项目 | 结果 |
| --- | --- |
| 会话发现 | 识别 PID 48136，状态“准备好” |
| 实时宏成功 | `SetSeed 424242` 完成，返回日志增量 |
| 实时静默宏 | `SetCurrentUnits Length m` 6.3 s 完成；`.fml` 末尾出现 `RunMacro` 哨兵 |
| 实时宏错误 | `Units = "bogusunit"` → `macro_error`，自动关闭“确定”框，`ping` 恢复 |
| 批次 DFN | 60 条裂缝 8.58 s，产物 23,959 B |
| 批次并发 | 2 实例 13.35 s 成功；第 3 个被 `BatchBusy (2/2)` 拒绝 |
| 批次错误 | 6.79 s 结束，`status=failed`，`.fml` 报 line 12 |
| 取消/超时 | `cancel_run` 2 s 内终止；`timeout=4` 返回 `timeout` |
| 解析器 | 200 裂缝 FAB（57,841.9 m²）、158 裂缝 FFB、12 段 F2D（16,537.854）、386 点 SAB（8,942.8） |
| 工程生命周期 | Exercise 12e 副本打开 → 截图 → 关闭（“保存？”答“否”）；PID 48136 不受影响 |
| 实时 DFN | 1 km×1 km×500 m、80 条裂缝；FAB 32,295 B，面积均值 29,697.96 m² |
| 截图 | 屏幕抓取整窗 2880×1523；3D 视图裁剪 1892×1224；`capture_method=screen` |
| dfnWorks 默认参数 | 20 m 立方域：25 条裂缝、25 个交点、P32 0.158、family 5/9/11 |
| dfnWorks 网格 | LaGriT 共形网格 10.4 s：18,873 节点、35,214 三角形、25 种材料 |
| dfnWorks 连通性 | 27 节点、37 边、单连通分量、s-t 连通；k=5 主干 11 节点 14 边 |
| dfnWorks 对比 | ESF1.fab（另一套 600 m 量级数据）→ 计数/面积/IoU/极点 4 条告警，判定合理 |
| 单元测试 | `69 passed` |

## 已知限制

- 实时宏一次调用约 6–19 s，主要耗时为 UIA 控件树扫描、日志轮询与对话框处理。
- UIA 日志面板上限 4096 字符；服务器用重叠匹配还原增量，但更早的历史不可见。
- `.frd` 是未公开的二进制容器，只提供横幅/对象名启发式信息；数值请通过宏导出或在
  GUI 中查看。
- 错误行号来自 FracMan 实际执行的包装后宏，可能比用户原文多 5 行左右。
- 截图会先把 FracMan 窗口激活到前台，再抓取物理像素；窗口被其他窗口遮挡时无法
  避免（`PrintWindow` 对 GPU 视图会返回旧帧，仅作后备）。
- 实时宏完成时会自动关闭信息类对话框，文本记入 `warnings`；只有含
  error/invalid/failed 等字样的对话框才判失败。
- 完成判定依赖包装宏末尾的 `RunMacro` 空哨兵；`STOP` 会在哨兵前终止宏，因此实时宏
  拒绝 `STOP`（批处理不受影响）。
- 不提供 `generate_dfn` / `run_welltest` 之类高层封装；Phase 1 交付宏模板与底层工具。
- 仅支持 Windows GUI 会话；无远程 HTTP 模式。
- dfnWorks 通道依赖 WSL2 与独立 venv，冷启动导入 `pydfnworks` 约 1 分钟；默认并发 1 个
  作业，提交时按 `wait_seconds` 等待，超时只返回 `timeout`，不自动重放。
- dfnWorks 的 `h`、裂缝半径、`n_poly` 相互耦合：`h` 过粗会丢交点，半径过小或
  数量不足则主簇不贯穿边界；MCP 只拦截能静态判定的 `3h` 规则，其余需看
  `result.json` 的 `errors`。
- 每次 `mesh` / `graph` 都会按 seed 重建 DFN，因此三者耗时相近的只有生成部分；
  网格阶段仍是主要开销。
- `dfnworks_compare` 不推断 FracMan set 与 dfnWorks family 的对应关系，只并列统计；
  域尺寸不一致时 P32/IoU 的差异属于输入差异，不是错误。

## 目录

```
MCP/FracMan/
  mcp_server.py
  fracman_mcp/
    config.py models.py requests.py sessions.py gui.py macro.py runner.py
    dfnworks/ (spec paths runner driver_template compare)
    parsers/ (common fab f2d sab fml frd)
    resources/ (agent_instructions.md macro_reference.md dfnworks_guide.md
                templates/dfn_generate.fmf)
  tests/ (test_*.py, test_dfnworks.py)
  skills/fracman-skill/
    SKILL.md
    references/ (macro-cookbook.md gui-automation.md troubleshooting.md)
  examples/mcp_config.example.json
  docs/superpowers/specs/2026-10-31-fracman-mcp-design.md
  README.md  pyproject.toml
```

## 测试

```powershell
py -3 -m pytest tests -q
```

单元测试不启动 FracMan；训练样例存在时会额外做只读解析回归。

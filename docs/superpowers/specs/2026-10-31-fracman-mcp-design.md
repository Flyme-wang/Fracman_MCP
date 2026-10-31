# FracMan MCP 设计（混合双通道）

- 日期：2026-10-31
- 状态：已实现并验收（2026-10-31）
- 目标：让 AI 通过 MCP 连接本机 FracMan 8.00，完成会话发现、宏执行、批处理、日志/错误读取、结果文件检查与窗口截图。

## 1. 背景与能力边界

FracMan 8.00 没有官方 Python/COM/命令行 API。可用的自动化接口只有三层：

1. 宏语言（`.fmf`，`BEGIN/END` 块）；
2. 运行中的 Windows GUI（UIA 自动化）；
3. 结果文件（`.fab`、`.ffb`、`.f2d`、`.sab`、`.frd`、`.fml`）。

因此本 MCP 不提供 `run_python`；执行层是宏，检查层是文件解析。这是相对 Abaqus MCP 的唯一硬性降级。

## 2. 已实测证据

- 批处理：`FracMan.exe macro.fmf` 隐藏启动；7.06 s 生成 200 条裂缝并导出 81 KB `.fab`。
- 并发：两个批处理实例同时运行，12.04 s 完成，输出互不干扰；许可至少支持 2 并发。
- 实时 GUI：UIA 可点击“运行 宏。”、填写文件对话框、读取日志面板、状态栏、PrintWindow 截图。
- 文件对话框：是主窗口子级 `#32770`；文件名编辑框 auto_id `1148`；打开按钮 auto_id `1`；路径必须使用反斜杠，正斜杠会报“文件名无效。”。
- 运行时错误：弹出 `#32770`，标题 `FracMan`，错误文本在 Static auto_id `65535`；点“确定”后状态栏回到“准备好”。
- 批处理错误：`.fml` 写入 `#Macro Error: ...`；加 `BEGIN EXITONERROR END` 可让进程退出；**退出码仍为 0**，不能作为成功依据。
- 项目打开：`FracMan.exe <project.frd>` 可打开工程；首个 Beta 提示框为 `#32770`、文本含
  `beta test build`，服务器自动点“确定”后主窗口约 3 s 内出现。

## 3. 架构

### 3.1 实时 GUI 通道

- 按 PID 附着 FracMan 主窗口，不依赖端口。
- `run_macro` 将用户宏包装为带 `LOGTOFILE` 的临时 `.fmf`，通过“运行 宏...”执行；轮询日志面板、状态栏与模态框。
- 完成判据：包装宏末尾追加 `RunMacro` 空哨兵（`.fml` 会记录 `#Running Macro:` 与 `RunMacro` 块）；
  轮询 `.fml` 直到哨兵路径出现。`DrawingOn/DrawingOff` 只在回放时生效、不写入日志，不能用作哨兵。
  实时宏因此拒绝 `STOP`（它会在哨兵前终止宏）；出错时 `.fml` 的 `#Macro Error` 立即判失败。
- 检测到错误框时保存文本并点“确定”；超时返回 `timeout_pending`，由后台 watcher 继续完成，绝不自动重放。
- 支持打开工程副本（新进程）、截图、关闭 MCP 自建会话。

### 3.2 批处理通道

- `FracMan.exe <macro>` 独立进程，宏头注入 `LOGTOFILE` + `EXITONERROR`，结尾补 `EXIT`。
- 监视进程与 `.fml`；`#Macro Error` 或超时即判失败；只终止自己启动的 PID。
- 默认并发 2；达到上限时返回 `BatchBusy` 并附当前活动运行，不排队；`monitor_run` 查询进度，`cancel_run` 取消。
- 成功判据：进程退出 + `.fml` 无 `#Macro Error` + 指定产物存在（若提供）。

### 3.3 文件解析通道

- `.fab`/`.ffb`：统计裂缝数、集合、属性名、顶点、法向、包围盒、面积/半径分布与平均极点。
- `.f2d`：迹线表，统计迹线数、段数、总长、包围盒。
- `.sab`：井轨迹，兼容 `BEGIN Well` 与 `BEGIN curved_borehole` 两种写法。
- `.fml`：已执行块、`#Macro Error` 块、宏文件/行号、宏栈、日志尾部。
- `.frd`：二进制，仅启发式提取版本横幅与对象名，结果以 `note` 标注“不解码数值”。
- `inspect_file` 按扩展名分派，返回统一结构；无法识别时返回文件大小、修改时间与文本/二进制标记。

### 3.4 统一模型

- Session：`fracman_<pid>`，记录窗口、工程名、owned、状态。
- Request：实时宏的 `request_id`，单飞、指纹防冲突、超时不重放。
- Run：批处理 `run_id`，记录宏、日志、PID、状态、错误、产物。
- 所有写操作限制在工作区；只读检查可访问任意可读路径。

## 4. 工具集（Phase 1）

配置：`get_config`、`set_workspace`。

会话：`discover_sessions`、`list_sessions`、`set_active_session`、`get_session_info`、`open_project`、`close_session`、`ping`。

执行：`run_macro`、`submit_macro`、`monitor_run`、`cancel_run`、`get_run_log`、`list_runs`、`get_request_status`。

检查与视图：`inspect_file`、`list_files`、`capture_window`、`get_viewport_image`、`get_macro_reference`。

资源：`fracman://sessions`、`fracman://runs`、`fracman://macro-reference`、`fracman://agent-instructions`。

## 5. 错误与超时语义

- 实时宏：BUSY 返回当前 `active_request_id`；超时返回 `timeout_pending`；未知结果不视为未执行；不自动重放。
  成功判据为 `.fml` 无 `#Macro Error` 且日志非空；自动关闭的信息框记入 `warnings`，
  只有对话框文本含 error/invalid/failed 等失败语义时才判失败。
- 批处理：`EXITONERROR` 保证错误时不挂死；`.fml` 是唯一可靠判错来源；退出码忽略。
- 模态框：任务对话框读 `ContentText`；经典消息框读 Static `65535`；默认只点“确定”；关闭时
  `save=false` 点“否”，仅当调用方显式 `save=true` 才点“是”。
- 文件对话框路径统一转反斜杠；宏内路径统一正斜杠。

## 6. 安全边界

- 不关闭、不终止用户自有 FracMan 会话，除非显式 `force=true`。
- 实时宏默认拒绝 `EXIT` 与 `DeleteAll`，除非调用方显式 `allow_exit=true` / `allow_destructive=true`。
- 只终止 MCP 自己启动的批处理 PID。
- 测试工程一律用副本；培训资料只读。
- 工作区默认 `%LOCALAPPDATA%\FracManMCP`，ASCII 路径。
- MCP 为本地 stdio，无网络监听、无 TLS/认证。

## 7. 目录

```
MCP/FracMan/
  mcp_server.py
  fracman_mcp/
    config.py models.py requests.py sessions.py gui.py macro.py runner.py
    parsers/{fab,ffb,f2d,sab,fml,frd,common}.py
    resources/{agent_instructions.md,macro_reference.md,templates/*.fmf}
  tests/
  examples/mcp_config.example.json
  README.md
  pyproject.toml
```

## 8. 验收结果（2026-10-31）

1. `discover_sessions` 识别 PID 48136，状态“准备好”。
2. 实时宏 `SetSeed 424242` 完成并返回日志增量；错误宏 `Units = "bogusunit"` 返回
   `macro_error`、FracMan 错误文本，自动关闭“确定”框，随后 `ping` 恢复 `ready`。
3. 批处理 60 裂缝 8.58 s、产物 23,959 B；40×2 并发 13.35 s 成功；第 3 个返回
   `BatchBusy (2/2)`；错误宏 6.79 s 判失败；`cancel_run` 2 s 内终止；`timeout=4`
   返回 `timeout`。
4. `inspect_file`：200 裂缝 FAB（5 属性、57,841.9 m²）、158 裂缝 FFB（3 属性）、
   F2D 12 段（16,537.854）、SAB 386 点（8,942.8）；FRD 返回横幅与启发式对象名。
5. `capture_window` 整窗 2880×1523；`mode=view` 裁剪 1892×1224，`view_cropped=true`，
   `capture_method=screen`（GPU 视图改用屏幕物理像素抓取）。
6. 打开 Exercise 12e 副本（88,917 B）→ 截图 → `close_session(save=False)` 自动答“否”；
   PID 48136 始终存活，MCP 自有进程全部退出。
7. 培训资料未修改；仅读取样例做解析回归。
8. `py -3 -m pytest tests -q` → 43 passed；stdio 客户端验证 21 个工具、4 个资源。
9. 实时 DFN：1 km×1 km×500 m、80 条裂缝（FISHER trend 15°/plunge 85°/κ=8，
   UNIFORM 长度 100±40 m）；FAB 32,295 B，面积均值 29,697.96 m²，总 2,375,836.46 m²；
   3D 视图关闭区域实体后显示全部裂缝。

## 9. 非目标

- 不实现 FracMan 无 API 的模型对象级读写；只能通过宏。
- 不保证 `.frd` 完整语义解析。
- 不在 Phase 1 提供 `generate_dfn`/`run_welltest` 高层封装，先以宏模板与底层工具交付。
- 不实现 dfnWorks/Abaqus 联动。

## 10. 已知限制

- 实时宏单次调用约 6–19 s，主要耗时为 UIA 控件扫描、日志轮询与对话框处理。
- UIA 日志面板上限 4096 字符，增量用重叠匹配还原；更早历史不可见。
- 错误行号对应服务器包装后的宏，比用户原文多约 5 行。
- 截图会激活 FracMan 到前台并抓取屏幕物理像素；窗口被遮挡时无法避免。
  `PrintWindow` 对 GPU 视图返回旧帧，仅作后备。
- 实时宏完成时自动关闭的信息框记入 `warnings`；仅含 error/invalid/failed
  等字样或 `.fml` 有 `#Macro Error` 时判失败。
- 仅 Windows + 本机 GUI；无 HTTP/远程模式。

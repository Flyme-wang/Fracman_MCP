---
name: fracman-skill
description: 通过 FracMan MCP 操作本机 FracMan 8.0 与 WSL 内 dfnWorks 的实战技能，覆盖会话发现与附着、实时/批处理宏、区域与随机裂缝 DFN 建模、FAB/FFB/F2D 导出解析、3D 截图、dfnWorks DFN 生成与 LaGriT 共形网格/连通性/对比，以及静默宏超时、信息框误判、截图旧帧、单位污染、区域遮挡、会话锁与超时等问题的排查。Use when working with FracMan, dfnWorks, DFN, fracture sets, .fab/.fml macros, LaGriT meshing, or the fracman MCP server.
---

# FracMan Skill

配合 `fracman_mcp_server`（工具前缀 `mcp__fracman_mcp_server__`）使用。本技能记录在
FracMan 8.00 Beta（2021.02.05，64 bit）上实测过的操作路径与踩坑结论；命令语法基线
以 MCP 资源 `fracman://agent-instructions`、`fracman://macro-reference` 为准。

## 何时使用

- 让 AI 直接操作本机正在运行的 FracMan：改模型、建 DFN、导出、截图。
- 编写或调试 FracMan 宏，尤其是无输出、报错、超时、对话框卡住。
- 解析 `.fab/.ffb/.f2d/.sab/.fml` 并给出可核验的数字。
- 无人值守地批量生成裂缝网络或视图。
- 需要开源 DFN 生成、共形网格或与 FracMan 统计对比时，走 `dfnworks_*`（第 11 节）。

## 1. 开工 60 秒检查

1. `get_config`：确认 `fracman_exe_exists=true`，workspace 为 ASCII 路径。
2. `discover_sessions`：拿到 `fracman_<pid>`。不要凭端口或进程名猜会话。
3. `set_active_session` + `ping`：状态栏为“准备好”才算可用。
4. `get_session_info`：读 `project`、`dialogs`、窗口/视图矩形、日志尾部。
5. 读资源 `fracman://agent-instructions`；命令不确定时调用
   `get_macro_reference(topic=...)`。

会话 id 就是 PID；窗口标题形如 `弗拉克曼 8.00 Beta ... - <project>`。用户自有会话
`owned=false`，关闭或终止前必须获得明确授权。

## 2. 通道选择

| 目标 | 工具 | 判据 |
| --- | --- | --- |
| 改用户眼前的模型、短宏 | `run_macro` | 单飞；`.fml` 出现尾部哨兵即完成 |
| 大批量生成、无需 GUI | `submit_macro` | 退出码无意义；看 `.fml` + 产物 |
| 读结果文件 | `inspect_file` | 按扩展名分派解析 |
| 截整窗或 3D 视图 | `capture_window` / `get_viewport_image` | `capture_method=screen` 才可信 |
| 打开工程副本 | `open_project(copy=true)` | 新进程、不改源文件 |
| 关闭 MCP 自建会话 | `close_session` | 默认对“保存？”答“否” |
| 开源 DFN、共形网格、连通性 | `dfnworks_generate` / `dfnworks_mesh` / `dfnworks_graph` | WSL 作业；看 `result.json` |
| 与 FracMan 结果对比 | `dfnworks_compare` | 计数/面积/P32/IoU/极点夹角 |

实时宏与批处理都能改模型；区别是实时宏占用用户窗口、能截图，批处理不打扰用户但
无法看到中间画面。生成类任务优先批处理，展示类任务用实时宏。

## 3. 宏写作硬规则

- 先 `SetCurrentUnits Length m`，再写任何数值命令。跳过单位设置的后果是数值被
  自动附加单位：日志里会出现 `Seed = 12 [m]`，参数含义被悄悄改掉。
- 需要复现时先 `SetSeed`；生成日志会打印实际使用的随机数种子。
- 坐标约定：X 东、Y 北、Z 上，深度为负。埋深 500 m 的盒子是 `z=-500..0`，
  即 `Center = 0, 0, -250`、`Size = 1000, 1000, 500`。
- 字符串用双引号、数字裸写、布尔值 `0/1`、`#` 开头为注释。
- 宏内路径用正斜杠；文件对话框路径由 MCP 自动转反斜杠，不要手工混用。
- 实时宏里禁止写 `LOGTOFILE` 和 `EXITONERROR`（MCP 自行包装；后者会杀掉用户会话）。
- `EXIT`、`DeleteAll` 默认被拒绝；`STOP` 在实时宏中也会被拒绝，因为它会在完成
  哨兵之前终止宏。
- 报错行号来自包装后的宏，通常比用户原文大 5 行左右；以 `#Macro file ... (line: N)`
  和宏栈为准。

## 4. 实时宏判停语义（重点）

`run_macro` 的包装宏结构：

```
BEGIN LOGTOFILE
FILE = "<run_dir>/run.fml"
END
<用户宏>
BEGIN RunMacro
Path = "<run_dir>/sentinel.fmf"
END
```

- 完成判据是 `.fml` 中出现哨兵路径（`#Running Macro: ...` 或 `RunMacro` 块），
  因此**静默宏**也能在数秒内返回 `completed`。
- `DrawingOn` / `DrawingOff` 是回放专用命令，不写入 `.fml`，不能用作完成哨兵。
  如果 `.fml` 已有用户命令却仍返回 `timeout_pending`，说明 MCP 仍是 v0.1.0，
  升级到 v0.1.1 或手动检查 `.fml` 尾部。
- `status=busy`：上一请求尚未结束。调用 `get_request_status`，**绝不重放**。
- `status=timeout_pending`：后台线程仍在等哨兵，会话锁保留；继续用
  `get_request_status(wait_seconds=...)` 查询。
- `status=macro_error`：`.fml` 出现 `#Macro Error`，后续命令不会执行。
- `status=needs_user_action`：弹出了“保存/是/否”类确认框，需人工在 GUI 里回答。
- 成功完成后响应顶层直接给出 `errors`、`warnings`、`elapsed_seconds`、`fml_tail`；
  信息类对话框只进 `warnings`，含 error/invalid/failed 等字样的对话框才判失败。

## 5. 已验证的 DFN 配方

1 km × 1 km × 500 m、80 条随机裂缝。实测宏提交到 FAB 落盘约 14 s，整次实时调用约 15 s：

```
BEGIN SetSeed
Seed = 20261031
END
BEGIN CREATEREGION
ObjectName = "DFN_1km_Box"
Type = "Box"
Center = 0, 0, -250
Size = 1000, 1000, 500
END
BEGIN DefineFractureSetDefinition
DefinitionName = "DFN_1km_Def"
FractureModel = "REGION"
RegionName = "DFN_1km_Box"
ClipToRegion = 1
UseSurfacePoints = 0
Count = 80
AdjustedByTruncation = 0
UsePole = 1
BEGIN OrientationDistribution
Type = "FISHER"
Parameter "Mean Trend" = 15
Parameter "Mean Plunge" = 85
Parameter "Concentration" = 8
END
BEGIN SizeDistribution
ValueType "Length" Units "m"
Type = "UNIFORM"
Parameter "Mean" = 100
Parameter "Deviation" = 40
END
NumSides = 5
Termination = 0
END
BEGIN GenerateFractureSet
DefinitionName = "DFN_1km_Def"
FractureSetName = "DFN_1km_Set"
END
BEGIN ExportFile
Path = "<workspace>/exports/dfn_1km_1km_500m_n80.fab"
TypeName = "FAB"
Object = "DFN_1km_Set"
END
```

预期结果（`inspect_file` 实测）：

| 指标 | 数值 |
| --- | --- |
| 裂缝数 | 80 |
| 包围盒 | X/Y −500..500，Z −500..0 |
| 总面积 | 2,375,836.46 m² |
| 面积均值 / 中位数 | 29,697.96 / 25,943.32 m² |
| 半径均值 | 116.22 m |
| 平均法向 | 走向 213.06°、倾角 83.88° |

区域线框会遮挡裂缝。隐藏它（不要删除）：

```
BEGIN SetObjectOption
Visible = 0
Object = "DFN_1km_Box"
END
```

隐藏全部区域用 `Object = "Regions"`；恢复显示把 `Visible` 改为 `1`。

## 6. 调试决策树

1. `busy` → `get_request_status`；确认上一请求结束后再发新宏。
2. `timeout_pending` → 看 `.fml` 尾部：
   - 有用户命令、无 `RunMacro` 哨兵 → 旧版包装器，升级 MCP。
   - `.fml` 不存在 → 宏根本没执行：文件对话框路径或焦点问题；查
     `get_session_info.dialogs`，重发一个新请求（不是重放旧请求）。
   - 有哨兵但未完成 → 后台尚未轮询到，等待或查状态。
3. `macro_error` → 读 `errors` 原文，修单位/语法/对象名；确认 `ping` 准备好再重试。
4. `ok=false` 且 `errors=[]` → 旧版把信息框当失败；查 `warnings`，或升级 MCP。
5. 截图黑边/旧画面 → `capture_method` 为 `printwindow`；升级后用屏幕抓取，
   并确认 `activated=true`。
6. 截图抓到了别的窗口 → 前台切换失败；重试或先手动把 FracMan 置前。
7. 区域盒挡住裂缝 → `SetObjectOption Visible=0`，不要删除区域。
8. 数值被加单位（`12 [m]`）→ 宏开头缺 `SetCurrentUnits`。
9. 批处理第 3 个被拒 → `BatchBusy (2/2)`；等 `monitor_run` 或调整
   `FRACMAN_MCP_MAX_BATCH`，不要并发硬闯。
10. 批处理退出码 0 但判失败 → 退出码不可信；只看 `.fml` 与预期产物。

## 7. 截图规范

- `capture_window(mode="view")` 只抓 3D 视图；`mode="full"` 抓整窗。
- 截图会先把 FracMan 激活到前台，再按物理像素抓取；会短暂抢焦点。
- 返回 `capture_method`：`screen` 可信；`printwindow` 是后备，GPU 视图可能返回
  旧帧或黑边，不得直接用于交付。
- 交付前必须打开 PNG 自检：能看到模型、图例和坐标轴；只有线框没有裂缝说明
  可见性/相机没设好。

## 8. 批处理

- `submit_macro` 自动加 `LOGTOFILE`、`EXITONERROR`、`EXIT`；返回值里的 `run_id`
  是监控句柄。
- 默认最多 2 个并发；达到上限返回 `BatchBusy` 并附活动运行，不排队。
- 成功判据：进程退出 + `.fml` 无 `#Macro Error` + `expected_artifacts` 存在。
- `cancel_run` 只终止 MCP 自己启动的 PID，绝不碰用户 GUI 会话。
- `get_run_log` 可同时看宏日志、stdout、stderr 与解析后的错误。

## 9. 安全边界

- 不关闭、不终止用户自有 FracMan 会话，除非明确授权。
- 实时宏默认拒绝 `EXIT` / `DeleteAll`；不要用 `allow_*` 绕过用户的正常模型。
- 测试工程一律用副本（`open_project(copy=true)`）；培训资料只读。
- 工作区默认 `%LOCALAPPDATA%\FracManMCP`，路径保持 ASCII。
- MCP 是本地 stdio，无网络监听；不要把会话暴露到网络。

## 10. 交付清单

报告时给出：对象名、关键参数、裂缝数、包围盒、面积统计、产物路径与大小/哈希、
截图路径与尺寸。若模型只在实时会话里，注明“工程未保存，`.fab` 是持久产物”。
没有 `.fml` 与产物双重证据时，不要声称任务成功。

## 11. dfnWorks 通道（WSL）

同一台服务器里还有 9 个 `dfnworks_*` 工具，负责开源 DFN 生成、LaGriT 共形网格、
连通性图与 FracMan 对比。它们不碰 GUI，与 FracMan 工具可混用。

标准顺序：

```
dfnworks_check()
gen = dfnworks_generate(domain=[100,100,100], seed=20261101)
dfnworks_mesh(gen["job_id"])
dfnworks_graph(gen["job_id"], k=10)
dfnworks_compare(r"C:\path\model.fab", job_id=gen["job_id"])
```

- 三个阶段用同一 seed 重建同一 DFN；`job_id` 幂等，重提不同参数会被拒绝。
- 作业记录随重启从 `<workspace>/dfnworks/jobs/<job_id>/` 恢复；重启时没写出
  `result.json` 的作业显示 `interrupted`，同 `job_id` 同参数重提即原目录续跑。
- `result.json` 是唯一可靠判错来源；`dfnworks_job_log` 另外给出 LaGriT
  只在 `run.log` 里的报错。
- 默认按域尺寸缩放：`h = L/50`、半径 `= L/5`、三组斜交 constant family、
  `n_poly = 100`、网格 `h/4` 与 `5h`。20 m / 100 m / 1 km 立方域实测均为
  25 条裂缝且左右连通。
- 失败模式先说结论再调参：
  - `no intersecting fractures`：`h` 相对裂缝太粗（`h/半径 ≥ 0.2`）或半径/数量
    不足；优先加大半径或减小 `h`，不要先加 `n_poly`。
  - `3h meshing limit`：正方形 family 4 点轮廓最短边 < `3h`；提高半径或 `aspect`。
  - `No fractures in the network are in the requested family`：某 family 一条都
    没留下，且显式给了 `hy_variable`；去掉水力学属性或放宽该 family。
- 不封装 PFLOTRAN/FEHM 与 dfnTrans；需要流动/输运时改用 WSL 内的 dfnWorks 原始流程。
- 细节以资源 `fracman://dfnworks-guide` 为准。

## 参考

- `references/macro-cookbook.md`：命令模板、分布语法、校验规则、错误格式。
- `references/gui-automation.md`：UIA 控件表、实时执行流程、焦点与截图内幕。
- `references/troubleshooting.md`：症状排查表、证据命令、复盘模板。

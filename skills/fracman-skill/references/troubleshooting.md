# FracMan MCP 排查手册（真实案例）

每条都来自本机实测。原则：先取证，再改；不确定的执行结果绝不重放。

## 1. 症状速查

| 症状 | 先看什么 | 根因 | 处理 |
| --- | --- | --- | --- |
| `ok=false` 但导出的 FAB 完全正确 | `.fml` 无 `#Macro Error`；`warnings` | 信息类对话框被误判为失败（v0.1.0） | 升级到 v0.1.1；信息框只进 `warnings` |
| 静默宏 40–60 s 后 `timeout_pending`，`.fml` 只有用户命令 | `.fml` 尾部；`elapsed_seconds` | 完成判定依赖日志面板变化；`DrawingOn` 不写入 `.fml` | 升级到 v0.1.1（`RunMacro` 哨兵）；实测 6.3 s 完成 |
| `.fml` 根本不存在 | 运行目录内容；`get_session_info.dialogs` | 宏未被执行：文件对话框路径/焦点问题 | 确认路径反斜杠、GUI 未卡死；发**新**请求 |
| 返回 `busy` | `active_request_id` | 上一实时宏仍在跑/等待哨兵 | `get_request_status` 等待；不要重放 |
| 返回 `needs_user_action` | `dialogs` 文本 | 弹出保存/是否确认框 | 人工在 FracMan 回答；锁会保留 |
| 截图是旧画面、带黑边、内容缩小 | `capture_method` | `PrintWindow` 对 GPU 视图返回旧帧 | 升级后用 `screen`；确认 `activated=true` |
| 截图抓到了别的窗口 | `activated` | FracMan 未成功切到前台 | 重试；先手动置前；避免后台截图 |
| 日志出现 `Seed = 12 [m]` | 宏开头 | 未设置长度单位，数值被附加单位 | 先 `SetCurrentUnits Length m` |
| 区域黄框挡住裂缝 | 可见性 | 区域线框显示在裂缝前面 | `SetObjectOption Visible=0`，不要删除区域 |
| 批处理第 3 个被拒 | 响应里的活动运行 | 并发上限 2 | 等 `monitor_run`；必要时调 `FRACMAN_MCP_MAX_BATCH` |
| 批处理退出码 0 却判失败 | `.fml` 的 `#Macro Error` | 退出码不可信；宏中途报错 | 按 `.fml` 行号修复；补 `expected_artifacts` |
| 错误行号比原文大 5 行 | `#Macro file (line: N)` | 包装宏头部占用行数 | 用宏栈定位到用户行 |

## 2. 取证顺序

1. `get_session_info(session_id, log_chars=4000)`：状态、工程名、对话框、窗口矩形。
2. 读运行目录：
   - `macro.fmf`：实际提交的包装宏；
   - `run.fml`：唯一可信判据；
   - `sentinel.fmf`：完成哨兵内容。
3. `get_request_status(request_id, wait_seconds=...)`：确认是否仍 running。
4. `inspect_file(export_path)`：数量、包围盒、面积、集合、属性。
5. 截图自检：`capture_method`、尺寸、画面内容。

PowerShell 辅助（仅当 MCP 不可用时）：

```powershell
Get-Content "<run_dir>\run.fml" -Tail 30
Get-FileHash "<export>" -Algorithm SHA256
Get-ChildItem "<run_dir>"
```

## 3. 典型案例复盘

### 3.1 信息框导致假失败

- 现象：DFN 宏执行完，FAB 32,295 B 正常，`run_macro` 却 `ok=false`。
- 证据：`.fml` 没有 `#Macro Error`；自动关闭的对话框文本是提示信息。
- 根因：旧逻辑要求“没有任何对话框”才算成功。
- 修复：区分错误类与信息类对话框；信息进 `warnings`，只有错误关键词才失败。
- 验证：再跑同一宏，`ok=true`，FAB 哈希不变。

### 3.2 静默宏超时

- 现象：`SetCurrentUnits Length m` 40 s 后 `timeout_pending`；`.fml` 有 4 行命令。
- 证据：宏确实执行，但日志面板无新内容；`DrawingOn` 不在 `.fml` 中。
- 根因：完成判定依赖日志变化，静默命令没有输出。
- 修复：包装宏末尾追加 `RunMacro` 指向只含注释的哨兵文件；轮询 `.fml` 中的哨兵路径。
- 验证：同一宏 6.3 s 返回 `completed`；错误宏 5.9 s 返回 `macro_error`。

### 3.3 截图旧帧

- 现象：整窗 2880×1523 的截图里，3D 视图内容只有约 66% 大小，周围黑边，且是旧画面。
- 证据：`capture_method=printwindow`；UIA 读到的视图矩形与窗口客户区一致，说明裁剪坐标没错。
- 根因：`PrintWindow` 对 GPU 渲染视图返回旧窗口尺寸的缓存帧。
- 修复：激活窗口后 `ImageGrab` 屏幕抓取；`PrintWindow` 仅后备。
- 验证：`capture_method=screen`，裂缝与黄色区域框完整可见。

### 3.4 截图抓到别的应用

- 现象：屏幕抓取得到的是 Qoder 窗口。
- 证据：FracMan 的 `GetForegroundWindow` 不是目标 hwnd。
- 修复：`ShowWindow(SW_RESTORE)` + `set_focus` + `SetForegroundWindow` 重试 3 次，
  并核验前台句柄。
- 注意：截图会抢焦点，执行前告知用户。

### 3.5 区域遮挡

- 现象：DFN 生成成功，但 3D 视图只看到黄色线框，看不到裂缝。
- 根因：区域盒以实体/线框绘制在裂缝前面。
- 修复：`BEGIN SetObjectOption / Visible = 0 / Object = "DFN_1km_Box" / END`。
- 验证：80 条品红裂缝显现；导出数据不受影响。

## 4. 复盘模板

新问题解决后，把下面字段补进本文件或工作区调试记录：

```
日期：
会话/运行目录：
目标宏：
症状：
证据（.fml 行、对话框、截图路径、文件哈希）：
根因：
修复（文件名/函数/版本）：
验证（命令 + 结果）：
遗留风险：
```

## 5. 成功判据清单

- [ ] `get_session_info` 状态为“准备好”，无未处理对话框。
- [ ] `request_id` / `run_id` 状态为 `completed`，不是 `timeout_pending`。
- [ ] `.fml` 无 `#Macro Error`，尾部有哨兵（实时）或正常结束（批处理）。
- [ ] 产物存在且 `inspect_file` 的数量、包围盒、面积在预期范围。
- [ ] 截图 `capture_method=screen`，画面包含模型与图例。
- [ ] 报告里写清对象名、参数、路径与“工程是否保存”。

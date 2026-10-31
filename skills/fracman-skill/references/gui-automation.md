# FracMan GUI 自动化内幕（实测）

FracMan 8.00 Beta 没有公开 API。MCP 通过 Windows UIA（pywinauto）点按钮、填文件
对话框、读状态栏与日志，并用屏幕抓取截图。以下控件与流程都在这台机器上验证过。

## 1. 会话模型

- 会话 id：`fracman_<pid>`，例如 `fracman_48136`。
- 会话由 `discover_sessions` 枚举 FracMan 窗口得到；`owned` 表示是否由 MCP 启动。
- 主窗口标题：`弗拉克曼 8.00 Beta (2021.02.05) 64bit - <project>`；
  project 为空表示未命名/未保存工程。
- 主窗口内部是 MDI 工作区：左侧 Objects/Definitions 停靠栏，右侧 3D 视图，
  下方日志面板与图例面板。

## 2. UIA 控件表

| 控件 | 识别方式 | 用途 |
| --- | --- | --- |
| Ribbon | auto_id `59398` | 找“运行 宏...”按钮 |
| 日志面板 | Scintilla，auto_id `3715` | 读状态与增量；上限 4096 字符 |
| 状态栏 | auto_id `59393` | 读“准备好”/忙碌 |
| 3D 视图 | class `AfxFrameOrView140u`，auto_id `59648` | 截图裁剪区域 |
| MDI 子窗 | title `FracMan1` | 3D 视图宿主 |
| Objects 树 | auto_id `3713` | 对象可见性、层级 |
| 文件对话框 | Edit `1148`，按钮 `1` | 填宏/工程路径 |
| 错误消息框 | Static `65535`，按钮 `1` | 读文本、点“确定” |

枚举控件时用 `ctrl.element_info.control_type`，不要访问不存在的 `ctrl.control_type`。

## 3. 实时宏执行流程

1. 读状态栏，非“准备好”直接拒绝。
2. 关闭残留错误框，记录日志面板快照。
3. 找到并点击 Ribbon 的“运行 宏...”按钮。
4. 最多等 8 s 的文件对话框。
5. 把 `.fmf` 路径写入 Edit `1148`（反斜杠），点“打开”。
6. 每 250 ms 轮询：模态框、状态栏、日志面板、`.fml` 文件。
7. 判定：
   - `.fml` 出现 `#Macro Error` → 立即 `macro_error`。
   - `.fml` 出现哨兵路径 → `completed`。
   - 弹出“是/否”确认框 → `needs_user_action`，锁保留。
   - 等待超过调用方 `wait_seconds` → `timeout_pending`，后台线程继续。
8. 只有确认 GUI 已恢复“准备好”才释放会话锁；否则保留锁，绝不重放。

实测耗时 6–19 s/次，主要花在 UIA 控件树扫描、日志读取与对话框轮询。

## 4. 模态框处理

- 错误框：Static `65535` 读文本，点“确定”关闭；文本同时写入 `errors`。
- 信息框：自动点“确定”，文本进 `warnings`，不判失败。
- 失败判定关键词：`error`、`错误`、`invalid`、`无效`、`failed`、`失败`、
  `cannot`、`无法`、`not found`、`找不到`。
- 保存/是否确认框不自动回答——猜错会丢数据或改工程。

## 5. 截图

流程：

1. `ShowWindow(SW_RESTORE)`（若最小化）+ UIA `set_focus` + `SetForegroundWindow`，
   重试 3 次并核验 `GetForegroundWindow`。
2. `ImageGrab.grab(all_screens=True)`，按虚拟屏幕坐标裁剪窗口或 3D 视图。
3. 返回 `capture_method`：`screen` 或 `printwindow`；`activated` 表示前台是否成功。

为什么不用 `PrintWindow`：GPU 渲染的 3D 视图会返回**旧尺寸的旧帧**，内容缩放到
约 66% 并带黑边，裁剪后看起来像抓错了窗口。它只作屏幕抓取失败后的后备。

注意：

- 进程已 `SetProcessDPIAware`，返回的是物理像素；本机整窗 2880×1523，
  3D 视图 1892×1224。
- 截图会抢焦点；批量截图时提前告知用户。
- `get_viewport_image` 返回 PNG data URI，适合直接展示；`capture_window` 返回
  路径与尺寸，适合交付。

## 6. 对象可见性

- 首选宏：`BEGIN SetObjectOption / Visible = 0 / Object = "<名称>" / END`。
  `Object = "Regions"` 可一次隐藏全部区域线框。
- UI 后备：Objects 树（auto_id `3713`）每项的可见性复选框是文本左侧的原生状态
  图像，点击位置约为 `item.rect.left - 38`、y 取行中心。不同 DPI 下偏移可能变化，
  能用宏就不要点像素。
- 区域线框是黄色，裂缝是品红色；线框只影响显示，不影响导出数据。

## 7. 工程生命周期

- `open_project(copy=true)`：把工程复制到 `<workspace>/projects/`，再启动新 FracMan
  进程打开副本；源文件不变。
- `close_session(save=false)`：对“保存？”答“否”；`save=true` 才答“是”。
- 用户自有会话（`owned=false`）默认拒绝关闭；`force=true` 也只应在明确授权时使用。
- 启动/许可对话框只自动点 OK/Continue，其余报告给调用方。

## 8. 已知限制

- 日志面板只保留 4096 字符；长日志必须读 `.fml`。
- UIA 扫描慢，实时宏不适合高频小命令循环；批量或一条宏内合并。
- `.frd` 是未公开二进制，只做横幅/对象名启发式解析，不解码数值。
- 同一会话单飞；`busy` 时不要并行再发。
- 文件对话框路径必须是反斜杠；宏内路径必须是正斜杠。

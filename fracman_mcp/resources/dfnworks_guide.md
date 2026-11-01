# dfnWorks 通道操作指南

dfnWorks（LANL）运行在 WSL 内的独立 venv 中，本 MCP 只负责校验参数、生成驱动脚本、
启动/监控 WSL 作业并解析结果。FracMan 与 dfnWorks 的工具共用同一个 stdio 服务器。

## 先检查环境

```
dfnworks_check()
```

返回 `probe`（pydfnworks 版本、`.dfnworksrc`、DFNGen/LaGriT 路径、networkx 版本）与
`checks`。`ok=false` 时先修环境，不要盲目提交作业。

默认值（可用环境变量覆盖）：

| 设置 | 环境变量 | 默认 |
| --- | --- | --- |
| WSL 发行版 | `FRACMAN_WSL_DISTRO` | `Ubuntu` |
| WSL 用户 | `FRACMAN_WSL_USER` | `wmw` |
| dfnWorks venv | `FRACMAN_DFNWORKS_VENV` | `/opt/dfnworks-venv` |
| dfnWorks 仓库 | `FRACMAN_DFNWORKS_REPO` | `/mnt/d/software/dfnWorks` |
| 并发作业数 | `FRACMAN_MCP_DFN_MAX_JOBS` | `1` |
| 作业超时 | `FRACMAN_MCP_DFN_TIMEOUT` | `7200` 秒 |

作业目录：`<workspace>/dfnworks/jobs/<job_id>/`。路径在 WSL 中通过 `/mnt/<盘符>`
访问，因此工作区必须位于带盘符的本地路径，不能是 UNC。

## 标准流水线

```
gen = dfnworks_generate(domain=[100,100,100], seed=20261101)
dfnworks_mesh(gen["job_id"], uniform_mesh=True)
dfnworks_graph(gen["job_id"], graph_type="fracture", inflow="left", outflow="right")
dfnworks_compare(r"C:\path\model.fab", job_id=gen["job_id"])
```

三个阶段都会按同一 seed 重建同一 DFN：

- `generate`：DFNGen 生成 + 统计 + 裂缝几何导出；
- `mesh`：再生成 + LaGriT 共形网格（最慢）；
- `graph`：再生成 + 连通性图/主干（不建网格，明显更快）。

## 默认值与缩放关系

不传任何几何参数时，规格按域尺寸 `L = max(domain)` 自动缩放：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `h` | `L/50` | LaGriT 目标尺度；同时是 DFNGen 的 FRAM 最小间距 |
| 裂缝半径 | `L/5` | 三组 constant family，概率 0.3/0.3/0.4，走向/倾伏 0/0、35/90、70/45 |
| `n_poly` | `100` | 尝试条数；实际保留的是连通左右边界的主簇 |
| 网格 `min_dist` | `h/4` | 交点附近分辨率 |
| 网格 `max_dist` | `5h` | 远场分辨率 |

实测（域 20 m、100 m、1000 m 立方体）：默认参数均得到 25 条裂缝、51 个交点、
左右边界连通。`h` 相对裂缝过粗会失败：

| `h / 半径` | 结果 |
| --- | --- |
| 0.1（默认） | 生成成功、左右连通 |
| 0.2 | `no intersecting fractures` |
| 0.4 | `no intersecting fractures` |

原因是 DFNGen 的 FRAM 在 `checkCloseEdge` 中把 `h` 当作交点最小间距，交点被缩短
超过 10% 就被丢弃；`h` 过粗会把几乎全部交点判掉。裂缝半径相对域太小
（`半径/L <= 0.15`）则连通簇无法贯穿边界。

`n_poly` 与实际裂缝数不是线性关系：100 → 25 条，200 → 68 条，400 → 230 条
（100 m 域）。需要更密网络时调大 `n_poly`，代价主要在 LaGriT 网格。

dfnWorks 还要求每条矩形 family 的 4 点轮廓最短边 ≥ `3h`，即
`半径 × min(√(1+aspect²), 2·aspect, 2) ≥ 3h`；违反时 MCP 在提交前就拒绝
（`3h meshing limit`），避免 WSL 里跑一轮才报错。

## 参数要点

- `stop_condition=0`：用 `n_poly` 与各 family 的 `probability` 控制数量；
  `stop_condition=1`：用各 family 的 `p32` 控制强度。
- `families` 直接使用 `add_fracture_family` 的关键字：`shape`（rect/ell）、
  `distribution`（constant/tpl/log_normal/exp）、`kappa`、`aspect`、`trend`、
  `plunge`、`constant`/`min_radius`/`max_radius`、`hy_variable`、`hy_function`、
  `hy_params` 等；未知键会被拒绝，避免拼写错误静默生效。
- 默认不赋水力学属性。dfnWorks 在某一 family 一条裂缝都没留下时会直接
  `SystemExit`；本通道只做几何、网格与连通性，需要时再显式给 `hy_variable`。
- 共形网格规模近似随 `(L/h)^2 × 裂缝数` 增长。
- 图边界的 `inflow`/`outflow` 取 `left/right/front/back/top/bottom`。

## 产物

| 文件 | 内容 |
| --- | --- |
| `dfn_stats.json` | 裂缝数、family 计数、面积/半径统计、bbox、P32、平均极点、交线统计、连接度 |
| `fractures.json` / `.csv` | 每条裂缝的顶点、中心、法向、面积、半径、family |
| `mesh_stats.json` | 节点数、三角形数、材料数、网格文件大小 |
| `graph.json` | networkx node-link 图（含 s/t 源汇节点） |
| `graph_metrics.json` | 节点/边/连通分量、s-t 连通、最短路径、主干规模 |
| `backbone.json` | k 最短路径主干（仅 s-t 连通时） |
| `result.json` | 驱动最终状态；**唯一可靠判错来源** |
| `stdout.txt` / `stderr.txt` | 阶段进度与 Python traceback |

## 判错与取消

- `dfnworks_job_status` 的 `status`：`queued/running/completed/failed/timeout/cancelled`。
- 驱动捕获 `SystemExit` 与异常并写入 `result.json`；`result.ok=false` 时查看
  `errors` 里的 traceback 尾部。
- 超时只报告 `timeout`，不会自动重放；同一 `job_id` 用不同参数重提会被拒绝。
- `dfnworks_job_cancel` 只终止本服务器启动的 WSL 驱动（按作业唯一路径匹配），
  不会影响用户自己的 WSL 进程。
- 网格/图阶段失败时先看 `dfnworks_job_log` 的 `dfnworks_logs`，LaGriT 的报错通常
  只在 `run.log` 中。

## 与 FracMan 对比

`dfnworks_compare` 同时解析 FracMan `.fab/.ffb` 与 dfnWorks 统计，输出：

- 裂缝数比值、总面积/平均面积/平均半径比值；
- P32（dfnWorks 自带；FracMan 侧用 `domain` 或 dfnWorks 域体积折算）；
- 包围盒 IoU、平均极点夹角（检查 trend/plunge 与 strike/dip 约定差异）；
- 2 倍阈值告警。

FracMan 的 set 与 dfnWorks 的 family 只是并排列出，不假设编号对应。

## 不包含的内容

本通道不封装 PFLOTRAN/FEHM 流动与 dfnTrans 输运；它们需要额外的求解器与配置。
需要流动/输运时应在 WSL 中直接使用 dfnWorks 原始工作流。

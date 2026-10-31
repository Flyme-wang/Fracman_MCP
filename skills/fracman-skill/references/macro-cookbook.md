# FracMan 宏调试手册（实测子集）

所有片段均在 FracMan 8.00 Beta（2021.02.05，64 bit）上针对本机 MCP 验证过。
未列出的命令请用 `get_macro_reference` 或 GUI 录制宏确认后再用。

## 1. 包装方式

### 实时宏（MCP 自动生成）

```
BEGIN LOGTOFILE
FILE = "<workspace>/runs/live_<时间戳>_<token>/run.fml"
END

<用户宏>

BEGIN RunMacro
Path = "<workspace>/runs/live_<时间戳>_<token>/sentinel.fmf"
END
```

- 哨兵文件只含一行注释，用于在 `.fml` 中留下完成标记。
- 实时宏禁止 `LOGTOFILE`、`EXITONERROR`、`STOP`、`EXIT`、`DeleteAll`（后两者需显式授权）。
- 单飞：同一会话同时只能有一个实时宏。

### 批处理（MCP 自动生成）

```
BEGIN LOGTOFILE
FILE = "<run_dir>/run.fml"
END
BEGIN EXITONERROR
END

<用户宏>

EXIT
```

- 成功判据：进程退出 + `.fml` 无 `#Macro Error` + 预期产物存在。
- 退出码恒为 0，不能作为判据。
- 默认并发 2，超出返回 `BatchBusy (2/2)`。

## 2. 单位与可复现性

```
BEGIN SetCurrentUnits
Value = "Length"
Units = "m"
END
BEGIN SetSeed
Seed = 20261031
END
```

- 单位设置必须放在数值命令之前，否则日志会出现 `Seed = 12 [m]` 这类污染。
- 用 `SetSeed` 固定随机数；`GenerateFractureSet` 会在日志打印实际种子：
  `#Generating Fractures <定义名> with rand <整数>`。
- 若要换一套裂缝但保持统计特征，只改种子，不改分布。

## 3. 区域

```
BEGIN CREATEREGION
ObjectName = "DFN_1km_Box"
Type = "Box"
Center = 0, 0, -250
Size = 1000, 1000, 500
END
```

- 日志会把 `CREATEREGION` 回显为 `EDITREGION`，两者等价。
- `Center` 是几何中心，`Size` 是三边长度。埋深 500 m、平面 1 km 见方：
  X/Y 中心 0，Z 中心 −250，Z 尺寸 500，得到 X/Y −500..500、Z −500..0。
- 隐藏区域线框（不删除）：

```
BEGIN SetObjectOption
Visible = 0
Object = "DFN_1km_Box"
END
```

  全部区域用 `Object = "Regions"`；恢复显示改 `Visible = 1`。
- `BEGIN SelectObject / Action = 1 #1=replace, 2=add, 3=rem, 4=toggle / Object = "..." / END`
  可用于选择对象，实测可用。

## 4. 裂缝集定义

```
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
```

要点：

- `Count` 是目标裂缝数；`ClipToRegion = 1` 保证裂缝被区域裁剪。
- `AdjustedByTruncation = 0` 表示不因截断回填数量；需要严格数量时保持 0 并检查导出数。
- `NumSides = 5` 生成五边形多边形裂缝；多边形会做非凸优化。
- 方向分布 `FISHER` 的参数名必须带引号：`Parameter "Mean Trend" = 15`。
- 尺寸分布 `ValueType "Length" Units "m"` 决定 `Mean/Deviation` 的单位。
- 可省略 `Properties`，FracMan 会补默认的 `Transmissivity`、`Storativity`；
  需要导水/储水属性时显式定义：

```
BEGIN Properties
Name = "Aperture"
BEGIN Distribution
ValueType "Aperture" Units "m"
Type = "CONSTANT"
Parameter "Value" = 0.0001 [m]
END
END
```

- 已验证的分布类型：`CONSTANT`、`UNIFORM`、`LOGNORMAL`、`EXPONENTIAL`。
- `BEGIN GenerateFractureSet` 消费定义：

```
BEGIN GenerateFractureSet
DefinitionName = "DFN_1km_Def"
FractureSetName = "DFN_1km_Set"
END
```

## 5. 导出

```
BEGIN ExportFile
Path = "<workspace>/exports/dfn.fab"
TypeName = "FAB"
Object = "DFN_1km_Set"
END
```

- `TypeName = "FAB"` 对裂缝集验证可用；其他类型名未经证实，先录制宏。
- 宏内路径正斜杠；导出目录需已存在（MCP 的 `exports/` 会自建）。
- 导出后用 `inspect_file` 核对：裂缝数、包围盒、面积统计、集合名、属性名。

## 6. 错误格式

```
#Macro Error: Invalid value/unit specified
#11
#END
#Macro file macro.fmf (line: 12)
#
#Invalid value/unit specified
#
#   Macro Stack:
#   macro.fmf (line: 14)
```

- 错误发生后剩余命令不再执行，所以哨兵不会出现；MCP 会立即返回 `macro_error`。
- 行号对应包装后的宏，比用户原文大约 +5 行；用宏栈里的最后一行定位。
- 常见原因：单位未设置、参数名拼错、对象名不存在、路径目录不存在、数值写成字符串。

## 7. 校验与拒绝矩阵

| 内容 | 实时宏 | 批处理 |
| --- | --- | --- |
| `LOGTOFILE` | 拒绝（MCP 自带） | 拒绝（MCP 自带） |
| `EXITONERROR` | 拒绝（会杀会话） | MCP 自动添加 |
| `EXIT` | 拒绝，除非 `allow_exit=true` | 自动补，不重复 |
| `DeleteAll` | 拒绝，除非 `allow_destructive=true` | 同样受限 |
| `STOP` | 拒绝（跳过哨兵，无法判完成） | 允许但不要用：`EXIT` 不会执行，进程挂到超时 |
| `RunMacro` | 允许，但产生告警 | 允许 |

`BEGIN/END` 或 `BEGIN_LOOP/END_LOOP` 数量不配对只告警，不阻止执行；但这类宏
通常会在运行中报错。

## 8. 读日志

- `.fml` 是唯一可信判据；实时宏日志还会记录 `#Running Macro: <路径>` 与 `RunMacro` 块。
- 实时宏日志来自 FracMan 的 LOGTOFILE，下一次宏会把 `RunMacro` 记录写进**上一个**
  日志文件，这是正常现象，不要据此判断本次执行。
- UIA 日志面板只保留最近 4096 字符；长宏请直接读 `.fml` 文件。

## 9. 交付前数值核验

对 FAB 至少核对：

1. `fracture_count` 与请求的 `Count` 一致。
2. `bbox` 落在区域范围内（允许 1e-5 量级浮点误差）。
3. `set_counts` 只有一个集合且数量正确。
4. `area.mean`/`median` 量级合理；五边形、半径约 100 m 时面积在 1e4 m² 量级。
5. 文件大小非 0，必要时记录 SHA-256。

## 10. 记录新命令

`Tools → Record Macro` 能捕获建模/分析命令，但捕不到渲染与颜色变化。录制得到的
`.fmf` 可直接作为 `run_macro(path=...)` 的输入；先删掉录制文件里的 `LOGTOFILE`
再提交，避免与 MCP 包装冲突。

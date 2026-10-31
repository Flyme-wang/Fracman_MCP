# FracMan 8.0 macro reference (verified subset)

FracMan's macro language is documented in `D:\software\fracman\FracMan.chm`
under **Using Macros**. The list below contains only commands and forms that
were exercised against FracMan 8.00 Beta on this machine. The language is not
case sensitive.

## File and execution control

```
BEGIN LOGTOFILE
FILE = "C:/work/session.fml"
END
```
Writes progress messages and echoed commands to a `.fml` log. The MCP server
adds this wrapper itself; do not repeat it in user macros.

```
BEGIN EXITONERROR
END
```
Exit the program on the first macro error. Required for headless batch runs or
FracMan keeps waiting for input. The MCP batch runner adds it automatically.

```
EXIT
```
Quit FracMan. Rejected in live macros unless `allow_exit=true`.

```
STOP
```
Stop the current macro and keep the session alive (playback-only command, no
`BEGIN`/`END`).

```
BEGIN RunMacro
Path = "C:/work/child.fmf"
END
```
Execute another macro file.

```
BEGIN DeleteAll
END
```
Delete every project object. Rejected unless `allow_destructive=true`.

## Units and reproducibility

```
BEGIN SetSeed
Seed = 12345
END

BEGIN SetCurrentUnits
Value = "Length"
Units = "m"
END
```

## Regions

```
BEGIN CREATEREGION
ObjectName = "Box1"
Type = "Box"
Center = 0, 0, 0
Size = 100, 100, 100
END
```
The log echoes this as `EDITREGION`; both forms accept `ObjectName`, `Type`,
`Center`, and `Size`. `Type = "Box"` is verified.

## Fracture sets

```
BEGIN DefineFractureSetDefinition
DefinitionName = "SetDef1"
FractureModel = "REGION"
RegionName = "Box1"
ClipToRegion = 1
UseSurfacePoints = 0
BEGIN Properties
Name = "Aperture"
BEGIN Distribution
ValueType "Aperture" Units "m"
Type = "CONSTANT"
Parameter "Value" = 0.001 [m]
END
Name = "Permeability"
BEGIN Distribution
ValueType "Permeability" Units "m2"
Type = "CONSTANT"
Parameter "Value" = 9.869233e-13 [m2]
END
Name = "Compressibility"
BEGIN Distribution
ValueType "Compressibility" Units "1/kPa"
Type = "CONSTANT"
Parameter "Value" = 0.001 [1/kPa]
END
END
Count = 200
AdjustedByTruncation = 0
UsePole = 1
BEGIN OrientationDistribution
Type = "FISHER"
Parameter "Mean Trend" = 15
Parameter "Mean Plunge" = 85
Parameter "Concentration" = 15
END
BEGIN SizeDistribution
ValueType "Length" Units "m"
Type = "UNIFORM"
Parameter "Mean" = 10 [m]
Parameter "Deviation" = 3 [m]
END
NumSides = 5
Termination = 0
END
```

Notes proven by the probe log (`dfn_probe.fml`):

- `Count` is the fracture count; FracMan adds `Transmissivity` and
  `Storativity` defaults if they are omitted.
- Distribution blocks accept `Type = "CONSTANT" | "UNIFORM" | "LOGNORMAL" |
  "EXPONENTIAL"` with `Parameter "<name>" = <value> [unit]`.
- `BEGIN GenerateFractureSet` consumes the definition:

```
BEGIN GenerateFractureSet
DefinitionName = "SetDef1"
FractureSetName = "Set1"
END
```

- Generation log lines are prefixed with `#` (`#Generating Fractures…`,
  `#Writing Fractures`).

## Export

```
BEGIN ExportFile
Path = "C:/work/dfn.fab"
TypeName = "FAB"
Object = "Set1"
END
```
`TypeName = "FAB"` with a fracture-set `Object` is verified. Other export
type names exist but were not tested here; record a macro in the GUI to obtain
the exact form.

## Diagnosing macro errors

A failing live macro produces a FracMan message box and a `.fml` block:

```
#Macro Error: Invalid value/unit specified
#11
#END
#Macro file mymacro.fmf (line: 12)
#
#Invalid value/unit specified
#
#   Macro Stack:
#   mymacro.fmf (line: 14)
```

After the error, remaining commands are **not** executed. Fix the line named
in the stack and rerun only after checking the GUI is ready.

## Recording new commands

`Tools → Record Macro` captures network generation and analysis commands but
not rendering/colour changes. Recording is the reliable way to discover
command names for operations not covered above.

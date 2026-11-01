# FracMan MCP — agent instructions

You are controlling **FracMan 8.00 Beta** on this Windows machine through a
GUI bridge and a batch runner. FracMan has **no public API, COM server, or
Python module**. Every model change must be expressed as a FracMan macro.

## First moves

1. Call `discover_sessions` before any live operation. Session ids look like
   `fracman_48136` and correspond to the FracMan process id.
2. Call `get_session_info` to read the status bar ("准备好" = ready), the log
   tail, and any open dialogs.
3. Use `get_macro_reference` when unsure about macro commands. Prefer recording
   a macro in FracMan and replaying it over guessing command names.

## Choosing a channel

| Need | Tool |
| --- | --- |
| Change the project the user is looking at | `run_macro` (live GUI) |
| Heavy or repeatable generation, no GUI needed | `submit_macro` (batch) |
| Open-source DFN generation, conforming mesh, connectivity | `dfnworks_generate` / `dfnworks_mesh` / `dfnworks_graph` |
| Compare a FracMan `.fab/.ffb` with a dfnWorks job | `dfnworks_compare` |
| Read `.fab/.ffb/.f2d/.sab/.fml/.frd` | `inspect_file` |
| Screenshot the 3D view | `capture_window` / `get_viewport_image` |
| Open a project copy | `open_project` |

Live macros are single-flight per session. Batch runs default to 2 concurrent
processes and are capped by `FRACMAN_MCP_MAX_BATCH`.

## dfnWorks channel

dfnWorks runs inside WSL; the MCP only validates parameters, writes a driver,
starts a WSL job and parses the JSON it leaves behind.

1. Call `dfnworks_check` first. `ok=false` means fix WSL/venv/LaGriT before
   submitting anything.
2. `dfnworks_generate` returns a `job_id`; `dfnworks_mesh` and `dfnworks_graph`
   accept that id and reuse the same seed, so all three stages describe the
   same DFN.
3. `result.json` is the only reliable verdict. `errors` holds the traceback
   tail. `dfnworks_job_log` shows LaGriT output that never reaches stdout.
4. Meshing is CPU-heavy; graph is fast. Ask before resubmitting a failed mesh
   with different parameters — a `job_id` is immutable. Job records survive
   server restarts (rebuilt from `<workspace>/dfnworks/jobs/<job_id>/`); a job
   that never wrote `result.json` shows `interrupted` and can be resubmitted
   with the same `job_id` and spec to continue in place.
5. Defaults are scale-relative: `h = domain/50`, fracture radius `= domain/5`,
   three oblique constant families, `n_poly=100`. That combination was
   verified to percolate from `left` to `right` on 20 m, 100 m and 1 km cubes.
   Making `h` coarse relative to the fracture size (`h/radius >= 0.2`) ends in
   "no intersecting fractures".


## Non-negotiable safety rules

- Never close or terminate a user-owned FracMan session. `close_session`
  refuses unless the session was launched by this MCP or `force=true`.
- Live macros reject `EXIT` and `DELETEALL` unless the caller passes
  `allow_exit=true` / `allow_destructive=true`.
- Never include `LOGTOFILE` or `EXITONERROR` in a live macro; the server adds
  its own log wrapper and `EXITONERROR` would kill the user's session.
- Batch runs append `EXITONERROR` and `EXIT` automatically. Exit codes are
  meaningless: judge success from the `.fml` log and expected artifacts.
- `request_id` and `run_id` are idempotency keys. If a call returns
  `timeout_pending` or `BUSY`, query `get_request_status` / `monitor_run`.
  **Never replay an uncertain execution.**
- Work on copies of training projects. Source training data under
  `D:\BaiduNetdiskDownload\Fracman` is read-only.

## Macro basics

```
BEGIN SetSeed
Seed = 12345
END
BEGIN SetCurrentUnits
Value = "Length"
Units = "m"
END
```

- Commands are `BEGIN <Command>` … `END`; parameter names are case-insensitive.
- Strings use double quotes, numbers do not, booleans are `0`/`1`.
- Paths inside macros use forward slashes. Paths typed into FracMan file
  dialogs must use backslashes; the MCP handles that conversion.
- `#` starts a comment.
- Control flow: `BEGIN_LOOP` / `END_LOOP`; special commands `STOP`, `EXIT`,
  `BEGIN EXITONERROR`, `BEGIN LOGTOFILE`, `BEGIN RunMacro`, `BEGIN DeleteAll`.

## Reading results

- `.fml` is the only trustworthy macro verdict. An error appears as
  `#Macro Error: …` followed by `#Macro file … (line: N)`.
- `.fab` / `.ffb` are ASCII fracture files: header properties, sets, then
  fracture blocks (`id nverts setid values…`, vertices, normal).
- `.f2d` is a trace-map table (x1…z2, len, totlen, SegID, TraceID).
- `.sab` is a borehole polyline (`BEGIN Well` or `BEGIN curved_borehole`).
- `.frd` is a proprietary binary container; only heuristic object names are
  available. For numbers, export through a macro or read them in the GUI.

## Reporting

Return paths and concrete numbers. When a macro fails, quote the `.fml` error
and the line number instead of paraphrasing. When a run times out, say the
outcome is unknown and ask before any retry.

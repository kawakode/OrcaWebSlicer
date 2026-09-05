# Isolated worker executor

Status: Active groundwork contract for G3 and G4

`scripts/web_worker_executor.py` is the framework-neutral boundary between a
future API and one native slicing worker. It accepts an already prepared,
immutable job directory and starts exactly one worker for that manifest. Job
directory creation, input copying, retention, and queueing remain API-layer
work.

The executor intentionally supports only the canonical Linux container runtime.
It fails closed on other platforms instead of silently running without hard
limits. Windows development and CI invoke it through Docker.

## Process controls

Every worker starts in a new process group with standard input closed. Before
exec, the child receives hard Linux limits for address space, CPU time, output
file size, process count, open files, and core dumps. The parent independently
enforces wall time. Cancellation or a limit violation sends `SIGTERM` to the
whole process group, waits a bounded grace period, and then sends `SIGKILL` if
the worker has not exited.

The container remains a second boundary. The `executor-smoke` service runs as
an unprivileged user with no network, no capabilities, no-new-privileges, a
read-only root filesystem, private temporary storage, and a cgroup PID limit.
The final production container and sandbox choice remains a G6 decision.

## Output controls

Worker stdout is parsed continuously as UTF-8 NDJSON while stderr is drained
separately. The executor stops a job when any event line, total event volume,
event count, or log volume exceeds its configured ceiling. It continues draining
both pipes during shutdown, so a noisy worker cannot deadlock on a full pipe.
Only the bounded stderr prefix is retained.

After a normal exit, the executor requires contiguous event sequences, one last
terminal state, a matching bounded `result.json`, the documented exit code, and
safe regular files for every declared artifact. Malformed output, crashes,
timeouts, missing results, and missing artifacts become executor failures for
that job rather than exceptions in a long-lived API process.

The real-worker integration check places independent canaries in model content,
manifest-only data, and generated G-code. It verifies that none appear in the
captured event or diagnostic streams.

## Configuration

All limits are positive integers. Byte limits are raw byte counts and time
limits are milliseconds.

| Environment variable | Default | Enforcement |
| --- | ---: | --- |
| `ORCA_WEB_MAX_WALL_TIME_MS` | 300000 | Parent timer |
| `ORCA_WEB_MAX_CPU_TIME_MS` | 300000 | `RLIMIT_CPU` |
| `ORCA_WEB_MAX_MEMORY_BYTES` | 4294967296 | `RLIMIT_AS` |
| `ORCA_WEB_MAX_OUTPUT_BYTES` | 1073741824 | `RLIMIT_FSIZE` plus worker checks |
| `ORCA_WEB_MAX_PROCESSES` | 64 | `RLIMIT_NPROC` plus container PID limit |
| `ORCA_WEB_MAX_OPEN_FILES` | 128 | `RLIMIT_NOFILE` |
| `ORCA_WEB_TERMINATION_GRACE_MS` | 5000 | Delay between `SIGTERM` and `SIGKILL` |
| `ORCA_WEB_MAX_EVENT_BYTES` | 1048576 | Total worker stdout |
| `ORCA_WEB_MAX_EVENT_LINE_BYTES` | 65536 | One NDJSON record |
| `ORCA_WEB_MAX_EVENTS` | 4096 | Parsed record count |
| `ORCA_WEB_MAX_LOG_BYTES` | 1048576 | Total worker stderr |

The command-line wrapper prints one compact executor summary. A future API may
import `execute_worker` to receive the validated events and terminal result
without coupling this boundary to an API framework.

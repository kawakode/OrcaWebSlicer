# Isolated worker executor

Status: Active groundwork contract for G3 and G4

`scripts/web_worker_executor.py` is the framework-neutral boundary between the
API and one native slicing worker. It accepts an already prepared, immutable job
directory and starts exactly one worker for that manifest.
`scripts/web_job_directory.py` is its companion: it builds that directory,
stages exactly the declared files, and reclaims directories afterwards. Queueing
remains API-layer work.

Both modules stay in `scripts/` and are imported by the API, per
[ADR 0002](adr/0002-web-stack.md). They must not depend on the API package,
because the smoke and baseline services also drive them.

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

## Sandbox

Resources are bounded above; everything else a worker must not reach is
confined by `scripts/web_worker_sandbox.py`, which the executor applies in the
child between `fork` and `exec`. [ADR 0003](adr/0003-worker-sandbox.md) records
why each control uses the mechanism it does, and what the runtime measurably
refused.

| Control | What the worker gets |
| --- | --- |
| `network` | A seccomp filter fails `socket()` with `EAFNOSUPPORT` for every address family but `AF_UNIX` |
| `no_new_privileges` | `PR_SET_NO_NEW_PRIVS`, which also lets an unprivileged process install that filter |
| `capabilities` | Ambient set cleared, bounding set dropped |
| `user` | Never root: a privileged executor switches to `ORCA_WEB_WORKER_USER`, an unprivileged one keeps the account it has |
| `private_tmp` | `TMPDIR` and `HOME` inside the job directory, `0700` |
| `environment` | Rebuilt from an allowlist: `PATH`, `LANG`, `LC_ALL`, `TZ`, `ORCA_SLICER_RESOURCES`, and `ORCA_WEB_MAX_*` |

The applied controls are reported in the executor summary, so a deployment can
assert what actually held rather than what was configured.

`required` is the default and fails closed: a control that cannot be applied
raises before the worker starts, as `sandbox_network_denial_unavailable`,
`sandbox_privileged_executor` (a root executor that named no worker user),
`sandbox_user_unavailable`, `sandbox_job_directory_unreachable` (a state root
the worker user cannot traverse), `sandbox_unsupported_platform`, or
`sandbox_unsupported_architecture`. `off` applies nothing and exists for hosts
that cannot provide the Linux controls at all.

A root executor keeps four capabilities to build that sandbox and one to see
its results: `CAP_CHOWN` to hand the job directory over, `CAP_SETUID` and
`CAP_SETGID` to switch the worker, `CAP_SETPCAP` to drop the worker's bounding
set, and `CAP_DAC_OVERRIDE` to read back what the worker published into a
directory it now owns.

Switching users hands the job directory over with it: every staged file becomes
read-only and owned by the worker user, and the directory itself stays `0700`,
so the handover narrows who can reach the job rather than widening it. A job
directory containing a symbolic link is refused rather than followed.

One worker uid is shared by concurrent jobs, so a compromised worker could read
a concurrently running job's directory. ADR 0003 records that residual risk and
what closing it would take.

## Output controls

Worker stdout is parsed continuously as UTF-8 NDJSON while stderr is drained
separately. The executor stops a job when any event line, total event volume,
event count, or log volume exceeds its configured ceiling. It continues draining
both pipes during shutdown, so a noisy worker cannot deadlock on a full pipe.
Only the bounded stderr prefix is retained.

A caller may pass an event observer to `execute_worker` to receive each parsed
event as it arrives, which is how the API reports live progress. It runs on the
reader thread, so it must not block; an observer that raises fails the job
rather than stalling the pipe it is draining.

After a normal exit, the executor requires contiguous event sequences, one last
terminal state, a matching bounded `result.json`, the documented exit code, and
safe regular files for every declared artifact. Malformed output, crashes,
timeouts, missing results, and missing artifacts become executor failures for
that job rather than exceptions in a long-lived API process.

The real-worker integration check places independent canaries in model content,
manifest-only data, and generated G-code. It verifies that none appear in the
captured event or diagnostic streams.

## Job directory lifecycle

Every request gets its own directory named by its `job_id`, created `0700` with
`O_EXCL` semantics. Reusing a `job_id` is the stable `job_directory_exists`
failure rather than a silent overwrite, so one job can never observe or
overwrite another's files.

Only files the request declares are staged: the input model and the resolved
machine and process profiles, plus one flattened profile per filament slot the
request names. Each declared destination must be a
relative POSIX path inside the job; absolute paths, `..`, backslashes, repeated
separators, paths crossing a symbolic link, a duplicate destination, and
anything named `request.json` are rejected. Sources are copied through a bounded
stream, so a file that grows during staging cannot exceed its allowance, and
only regular files are accepted.

The manifest is written last, to a temporary name and then renamed. A directory
that holds `request.json` is therefore always fully staged, and any staging
failure removes the entire directory rather than leaving a partial job.

Reclamation is by explicit sweep. A directory is removed once its last
modification is older than the retention window, which also covers directories
abandoned by a crash before their manifest was written. Job IDs the caller
names are never swept, so a long, quiet slice is safe. Stray files and links in
the job root are never jobs and are removed on sight. The sweep reports the
bytes each removed entry held.

The API names every job it still holds a record for. It expires those jobs
itself, counted from when each finished, so last modification only decides the
fate of directories no record claims. The API also audits each deletion; see
[api.md](api.md#retention-and-deletion-audit).

## Configuration

All limits are positive integers. Byte limits are raw byte counts and time
limits are milliseconds.

| Environment variable | Default | Enforcement |
| --- | ---: | --- |
| `ORCA_WEB_MAX_INPUT_BYTES` | 262144000 | Largest single staged file |
| `ORCA_WEB_MAX_TOTAL_INPUT_BYTES` | 536870912 | All staged files in one job |
| `ORCA_WEB_MAX_JOB_INPUTS` | 16 | Declared files in one job |
| `ORCA_WEB_JOB_RETENTION_SECONDS` | 86400 | Retention window: after a job finishes, after an upload is created, or after an unclaimed directory was last modified |

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

| Environment variable | Default | Enforcement |
| --- | --- | --- |
| `ORCA_WEB_WORKER_SANDBOX` | `required` | `required` fails closed, `off` applies nothing |
| `ORCA_WEB_WORKER_USER` | unset | Numeric `uid[:gid]`; required of a root executor, ignored by an unprivileged one |

The command-line wrapper prints one compact executor summary. The
[API](api.md) imports `execute_worker` directly and receives the validated
events and terminal result, so this boundary stays free of any API framework.

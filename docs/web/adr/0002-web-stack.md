# ADR 0002: Minimum web stack

Date: 2026-09-06
Status: Accepted

## Context

[ADR 0001](0001-browser-ui-native-workers.md) fixed the four boundaries but
deliberately deferred the frontend and service frameworks until their decision
inputs existed. G3 has now produced those inputs.

The worker contract, the isolated executor, and the job-directory lifecycle are
implemented and tested. Two of them, `scripts/web_worker_executor.py` and
`scripts/web_job_directory.py`, are already framework-neutral Python modules
that hold the security-critical logic: hard `RLIMIT` application, process-group
termination, NDJSON event validation, terminal-contract checking, path
containment, staged-input limits, and retention sweeping. They are exercised by
31 tests in `tests/web/` on every `executor-smoke` run.

That is the decisive input. Whatever the API is written in, it must drive those
two modules. Anything other than Python either re-implements them in a second
language or invokes them as an extra subprocess and re-parses their output.

The frontend's hardest work is not this gate. It is the G5 single-plate plater
and browser layer preview, which are 3D rendering problems.

## Decision

### API: Python with FastAPI

The API imports `web_worker_executor` and `web_job_directory` directly. There is
no second job contract and no reimplementation of the isolation logic in another
language.

FastAPI supplies request-schema validation and a generated OpenAPI document,
which PLAN item 8 requires. `python3` is already in the build image and already
runs the smoke, baseline, and parity tooling.

ADR 0001's rule stands and is now enforceable by construction: the API process
never links `libslic3r` and never parses model geometry. Untrusted geometry is
touched only by a disposable worker process inside its own job directory.

### Frontend: React with TypeScript, built by Vite

`react-three-fiber` and `drei` are the best-supported route to the G5 plater and
layer preview, and the React ecosystem is the largest for the generated settings
UI that PLAN item 11 needs. Vite is the build tool.

The G4 screen stays intentionally small: upload, bundled profile selection,
progress, warnings, and download. No plater, no preview.

### Local development layout

```text
web/
  api/          FastAPI service; imports the scripts/ modules
  frontend/     React + TypeScript + Vite
```

The executor and job-directory modules stay in `scripts/`. They are shared by
the API, the smoke services, and the baseline tooling, and none of those may
depend on the API package.

### Still deferred

Database, queue, and object-store products remain unselected. Local job
throughput and artifact sizes have not been measured, so selecting them now
would be speculation. The MVP keeps job state and artifacts on the job-directory
filesystem behind a narrow interface that a later store can replace.

## Consequences

### Positive

- The security-critical isolation code has one implementation, in one language,
  already under test.
- No cross-language job contract to version, serialize, or keep in sync.
- OpenAPI schema and request validation come with the framework.
- The 3D-heavy G5 work sits on the best-supported rendering stack.

### Costs

- Two languages in the service tier (C++ worker, Python API) plus TypeScript in
  the browser.
- Python's GIL makes the API unsuitable for CPU-bound work. This is acceptable
  and intended: all CPU-bound work is in the worker process by design.
- React carries more boilerplate and a larger bundle than Svelte.

## Rejected alternatives

### Node with Fastify

Rejected. Sharing one language with the frontend is real but smaller than the
cost it creates here: the executor and job-directory modules would have to be
ported to TypeScript, or spawned as an extra subprocess whose JSON summary is
re-parsed. Porting duplicates the exact logic that must not drift; spawning adds
a process layer and loses the typed `ExecutionResult` the modules already
return.

### Go with net/http

Rejected for the same re-implementation cost, and it adds a third language
alongside C++ and Python. Its supervision primitives are good, but the
supervision problem is already solved and tested in Python.

### Svelte or Vue

Rejected on plater risk rather than on the frameworks themselves. Both are
capable and produce smaller bundles. `TresJS` and plain `three.js` bindings are
less battle-tested than `react-three-fiber` for the manipulation, picking, and
large-toolpath rendering that G5 requires.

### Deferring the frontend choice again

Rejected. The inputs are available now, and leaving it open would block PLAN
item 9 at the moment work reaches it.

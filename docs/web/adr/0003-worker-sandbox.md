# ADR 0003: Worker sandbox and production isolation model

Date: 2026-09-14
Status: Accepted

## Context

G6 opens with one requirement: a worker runs as an unprivileged user, with a
read-only root filesystem, private temporary storage, no network, dropped
capabilities, and bounded resources. Through G5 only the last of those was
real. `scripts/web_worker_executor.py` applied hard `RLIMIT`s and killed the
process group, but the worker otherwise inherited everything the API had: its
user, its environment, its network, and a shared `/tmp`.

The container was the only other boundary, and it was applied to one test
service. `executor-smoke` runs read-only, unprivileged, capability-free and
network-free — but the API does not, and cannot: it has to listen on a socket
and it starts as root.

So the question this ADR answers is which layer confines the worker, given that
the service that launches it is deliberately less confined than the worker
should be.

### What the runtime actually permits

Namespaces were the obvious mechanism and are not available. Measured inside
the canonical build image, as **root**, with Docker's default profile:

| `unshare` flag | Result |
| --- | --- |
| `CLONE_NEWNET` | `EPERM` |
| `CLONE_NEWUSER` | `EPERM` |
| `CLONE_NEWUSER \| CLONE_NEWNET` | `EPERM` |
| `CLONE_NEWNS` | `EPERM` |

The default seccomp profile refuses every one of them without `CAP_SYS_ADMIN`,
and granting `CAP_SYS_ADMIN` to the API to let it build a sandbox would hand the
API more privilege than the sandbox removes.

Installing a seccomp filter is the opposite case. `prctl(PR_SET_SECCOMP,
SECCOMP_MODE_FILTER)` needs no capability at all once `PR_SET_NO_NEW_PRIVS` is
set, it is permitted by the default profile, and filters stack, so the one the
runtime already applied stays in force underneath.

## Decision

### Two layers, with the executor responsible for the worker

Per-process confinement is applied by the executor, in the child, between
`fork` and `exec`, by `scripts/web_worker_sandbox.py`:

| Control | Mechanism |
| --- | --- |
| No network | A seccomp filter that fails `socket()` with `EAFNOSUPPORT` for every address family except `AF_UNIX` |
| No new privileges | `PR_SET_NO_NEW_PRIVS` |
| No capabilities | Ambient set cleared and the bounding set dropped; a privileged executor must succeed, an unprivileged one has none to hold |
| Unprivileged user | `setgroups`/`setgid`/`setuid` to `ORCA_WEB_WORKER_USER`, with the job directory handed to it |
| Private temporary storage | `TMPDIR` and `HOME` point inside the job directory |
| No inherited secrets | The environment is rebuilt from an allowlist |
| Bounded resources | The `RLIMIT`s the executor already applied |

Denying `socket()` is enough to deny the network because the worker starts with
closed standard input and no inherited descriptors, so it has no socket it did
not create itself. `AF_UNIX` stays open: glibc resolves users and groups over
one, and a local socket reaches nothing outside the container.

Deployment-level controls stay the container's job, because a process cannot
give itself a read-only root filesystem without the namespaces above: read-only
root, private `/tmp`, a PID limit, dropped capabilities, `no-new-privileges`,
and cgroup memory and CPU bounds. `docker/web/compose.yml` applies them to
`executor-smoke`.

### Fail closed

`ORCA_WEB_WORKER_SANDBOX` defaults to `required`: a control that cannot be
applied fails the job with a stable error before the worker starts, rather than
running it with more reach than the deployment promised. `off` exists for hosts
that cannot provide the Linux controls at all and is never the default.

A root executor **must** name `ORCA_WEB_WORKER_USER`; refusing to start workers
as root is the point of the gate, so there is no silent fallback.

### The production shape

One long-lived API container, started as root and holding a network, launching
each worker as an unprivileged uid inside a disposable job directory it owns for
the length of the job. `sandbox-smoke` is exactly that shape, deliberately
keeping the container's network so that the worker's own refusal to reach it is
worth measuring.

## Consequences

### Positive

- The confinement travels with the executor, so it holds wherever a worker
  runs: the API, the smoke services, the baseline lanes, and a developer's
  shell, without each one having to be deployed correctly first.
- A compromised worker has no network, no capabilities, no privileges to gain,
  no access to the service's environment, and no shared temporary directory.
- It works under the runtime as configured, with no extra capability grants,
  no privileged container, and no custom seccomp profile to distribute.

### Costs

- A BPF program is assembled by hand. It is small, it is covered by tests that
  read `/proc/self/status` and probe every address family from inside a real
  worker, and it fails closed if the kernel refuses it.
- Linux and `x86_64`/`aarch64` only. The executor was already Linux-only.
- The worker cannot use the network even for a legitimate purpose. It has none.

### Residual risk, recorded rather than hidden

One worker uid is shared by every concurrent job. Job directories are `0700`
owned by that uid, so a compromised worker could read a concurrently running
job's directory. Nothing persists between jobs, and a finished job's directory
is swept, so this is bounded by concurrency rather than by time. Closing it
needs either a distinct uid per concurrent job slot or one container per job;
that choice belongs with the production hosting decision, which is still open.

### Two engine defects this surfaced

Neither was caused by the sandbox; both were found by running a worker without
privileges for the first time.

- The 3MF importer writes a backup tree under `temporary_dir()`, which only the
  desktop ever set. Unset, it resolved to `/orcaslicer_model` at the filesystem
  root: writable by a root worker, shared between jobs, and unwritable for an
  unprivileged one. The worker now sets the process temporary directory, which
  the sandbox points inside the job.
- Boost.Log installs its default sink on the first record when no sink is
  registered, and in this build that sink writes to **stdout** — the stream the
  worker protocol owns. A single engine diagnostic therefore corrupted the
  event stream. The worker now registers a stderr sink before anything can log,
  which is what [worker-protocol.md](../worker-protocol.md) always said the
  transport was.

## Rejected alternatives

### Network and mount namespaces

Rejected on measurement, not preference. Every `unshare` flag returns `EPERM`
under the canonical runtime, including as root. It would work only in a
privileged container, which trades a larger privilege for a smaller one.

### One container per job

Not rejected on merit; deferred with the hosting decision. It is the strongest
isolation available and it would close the shared-uid risk above, but it binds
the executor to a container runtime's API and a per-job image, and the runtime
has not been selected. The sandbox is written to compose with it rather than to
replace it: a per-job container would still start its worker through the same
executor and keep the same in-process controls.

### gVisor or Kata

Rejected for now, for the same reason: they are hosting decisions. Both are
compatible with everything above.

### Denying `connect`, `bind`, `sendto` and the rest instead of `socket`

Rejected as strictly weaker and larger. Filtering the one call that creates a
socket covers every later operation on it, and covers `AF_NETLINK` and
`AF_PACKET` with the same instruction.

### Trusting the container alone

Rejected. It is what G5 shipped, and it means the worker is exactly as confined
as the service that launched it — which, for the API, is not confined at all.

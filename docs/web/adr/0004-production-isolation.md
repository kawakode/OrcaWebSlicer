# ADR 0004: Production isolation on one host

Date: 2026-11-04
Status: Accepted

## Context

Step 14 of the roadmap asks for workers that run unprivileged, with a
read-only root, private temporary storage, no network, dropped capabilities,
and bounded resources, and for a documented isolation model. The deployment
target is one self-managed Linux VPS running Docker, not a cluster.

The API already starts each job as a separate worker process through the
executor, which applies per-process limits (wall time, CPU time, address
space, output size, open files) and kills the process on overrun. The open
question is what surrounds those processes.

Three shapes were considered:

- **Workers inside the API container**, with the container itself hardened.
- **A container per job**, started by the API through the Docker socket.
- **A per-job sandbox inside the container** (bubblewrap, nsjail) or a
  sandboxed runtime (gVisor), giving each job its own namespaces.

## Decision

Workers stay child processes of the API, and the API container is the
isolation boundary: uid 10001, every capability dropped, `no-new-privileges`,
a read-only root with a tmpfs `/tmp`, bounded memory, CPUs and PIDs, and
membership of an `internal` Docker network only, so neither the API nor any
worker has a route out of the host. Only the TLS proxy is reachable from
outside and can reach the internet.

## Rationale

- **No Docker socket in the API.** A container per job needs the API to
  control the Docker daemon, which is root on the host; a compromise of the
  API would then be a compromise of the server. That is a worse trade than the
  one it fixes.
- **Per-job namespaces need privileges the container does not have.**
  bubblewrap and nsjail create user and mount namespaces, which Docker's
  default seccomp profile refuses to an unprivileged container. Allowing them
  means a custom seccomp profile or `CAP_SYS_ADMIN`, again widening the outer
  boundary.
- **The executor already bounds each job.** Resource exhaustion, the
  realistic failure, is caught per job; the container limits catch what the
  per-process limits cannot (total memory, total PIDs).
- **No network is enforced by Docker, not by the application.** The internal
  network has no gateway, so even a compromised worker cannot exfiltrate data
  or fetch a payload.

## Consequences

- A worker that escaped its process limits shares the API's user and state
  volume: it could read or change other jobs' files and signal the API, but
  could not leave the container or reach the network. A crash or limit
  failure remains contained to its job, as before.
- Per-job namespaces (bubblewrap with a seccomp profile allowing them, or
  gVisor as the runtime of the `api` service) remain the next step if the
  service is opened to untrusted users, and can be added without changing the
  API or the worker.

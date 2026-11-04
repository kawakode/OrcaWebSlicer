# Production deployment

OrcaWebSlicer runs in production as two containers on one Linux host, defined
by [docker/web/compose.prod.yml](../../docker/web/compose.prod.yml):

```
internet ── :80/:443 ── proxy (Caddy) ──[backend, internal network]── api ── worker processes
```

- **proxy** is Caddy. It obtains and renews the HTTPS certificate by itself,
  redirects HTTP to HTTPS, asks for the site login, adds security headers, and
  forwards every request to the API.
- **api** is the production image, `ghcr.io/kawakode/orcawebslicer-web`. It
  serves the browser screen and the [API](api.md), and starts one disposable
  native worker process per job through the [executor](executor.md).

[docker/web/deploy.sh](../../docker/web/deploy.sh) wraps every procedure below.
Each section also gives the plain Compose commands it runs.

## The image

CI builds the image in `web.yml`'s `image` job from
[Dockerfile.prod](../../docker/web/Dockerfile.prod), only after the worker has
passed its smoke, matrix, parity, and browser checks. The worker binary is the
one those checks ran; the image does not recompile it. The job then:

1. writes an SPDX software bill of materials of the image (Syft), kept as the
   `orca-web-sbom.spdx.json` workflow artifact;
2. scans that SBOM (Grype) and fails on any critical vulnerability that has a
   fix; lower severities are listed in the log;
3. starts `compose.prod.yml` exactly as deployed, on `localhost`, and runs
   [scripts/test_web_deployment.py](../../scripts/test_web_deployment.py):
   HTTP to HTTPS redirect, TLS verified against Caddy's CA, login required,
   security headers, an upload sliced to G-code, and the API container's user,
   read-only root, and lack of network;
4. on `main` only, publishes the image as `sha-<12-character commit>`, which
   never moves, and `main`, which follows the branch.

The image's `org.opencontainers.image.revision` label records the commit.

Building the image by hand needs a worker binary at
`build-worker/src/Release/orca-slicer-worker` (the `worker-build` service of
[compose.yml](../../docker/web/compose.yml), or the `orca-slicer-worker`
artifact of a `web.yml` run):

```bash
docker build -f docker/web/Dockerfile.prod --build-arg GIT_COMMIT="$(git rev-parse HEAD)" \
  -t ghcr.io/kawakode/orcawebslicer-web:local .
```

## Isolation model

The production isolation model is a hardened container around the API and its
workers; see [ADR 0004](adr/0004-production-isolation.md) for why.

| Control | Where | Setting |
| --- | --- | --- |
| Unprivileged user | image, Compose | uid 10001 for the API and every worker |
| No privilege gain | Compose | `cap_drop: ALL`, `no-new-privileges` |
| Read-only root | Compose | `read_only: true`; writable only `/tmp` (1 GB tmpfs) and the state volume |
| No network | Compose | the API joins only `backend`, an `internal` network with no route out; it can answer the proxy but reach nothing else |
| Bounded container | Compose | `mem_limit`, `cpus`, `pids_limit`, `nofile` |
| Bounded worker | executor | wall time, CPU time, address space, output size, open files, events, and log size per job |
| Disposable job | executor | one process and one job directory per job, removed after the retention window |

The proxy also runs read-only with every capability dropped except binding
ports 80 and 443. It is the only container on the `edge` network, which it
needs for Let's Encrypt.

Two settings differ from development on purpose:

- `ORCA_WEB_MAX_PROCESSES` is 4096. The executor sets it as `RLIMIT_NPROC`,
  which Linux counts across every thread of the user, uvicorn's thread pool
  included; the executor's default of 64 would starve a worker. The
  container's `pids_limit` (1024) is the effective bound.
- uvicorn trusts `X-Forwarded-*` from any address, because only the proxy can
  reach it.

## Prepare the server (once)

Any 64-bit x86 Linux host with Docker Engine 24 or later and the Compose
plugin. Plan 2 vCPU and 8 GB of memory for the default two concurrent jobs,
and 20 GB of disk.

1. **DNS.** Create an `A` record (and `AAAA` for IPv6) for the chosen name,
   such as `slicer.example.com`, pointing at the server. Let's Encrypt checks it
   before it issues the certificate.
2. **Firewall.** Allow inbound 22/tcp, 80/tcp, 443/tcp, and 443/udp (HTTP/3),
   nothing else. Docker publishes only the proxy's 80 and 443. With `ufw`:
   `ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443 && ufw enable`.
3. **Docker.** Install it from Docker's repository
   (<https://docs.docker.com/engine/install/>), then check
   `docker compose version`.
4. **Deployment files.** Only `docker/web/` is needed on the server:

   ```bash
   git clone --depth 1 https://github.com/kawakode/OrcaWebSlicer.git /opt/orcawebslicer
   cd /opt/orcawebslicer/docker/web
   ```

5. **Registry access.** If the GitHub package is private, create a GitHub
   personal access token (classic) with only `read:packages`, then
   `docker login ghcr.io -u <github-login>` with the token as password. A public
   package needs no login.

## Install

```bash
./deploy.sh init     # writes .env: domain, e-mail, login, password
./deploy.sh up       # pulls the images, starts both containers, waits until healthy
```

By hand: copy `.env.example` to `.env` (mode 600), fill it in, set
`ORCA_WEB_PASSWORD_HASH='<hash>'` from
`docker run --rm -i caddy:2.10.2-alpine caddy hash-password`, then
`docker compose -f compose.prod.yml up -d --wait`.

The first start takes a minute or two: the API reads the engine's settings
catalog and evaluates the bundled profiles before it reports healthy, and Caddy
requests the certificate. Open `https://<domain>/` and log in.

Settings live in `.env`; [.env.example](../../docker/web/.env.example) lists
the usual ones, and every limit in [executor.md](executor.md) and
[api.md](api.md#configuration) that `compose.prod.yml` passes through can be
set there too. Apply a change with `./deploy.sh up`.

## Update

Use the immutable tag of the commit to deploy, which `web.yml`'s summary shows:

```bash
./deploy.sh update sha-0123456789ab
```

It records the running tag in `.deploy-history`, sets the new one, pulls it,
and recreates the API. If the new API does not become healthy within five
minutes, it puts the previous tag back and restarts it, then exits with an
error. By hand: change `ORCA_WEB_TAG` in `.env`, then
`docker compose -f compose.prod.yml pull && docker compose -f compose.prod.yml up -d --wait`.

An update interrupts work in progress. On `SIGTERM` the API stops accepting
requests, cancels running workers through the executor, and exits; a running
slice ends `canceled` and the browser must resubmit it. Jobs live in the API
process, so the new process lists none of the old ones, and the startup sweep
removes their directories. Update when nobody is slicing.

To refresh the deployment files themselves (`compose.prod.yml`, `Caddyfile`,
`deploy.sh`), `git pull` in `/opt/orcawebslicer`, then `./deploy.sh up`.

## Roll back

```bash
./deploy.sh rollback                    # the tag before the current one
./deploy.sh rollback sha-0123456789ab   # or any earlier tag
```

Images on GHCR are kept, so any earlier `sha-` tag remains deployable. Nothing
needs migrating back: the state volume holds only disposable uploads and jobs.

## Monitor

```bash
./deploy.sh status        # configured tag, its commit, container health
./deploy.sh logs api      # or proxy
```

Both containers have Docker health checks and `restart: unless-stopped`, so a
crashed process is restarted and an unhealthy one shows in `status`. Logs are
rotated by Docker at 5 × 10 MB per container. The proxy writes JSON access logs;
the API logs every job with its correlation ID. Metrics and alerts come with
step 16 of the [roadmap](../../PLAN.md).

## Back up and recover

| Data | Where | Back up? |
| --- | --- | --- |
| Settings and password hash | `docker/web/.env` | Yes: copy it somewhere safe after each change |
| Deployed tag history | `docker/web/.deploy-history` | Optional |
| Certificates, ACME account | volume `orca-web_caddy-data` | Optional: Caddy issues new ones (mind Let's Encrypt's rate limit of 5 identical certificates a week) |
| Uploads, jobs, artifacts | volume `orca-web_orca-state` | No: deleted after 24 hours by design |

To recover on a new server: prepare it as above, restore `.env`, point DNS at
it, and run `./deploy.sh up`. To recover from a broken host state on the same
server, `docker compose -f compose.prod.yml down` then `./deploy.sh up`; add
`--volumes` to `down` only to discard the certificates and job state as well.

## Security notes and known limits

- The site login is one shared HTTP basic-auth account in Caddy, checked
  before any request reaches the API. Accounts, quotas, and rate limits are
  step 15 of the roadmap.
- Workers share the API container, its user, and its state volume. A worker
  that escaped the executor's limits could read or alter other jobs' files and
  the API process, but nothing outside the container, and it has no network.
  See [ADR 0004](adr/0004-production-isolation.md).
- The scan blocks only on fixable critical findings. Review the scan log and
  the SBOM before each public release (step 17).
- AGPLv3 section 13 requires offering the corresponding source to users of the
  hosted service; the source link in the application is step 17 and must be in
  place before the site is opened to anyone else.

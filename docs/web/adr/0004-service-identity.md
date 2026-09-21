# ADR 0004: Service identity and authorization boundary

Date: 2026-09-16
Status: Accepted for implementation

## Context

G6 service controls begin with authentication and authorization, but the
production host and identity provider are intentionally not selected yet. The
browser still needs a real session flow, while the API needs a stable identity
contract that does not change when the deployment chooses one OIDC provider,
managed ingress, or forward-auth product over another.

The current API has no identity concept. Anyone who can reach it can upload a
model, list every in-memory job, read any job whose ID they know, cancel it,
retry it, or download its artifacts. Adding quotas or deletion auditing before
ownership would merely account shared anonymous state.

## Decision

### Authentication lives at the edge; verification remains in the API

An authenticating reverse proxy owns the browser login, OIDC authorization-code
flow with PKCE, session cookie, logout, and provider-specific claim mapping. It
injects a short-lived signed JWT identity assertion into the upstream request.
The API verifies that assertion for every protected route.

The assertion is provider-neutral and contains only:

- `iss`: the configured edge issuer;
- `aud`: the OrcaWebSlicer API audience;
- `sub`: a stable opaque user ID, not an email address or display name;
- `iat`, `nbf`, and `exp`: a short validity window;
- `jti`: a unique assertion ID;
- `roles`: reserved for a later, explicitly scoped administrator or service
  identity contract.

The API verifies signature, algorithm allowlist, issuer, audience, time claims,
claim types, and size before constructing a principal. Public verification keys
come from a read-only local file mounted with the release configuration. The API
does not fetch keys over the network in a request path, and a key rotation file
may contain both the retiring and replacement key during a bounded overlap.

The proxy must remove every client-supplied identity header and inject its own
`Authorization: Bearer` assertion. TLS, browser cookie security, and login CSRF
protection remain the proxy's responsibility. The API listener must not be
published around that proxy in an authenticated deployment.

The edge is also the initial policy mapper: it decides which authenticated
identities may enter the service, while the API treats every valid principal as
a normal user. The first implementation has no privileged role semantics.

For unsafe browser requests, the edge must reject a missing or mismatched
`Origin` header using an exact configured-origin comparison. This complements
`SameSite`, `Secure`, and `HttpOnly` session-cookie settings; none of those
controls replaces the signed assertion verified by the API.

### Secure mode is the application default

`ORCA_WEB_AUTH_MODE` has two values:

- `required`, the application default and the only production mode;
- `disabled`, an explicit local-development and isolated-test mode that creates
  the fixed principal `local-development`.

When runtime authentication lands, the reference developer Compose file will
set `disabled` visibly until an edge service is added. Readiness will report the
mode so an operator cannot mistake that topology for an authenticated
deployment. A future production Compose or host manifest must set `required`,
mount verification keys, and keep the API port private.

Liveness and readiness stay public so the runtime can manage the process.
Static frontend assets may remain public. Every `/api/v1` route other than the
three health endpoints requires a principal, including profile and settings
catalogs. The generated OpenAPI document is disabled or protected in required
mode.

Authentication failures use stable `authentication_required` responses with
HTTP 401 and `WWW-Authenticate: Bearer`. A resource that is absent or belongs to
another principal returns the same existing 404 response so identifiers cannot
be used as a cross-user existence oracle. HTTP 403 is reserved for an explicit
role or scope denial once such policy exists.

### Ownership is attached at creation and enforced at every lookup

The API derives `owner_id` as the hex SHA-256 digest of `issuer`, a NUL byte,
and `subject`. Raw identity-provider subjects, emails, and display names are not
persisted or written to application logs.

- An upload can only create scenes or slices for its owner.
- A job, scene, artifact, cancellation, and retry can only be reached by its
  owner.
- Job listing returns only the caller's jobs.
- A retry retains the original owner; a caller cannot transfer it.
- Filenames, model contents, emails, display names, and raw assertions never
  enter authorization logs.

There is no cross-user operator bypass in the first implementation. Support and
administrative access must use an explicit audited interface designed with the
observability work, rather than a broad role silently bypassing every lookup.

Upload metadata already survives restart, so `owner_id` becomes part of its
versioned on-disk record. Job ownership remains process-local until persistent
job metadata is selected. Old pre-auth uploads have no owner and are not exposed
in required mode; local disabled mode may treat them as
`local-development` during the documented transition.

An identity-provider migration must preserve the `(issuer, subject)` mapping or
perform an explicit, audited owner rebinding. Reusing a display name or email is
not identity continuity.

Runtime assertion verification and ownership enforcement must ship as one
atomic change. Protecting only some routes, or accepting principals before all
upload, scene, job, artifact, retry, cancellation, and listing lookups enforce
ownership, would create a misleading partial security boundary.

## Consequences

### Positive

- The browser gets a normal provider session without storing a long-lived API
  key or identity-provider token in application JavaScript.
- The API verifies cryptographic identity even if a routing mistake makes it
  reachable outside the edge.
- Provider selection and claim mapping can change without changing upload/job
  ownership or quota keys.
- Stable opaque subjects provide the boundary the next per-user quota batch
  needs.

### Costs and gates

- A production edge and signing-key rotation procedure are required before
  public hosting.
- JWT verification adds a small pinned cryptographic dependency and focused
  algorithm-confusion, expiry, issuer, audience, and rotation tests.
- Existing stored uploads need an explicit transition policy; they cannot be
  silently assigned to the first authenticated caller.
- Multi-replica authorization still depends on the later persistent job-state
  decision.

## Rejected alternatives

### Trust an unsigned user header

This makes one proxy-strip or network-routing mistake an authentication bypass.
Network placement remains defense in depth, not the API's only proof.

### Put static API keys in the browser

They are awkward to rotate, do not provide a safe browser session, and either
identify a whole deployment or expose a long-lived per-user secret to
JavaScript. They remain suitable only for a future separately scoped automation
client.

### Implement provider-specific OIDC in the API

It couples the service to login UI, cookies, redirect URIs, provider discovery,
and network access before a host or provider has been selected. The edge is the
better deployment-specific layer; the API still verifies the assertion it
receives.

## Implementation notes

Landed as `web/api/auth.py`, `web/api/config.py`, `web/api/app.py`,
`web/api/uploads.py`, and `web/api/jobs.py`. Concrete values not fixed above:

- The assertion algorithm allowlist is exactly `RS256`; the JWKS loader keeps
  only entries with `kty: RSA`, `use: sig` (or absent), `alg: RS256` (or
  absent), and a `kid` that is not repeated elsewhere in the file. A `kid`
  that does not resolve to exactly one such entry fails verification the same
  way an unknown `kid` does.
- The documented short lifetime is 300 seconds (`exp - iat`); clock skew
  tolerance on `exp`/`nbf`/`iat` is 30 seconds.
- An assertion over 8 KiB is refused before it is decoded.
- `ORCA_WEB_AUTH_MODE`, `ORCA_WEB_AUTH_ISSUER`, `ORCA_WEB_AUTH_AUDIENCE`, and
  `ORCA_WEB_AUTH_JWKS_PATH` are the environment variables; see
  [api.md](../api.md#authentication-and-authorization).
- Authentication is enforced by one `APIRouter(dependencies=[Depends(get_principal)])`
  that every non-health `/api/v1` route is registered on, rather than a
  per-route check, so a new route cannot ship unauthenticated by omission.

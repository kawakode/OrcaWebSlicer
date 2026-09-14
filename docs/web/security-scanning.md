# Web image and dependency scanning

The production-readiness scan inventories the canonical `orca-web-build` image
and the frontend lockfile, then checks both inventories for known
vulnerabilities. It uses Syft `1.51.1` to generate CycloneDX JSON and Grype
`0.116.1` to scan those documents. The versions are build arguments in
`docker/web/Security.Dockerfile`; update both there, rebuild the scanner image,
and review the resulting SBOM and policy changes together. The ephemeral
scanner image is based directly on the target image, so Syft inventories its
root filesystem without exporting a multi-gigabyte image through the Docker
socket. Scanner binaries and mounted input, output, cache, and virtual
filesystems are explicitly excluded from that inventory.

Build the application image before running the scan:

```powershell
docker compose -f docker/web/compose.yml build
docker compose -f docker/web/compose.yml build security-scan
docker compose -f docker/web/compose.yml run --rm security-scan
```

The scanner service has its own Compose profile so the first command builds
the target image without trying to derive the scanner image from a target that
does not exist yet. Naming `security-scan` explicitly activates it for the next
two commands.

Set `ORCA_WEB_SCAN_IMAGE` to inventory a differently tagged local image. The
scanner image must be rebuilt after changing that variable. The
scan writes these ignored diagnostic artifacts beneath
`artifacts/web-security/`:

- `image.sbom.cdx.json` and `frontend.sbom.cdx.json`: complete CycloneDX SBOMs.
- `image.grype.json` and `frontend.grype.json`: complete Grype reports,
  including findings for which no fix is currently available.

The command exits `2` when either SBOM has a fixable High or Critical finding.
Unfixed findings remain visible in the JSON reports but do not fail this gate;
they still require review before release. Scanner failures and malformed or
missing JSON artifacts exit `1` rather than being mistaken for a clean scan.
Grype needs network access to refresh its vulnerability database. The
`orca-web-grype-db` volume caches that database between runs, and
`orca-web-syft-cache` keeps Syft's cache writable while the scanner root
filesystem remains read-only. The scanner drops every Linux capability except
`DAC_OVERRIDE`, which lets Syft inventory package locations that are not
world-readable. The scanner does not receive the Docker socket.

## Coverage boundary

The image SBOM covers Ubuntu packages and language packages present in the
built image. The separate frontend SBOM covers packages declared by
`web/frontend/package-lock.json`, whose installed tree normally lives in a
Compose volume instead of the image.

Syft cannot reconstruct every custom CMake `ExternalProject` dependency from a
statically linked C++ worker. The generated image SBOM may identify binary and
dynamic-library evidence, but it is not a complete inventory of those native
source dependencies. Their pinned URLs and hashes in `deps/`, the worker
forbidden-dependency audit, and the third-party-license review remain separate
release controls until the native dependency build emits package metadata that
an SBOM cataloger can consume.

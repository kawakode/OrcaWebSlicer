ARG SYFT_VERSION=v1.51.1
ARG GRYPE_VERSION=v0.116.1
ARG TARGET_IMAGE=orca-web-build:latest

FROM ghcr.io/anchore/syft:${SYFT_VERSION} AS syft
FROM ghcr.io/anchore/grype:${GRYPE_VERSION} AS grype

FROM ${TARGET_IMAGE}

COPY --from=syft /syft /usr/local/bin/syft
COPY --from=grype /grype /usr/local/bin/grype
COPY scripts/web_security_scan.py /usr/local/bin/web-security-scan

ENTRYPOINT ["python3", "/usr/local/bin/web-security-scan"]

ARG BASE_IMAGE=ubuntu:24.04
FROM ${BASE_IMAGE}

ARG CMAKE_VERSION=4.3.0

ENV DEBIAN_FRONTEND=noninteractive
ENV CMAKE_CCACHE=ccache
ENV CI=true
ENV GITHUB_ACTIONS=true
ENV GITHUB_WORKSPACE=/workspace
ENV RUNNER_OS=Linux
ENV RUNNER_TEMP=/tmp

SHELL ["/bin/bash", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ccache \
        curl \
        python3 \
        sudo \
        tar \
    && rm -rf /var/lib/apt/lists/*

# Keep the system package list owned by OrcaSlicer's supported Linux build flow.
# The copied files form a stable image layer; normal source edits do not reinstall
# the toolchain.
COPY build_linux.sh /tmp/orcaslicer/build_linux.sh
COPY scripts/linux.d /tmp/orcaslicer/scripts/linux.d
COPY docker/web/run-build-linux.sh /usr/local/bin/run-build-linux

WORKDIR /tmp/orcaslicer

RUN sed -i 's/\r$//' /usr/local/bin/run-build-linux \
    && find ./scripts/linux.d -type f -exec sed -i 's/\r$//' {} + \
    && chmod +x /usr/local/bin/run-build-linux \
    && run-build-linux ./build_linux.sh -ur

# Match the CMake line used by the repository's Linux CI and built-in container
# runner. Falling back to Ubuntu's CMake would make local and CI baselines differ.
RUN case "$(uname -m)" in \
        x86_64|amd64) cmake_arch="x86_64" ;; \
        aarch64|arm64) cmake_arch="aarch64" ;; \
        *) echo "Unsupported CMake architecture: $(uname -m)"; exit 1 ;; \
    esac \
    && cmake_archive="cmake-${CMAKE_VERSION}-linux-${cmake_arch}.tar.gz" \
    && curl -fsSL \
        "https://github.com/Kitware/CMake/releases/download/v${CMAKE_VERSION}/${cmake_archive}" \
        -o "/tmp/${cmake_archive}" \
    && tar -xzf "/tmp/${cmake_archive}" -C /opt \
    && ln -sf "/opt/cmake-${CMAKE_VERSION}-linux-${cmake_arch}/bin/"* /usr/local/bin/ \
    && rm "/tmp/${cmake_archive}" \
    && cmake --version

RUN rm -rf /tmp/orcaslicer /var/lib/apt/lists/* \
    && mkdir -p /workspace /cache/ccache

WORKDIR /workspace

CMD ["bash"]

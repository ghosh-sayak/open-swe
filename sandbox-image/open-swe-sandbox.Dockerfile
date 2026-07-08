# Custom OpenSandbox sandbox image for the open-swe agent's TARGET workload.
#
# IMPORTANT -- this is the SANDBOX image, i.e. where the agent actually builds and
# tests the project. The agent itself (LangChain/LangGraph/deepagents, per the
# project's pyproject.toml) runs OUTSIDE the sandbox in the LangGraph API runtime and
# drives this sandbox by sending shell commands (e.g. `./gradlew test`) through
# deepagents-opensandbox (OpensandboxProvider.execute()). So this image carries the
# TARGET PROJECT toolchain, NOT the agent's Python/LangChain dependencies.
#
# Target projects are Kotlin + Gradle with unit tests, so the sandbox needs a JDK,
# Kotlin, and Gradle. We keep the python:slim base (Python is still needed to run the
# dummy verification app and any helper scripts) and add a build JDK (Temurin) plus
# Kotlin + Gradle via SDKMAN, alongside the usual coding-agent CLI tools.
#
# For code intelligence we also bake in the OFFICIAL JetBrains Kotlin LSP (the same
# IntelliJ engine your devs use). That LSP requires JDK 25 to *run*, so we install a
# separate JDK used ONLY by the LSP and leave the build JDK for Gradle untouched.
#
# OpenSandbox injects its execution daemon (execd) at runtime, so nothing
# OpenSandbox-specific is baked in here.
#
# --- Per-project configuration -------------------------------------------------
# All toolchain versions are build ARGs so you can retarget the image per project:
#
#   docker build -f open-swe-sandbox.Dockerfile -t open-swe-sandbox:latest \
#     --build-arg PYTHON_VERSION=3.12 \
#     --build-arg JAVA_VERSION=21 \
#     --build-arg GRADLE_VERSION=8.11.1 \
#     --build-arg KOTLIN_VERSION=2.3.21 \
#     --build-arg KOTLIN_LSP_VERSION=262.8190.0 \
#     --build-arg KOTLIN_LSP_JDK_VERSION=25 .
#
# JAVA_VERSION / KOTLIN_LSP_JDK_VERSION pin the JDK *major* (latest GA patch of it).
# GRADLE_VERSION / KOTLIN_VERSION must be valid SDKMAN identifiers
# (see `sdk list gradle` / `sdk list kotlin`).
# -------------------------------------------------------------------------------

ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim-bookworm

USER root

# Toolchain versions -- override any of these with --build-arg (see header).
ARG JAVA_VERSION=17
ARG GRADLE_VERSION=9.6.1
ARG KOTLIN_VERSION=2.4.0
ARG KOTLIN_LSP_VERSION=262.8190.0
ARG KOTLIN_LSP_JDK_VERSION=25

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SDKMAN_DIR=/root/.sdkman \
    JAVA_HOME=/opt/java

# System CLI + build prerequisites (the JDK is installed separately below so its
# version is controlled by JAVA_VERSION rather than pinned to Debian's package).
#   git, openssh-client -> clone/commit/push (https + ssh)
#   ripgrep, jq         -> fast codebase search + JSON on the command line
#   build-essential     -> compile native deps for pip packages
#   zip, unzip          -> required by SDKMAN, and common in JVM builds
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl wget gnupg \
      git openssh-client \
      build-essential pkg-config \
      ripgrep jq \
      zip unzip tar xz-utils \
      less vim nano file procps \
    && rm -rf /var/lib/apt/lists/*

# Build JDK (Eclipse Temurin via the Adoptium API), version pinned by JAVA_VERSION.
# This is the default `java` used by Gradle/Kotlin builds.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in amd64) a=x64 ;; arm64) a=aarch64 ;; *) echo "unsupported arch: $arch" >&2; exit 1 ;; esac; \
    mkdir -p "$JAVA_HOME"; \
    curl -fsSL "https://api.adoptium.net/v3/binary/latest/${JAVA_VERSION}/ga/linux/${a}/jdk/hotspot/normal/eclipse" -o /tmp/jdk.tar.gz; \
    tar -xzf /tmp/jdk.tar.gz -C "$JAVA_HOME" --strip-components=1; \
    rm -f /tmp/jdk.tar.gz; \
    "$JAVA_HOME/bin/java" -version
ENV PATH="$JAVA_HOME/bin:$PATH"

# GitHub CLI (gh) from GitHub's apt repo.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | \
      dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends gh && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Kotlin + Gradle via SDKMAN, versions pinned by GRADLE_VERSION / KOTLIN_VERSION.
# NOTE: most Kotlin repos ship a Gradle wrapper (./gradlew) that pins the project's
# own Gradle/Kotlin versions, so these globals are really a bootstrap.
RUN curl -s "https://get.sdkman.io?rcupdate=false" | bash && \
    bash -c "source '$SDKMAN_DIR/bin/sdkman-init.sh' && \
      sdk install gradle '${GRADLE_VERSION}' && \
      sdk install kotlin '${KOTLIN_VERSION}' && \
      sdk flush"

# SDKMAN keeps stable 'current' symlinks, so put them on PATH directly -- no need to
# source sdkman-init.sh in the non-login shells the agent's execute() uses.
ENV PATH="$SDKMAN_DIR/candidates/gradle/current/bin:$SDKMAN_DIR/candidates/kotlin/current/bin:$PATH"

# --- Official JetBrains Kotlin LSP for code intelligence ---
# Dedicated JDK for the LSP only (Temurin via Adoptium API), pinned by
# KOTLIN_LSP_JDK_VERSION. Kept separate so the build JDK for Gradle stays untouched.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in amd64) a=x64 ;; arm64) a=aarch64 ;; *) echo "unsupported arch: $arch" >&2; exit 1 ;; esac; \
    mkdir -p /opt/jdk-lsp; \
    curl -fsSL "https://api.adoptium.net/v3/binary/latest/${KOTLIN_LSP_JDK_VERSION}/ga/linux/${a}/jdk/hotspot/normal/eclipse" -o /tmp/jdk-lsp.tar.gz; \
    tar -xzf /tmp/jdk-lsp.tar.gz -C /opt/jdk-lsp --strip-components=1; \
    rm -f /tmp/jdk-lsp.tar.gz; \
    /opt/jdk-lsp/bin/java -version

# Official standalone Kotlin LSP archive (JetBrains CDN), pinned by KOTLIN_LSP_VERSION.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) lsp_file="kotlin-server-${KOTLIN_LSP_VERSION}.tar.gz" ;; \
      arm64) lsp_file="kotlin-server-${KOTLIN_LSP_VERSION}-aarch64.tar.gz" ;; \
      *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    mkdir -p /opt/kotlin-lsp; \
    curl -fsSL "https://download-cdn.jetbrains.com/language-server/kotlin-server/${KOTLIN_LSP_VERSION}/${lsp_file}" -o /tmp/kotlin-lsp.tar.gz; \
    tar -xzf /tmp/kotlin-lsp.tar.gz -C /opt/kotlin-lsp; \
    rm -f /tmp/kotlin-lsp.tar.gz; \
    lsp_script="$(find /opt/kotlin-lsp -maxdepth 3 -name kotlin-lsp.sh | head -n1)"; \
    test -n "$lsp_script"; \
    chmod +x "$lsp_script"; \
    ln -sf "$lsp_script" /usr/local/bin/kotlin-lsp.sh

# Wrapper so the LSP runs on its dedicated JDK without disturbing the build JDK.
# The agent's LSP client invokes `kotlin-lsp --stdio` (or a socket flag; see --help).
RUN printf '#!/bin/sh\nexport JAVA_HOME=/opt/jdk-lsp\nexport PATH="/opt/jdk-lsp/bin:$PATH"\nexec /usr/local/bin/kotlin-lsp.sh "$@"\n' > /usr/local/bin/kotlin-lsp && \
    chmod +x /usr/local/bin/kotlin-lsp

# The agent always invokes `GH_TOKEN=dummy gh` (a LangSmith-proxy convention). On
# providers without that proxy, the dummy token would override the real one in
# ~/.config/gh/hosts.yml and 401. /usr/local/bin precedes /usr/bin in PATH, so this
# wrapper shadows the real gh (kept at /usr/bin/gh) and strips the dummy token,
# letting gh fall back to hosts.yml. No recursion, real binary untouched.
RUN printf '#!/bin/sh\nunset GH_TOKEN GITHUB_TOKEN\nexec /usr/bin/gh "$@"\n' > /usr/local/bin/gh && \
    chmod +x /usr/local/bin/gh

# Agents clone repos into /workspace as root; avoid git "dubious ownership" errors.
RUN git config --system --add safe.directory '*'

WORKDIR /workspace

CMD ["bash"]

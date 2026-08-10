FROM node:20-bookworm-slim

ARG DEBIAN_FRONTEND=noninteractive
ARG POLYGLOT_REVISION=7e0611e77b54e2dea774cdc0aa00cf9f7ed6144f

LABEL org.opencontainers.image.title="Codini Polyglot Python/JavaScript benchmark"
LABEL org.opencontainers.image.description="Python and JavaScript toolchains for isolated Codini Polyglot evaluation"
LABEL org.opencontainers.image.source="https://github.com/Aider-AI/polyglot-benchmark"
LABEL io.codini.polyglot.revision="${POLYGLOT_REVISION}"
LABEL io.codini.polyglot.agent="true"

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# 仅安装 Python 题目所需运行时；Node.js 和 npm 由基础镜像直接提供。
RUN apt_install() { \
        for attempt in 1 2 3; do \
            if apt-get -o Acquire::Retries=3 install -y --no-install-recommends "$@"; then \
                return 0; \
            fi; \
            echo "apt install failed (attempt ${attempt}/3); retrying missing packages..." >&2; \
            sleep 3; \
        done; \
        return 1; \
    } \
    && rm -f /etc/apt/apt.conf.d/docker-clean \
    && apt-get -o Acquire::Retries=3 update \
    && apt_install \
        ca-certificates \
        git \
        python3 \
        python3-pip \
        python3-pytest \
        python3-venv \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# JavaScript 测试依赖与 Aider 官方 Polyglot benchmark 环境对齐。
RUN mkdir -p /opt/polyglot-js \
    && cd /opt/polyglot-js \
    && npm init -y \
    && npm install --no-audit --no-fund \
        jest@29.7.0 \
        @babel/core@7.25.2 \
        @exercism/babel-preset-javascript@0.2.1 \
        @exercism/eslint-config-javascript@0.6.0 \
        @types/jest@29.5.12 \
        @types/node@20.12.12 \
        babel-jest@29.6.4 \
        core-js@3.37.1 \
        eslint@8.49.0

ENV NODE_PATH="/opt/polyglot-js/node_modules"
ENV PATH="/opt/codini-venv/bin:/opt/polyglot-js/node_modules/.bin:${PATH}"

# 将当前仓库的 Codini 安装进评测镜像，Agent 不再从宿主机虚拟环境启动。
COPY pyproject.toml LICENSE /opt/codini-src/
COPY codini /opt/codini-src/codini
RUN python3 -m venv --system-site-packages /opt/codini-venv \
    && /opt/codini-venv/bin/pip install --no-cache-dir /opt/codini-src

COPY benchmarks/polyglot/verify_toolchains.sh /usr/local/bin/verify-polyglot-toolchains
RUN chmod 0755 /usr/local/bin/verify-polyglot-toolchains \
    && mkdir -p /workspace \
    && chown node:node /workspace

USER node
WORKDIR /workspace

CMD ["verify-polyglot-toolchains"]

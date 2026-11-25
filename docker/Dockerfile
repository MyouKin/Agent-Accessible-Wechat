# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv

FROM debian:12-slim AS desktop

ARG TARGETARCH
ARG WECHAT_ARM64_SHA256=""
ARG WECHAT_AMD64_SHA256=""

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       at-spi2-core ca-certificates curl dbus-x11 desktop-file-utils fonts-noto-cjk fonts-noto-color-emoji \
       gir1.2-atspi-2.0 openbox novnc python3 python3-gi python3-venv \
       tini websockify x11-utils x11vnc xauth xclip xdotool xvfb \
       libasound2 libatomic1 libatk-bridge2.0-0 libbz2-1.0 libcups2 libdrm2 \
       libegl1 libgbm1 libgl1 libgtk-3-0 libnotify4 libnss3 libpulse0 \
       libsecret-1-0 libudev1 libxcb-cursor0 libxcb-icccm4 libxcb-image0 \
       libxcb-keysyms1 libxcb-render-util0 libxcb-xinerama0 libxkbcommon-x11-0 libxss1 \
    && rm -rf /var/lib/apt/lists/*

RUN set -eu; \
    case "$TARGETARCH" in \
      arm64) package_arch=arm64; checksum="$WECHAT_ARM64_SHA256" ;; \
      amd64) package_arch=x86_64; checksum="$WECHAT_AMD64_SHA256" ;; \
      *) echo "Unsupported TARGETARCH: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl --fail --show-error --silent --location --proto '=https' --retry 3 \
      --output /tmp/wechat.deb \
      "https://dldir1v6.qq.com/weixin/Universal/Linux/WeChatLinux_${package_arch}.deb"; \
    test "$(dpkg-deb --field /tmp/wechat.deb Architecture)" = "$TARGETARCH"; \
    if [ -n "$checksum" ]; then printf '%s  %s\n' "$checksum" /tmp/wechat.deb | sha256sum -c -; fi; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends /tmp/wechat.deb; \
    rm -rf /tmp/wechat.deb /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 --shell /bin/bash wechat \
    && install -d -o wechat -g wechat -m 0700 /data \
    && chmod 0700 /home/wechat

ENV HOME=/home/wechat \
    LANG=C.UTF-8 \
    DISPLAY=:99 \
    DISPLAY_RESOLUTION=1280x900x24 \
    XDG_RUNTIME_DIR=/tmp/wechat-runtime \
    XAUTHORITY=/tmp/wechat-runtime/Xauthority \
    QT_QPA_PLATFORM=xcb \
    QT_ACCESSIBILITY=1 \
    QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1 \
    GTK_A11Y=1 \
    LIBGL_ALWAYS_SOFTWARE=1 \
    PYTHONUNBUFFERED=1

FROM desktop AS desktop-tools

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       imagemagick locales procps python3-pyatspi \
    && localedef -i zh_CN -f UTF-8 zh_CN.UTF-8 \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -o wechat -g wechat -m 0700 /wechat-tools

FROM desktop-tools AS operator
WORKDIR /opt/operator

RUN python3 -m venv --system-site-packages /opt/operator-venv
ENV VIRTUAL_ENV=/opt/operator-venv
ENV PATH="/opt/operator-venv/bin:${PATH}"

COPY docker/operator/requirements.lock /opt/operator/requirements.lock
RUN --mount=type=bind,from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    UV_CACHE_DIR=/root/.cache/uv UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy \
    uv pip sync --require-hashes /opt/operator/requirements.lock

# Bundle the pinned MIT-licensed upstream package and retain its license.
RUN set -eu; \
    revision=1a559f614c43e28abb2ec9936a5ad39579312916; \
    curl --fail --show-error --silent --location --proto '=https' --retry 3 \
      --output /tmp/wechat-mcp.tar.gz \
      "https://codeload.github.com/zongwei-tang/wechat-mcp/tar.gz/$revision"; \
    echo "a43600eeca13dcb52ad2379bb09bf8b12199fabd21f222e0ac1c5514bfce6e06  /tmp/wechat-mcp.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/wechat-mcp.tar.gz --strip-components=1 \
      "wechat-mcp-$revision/wechat_mcp" "wechat-mcp-$revision/LICENSE"; \
    rm /tmp/wechat-mcp.tar.gz

COPY docker/operator/bridge_operator.py docker/operator/wechat_sessions.py /opt/operator/
FROM desktop-tools AS bridge
WORKDIR /app

RUN python3 -m venv --system-site-packages /opt/bridge
ENV VIRTUAL_ENV=/opt/bridge
ENV PATH="/opt/bridge/bin:${PATH}"

COPY pyproject.toml uv.lock /app/
RUN --mount=type=bind,from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    UV_PROJECT_ENVIRONMENT="$VIRTUAL_ENV" UV_CACHE_DIR=/root/.cache/uv \
    UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy \
    uv sync --locked --no-dev --extra mcp --no-install-project --python /usr/bin/python3

COPY bridge /app/bridge
COPY tools /app/tools
COPY config.yaml /app/config.yaml
COPY --from=operator /opt/operator-venv /opt/operator-venv
COPY --from=operator /opt/operator /opt/operator

COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/bridge-entrypoint

USER wechat
EXPOSE 6080 8765
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/bridge-entrypoint"]
CMD ["python", "-m", "bridge.server"]

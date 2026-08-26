# 构建：docker build -f docker/Dockerfile -t ipclick:latest .
# 注意上下文是仓库根目录，不是 docker/ 目录。
#
# 默认镜像自带浏览器渲染能力（patchright + chromium 本体）。要换引擎或构建不带
# 浏览器的精简镜像，用 --build-arg ENGINE=...，取值见下面的 ENGINE 说明。

ARG PYTHON_VERSION=3.14

# 基础镜像的完整引用。默认是 Docker Hub 上的官方 python 镜像；本机配的加速器不通时
# 不用去改 Docker Desktop 的 registry-mirrors，直接换一个能通的源：
#   --build-arg PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.14-slim
# 写全了域名的引用不会再走 registry-mirrors——加速器只对不带域名的 docker.io 短名生效。
# 顺带一提：加速器坏掉时的报错落在 `load metadata` 这一步，和 Dockerfile 本身无关。
ARG PYTHON_IMAGE=python:${PYTHON_VERSION}-slim

# 渲染引擎，决定装哪个 extra、下哪个浏览器本体：
#   patchright  Playwright 的反检测分支 + chromium 本体（默认）
#   camoufox    Firefox 内核，反检测最彻底，本体约 1 GB
#   linux       上面两个都装（对应 pyproject 的 [linux] extra），镜像最大
#   none        不装浏览器，只有 curl_cffi，镜像最小
ARG ENGINE=patchright

# ============================ 构建阶段 ============================
FROM ${PYTHON_IMAGE} AS builder

ARG ENGINE

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# curl-cffi 等库可能需要编译环境（只存在于构建阶段，不进最终镜像）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# uv 从 PyPI 装，而不是 COPY --from=ghcr.io/astral-sh/uv。
# 依赖本来就要从 PyPI 拉，再多依赖一个 ghcr.io 只是白送一个失败点：国内网络下它
# 常年 TLS 握手超时，而且这一步在依赖层之前——load metadata 一失败，整个构建当场
# 就没了（net/http: TLS handshake timeout），连 apt 那层缓存都用不上。
#
# 版本区间跟 pyproject 的 [tool.uv].required-version 对齐。这个值是原样交给 pip 的
# requirement，要钉死某个版本就传 --build-arg UV_SPEC=uv==0.9.18。
ARG UV_SPEC=uv~=0.9.18
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --root-user-action=ignore "$UV_SPEC"

# 先只复制依赖定义，让依赖层能被 Docker 缓存复用
# LICENSE 也要进来：pyproject 用 PEP 639 的 license-files 声明了它，
# 缺文件会让构建后端在生成元数据时直接失败。
COPY pyproject.toml uv.lock README.md LICENSE ./

# ENGINE=none 时不带任何 extra；否则按名字点亮对应 extra。
# 刻意不用 --all-extras：[camoufox] 与 [playwright] 在 pyproject 的
# [tool.uv].conflicts 里声明为互斥，--all-extras 会同时点亮两者而直接失败。
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$ENGINE" = "none" ]; then \
        uv sync --frozen --no-install-project --no-dev; \
    else \
        uv sync --frozen --no-install-project --no-dev --extra "$ENGINE"; \
    fi

# 再复制源码并安装项目本身
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$ENGINE" = "none" ]; then \
        uv sync --frozen --no-dev; \
    else \
        uv sync --frozen --no-dev --extra "$ENGINE"; \
    fi

# ============================ 运行阶段 ============================
FROM ${PYTHON_IMAGE} AS runtime

ARG ENGINE
# camoufox 的默认插件（当前只有 uBlock Origin）必须真的下下来才算构建成功。
# 置 0 则明确放弃插件：把空目录清掉，让 camoufox 启动时按"没有这个插件"处理。
ARG CAMOUFOX_REQUIRE_ADDONS=1

WORKDIR /app

# 浏览器本体放在共享目录而不是某个用户的家目录：镜像里下一次、所有用户都能读，
# 也免得换 USER 之后引擎找不到自己刚下的浏览器。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    XDG_CACHE_HOME=/opt/cache

# 以非 root 用户运行
RUN groupadd --system --gid 1000 ipclick \
    && useradd --system --uid 1000 --gid ipclick --create-home ipclick

# 只搬运虚拟环境和源码，编译工具链留在构建阶段
COPY --from=builder --chown=ipclick:ipclick /app/.venv /app/.venv
COPY --from=builder --chown=ipclick:ipclick /app/src /app/src

# 下浏览器本体。必须在 USER 之前跑：--with-deps 要用 apt 装 chromium 的系统库
# （libnss3、libatk 那一堆），那是 root 才能做的事。装完把目录放成所有人可读可执行，
# 否则切到 ipclick 用户后起不来浏览器。
#
# 最后那个 case 干两件事，都是 camoufox 专属的。
#
# 一是把默认插件查实。`python -m camoufox fetch` 下插件失败时（addons.mozilla.org 会按
# 地区返回 451）**不会**让自己失败：它先 makedirs 再下载，异常被自己吞掉、只在 stdout
# 打一行，退出码仍是 0。留下的空目录才是真正的坑——camoufox 的 maybe_download_addons()
# 只看目录存不存在、不看内容，空目录照样被当成有效插件塞进启动参数，直到 confirm_paths()
# 才抛 "manifest.json is missing"。结果就是：镜像构建全绿、1.8 GB 推上去、一起浏览器就死。
# 所以在这里当场拦住。真要不带插件就传 --build-arg CAMOUFOX_REQUIRE_ADDONS=0，
# 那时把空目录删掉——目录不存在时 camoufox 会在启动时尝试补下，失败也只是跳过该插件，
# 而这个镜像里 addons 的父目录是 root 只读的，它连补下都做不到，于是每次都稳定地跳过。
# 关键是别留下"装了但是空的"这种半残状态。
#
# 二是给 camoufox 补一个可写的 fontconfig 目录：camoufox 0.5.5 起
# fontconfig 缓存跟着 XDG_CACHE_HOME 走（0.5.4 及以前硬编码 ~/.cache/camoufox/fontconfig，
# 落在用户家目录里天然可写）。现在它落进上面刚设成 a+rX 只读的 /opt/cache，而这份
# fontconfig 是**运行期**由 ipclick 用户起浏览器时才写的（_generate_fontconfig() 只在
# launch 路径上调，python -m camoufox fetch 走不到），不放开写就是当场 PermissionError。
# 只放开这一个子目录，浏览器本体仍然只读。
#
# 这一层刻意和上面的依赖层分开：浏览器本体是几百 MB 到 1 GB，
# 改源码时不该让它重新下一遍。
RUN set -eu; \
    case "$ENGINE" in \
      patchright) \
        patchright install --with-deps chromium ;; \
      camoufox) \
        apt-get update && apt-get install -y --no-install-recommends \
          libgtk-3-0 libx11-xcb1 libasound2 libdbus-glib-1-2 \
          && rm -rf /var/lib/apt/lists/*; \
        python -m camoufox fetch ;; \
      linux) \
        patchright install --with-deps chromium; \
        python -m camoufox fetch ;; \
      none) \
        echo "ENGINE=none：跳过浏览器本体" ;; \
      *) \
        echo "未知 ENGINE=$ENGINE，可选 patchright / camoufox / linux / none" >&2; exit 1 ;; \
    esac; \
    for d in /opt/ms-playwright /opt/cache; do \
      [ -d "$d" ] && chmod -R a+rX "$d" || true; \
    done; \
    case "$ENGINE" in \
      camoufox|linux) \
        if [ "$CAMOUFOX_REQUIRE_ADDONS" = "1" ]; then \
          for a in /opt/cache/camoufox/addons/*/; do \
            [ -d "$a" ] || continue; \
            [ -f "$a/manifest.json" ] || { \
              echo "构建中止：插件 $a 是空的，说明 camoufox fetch 没能把它下下来" >&2; \
              echo "（addons.mozilla.org 常按地区返回 451）。这种镜像能构建成功，但一起" >&2; \
              echo "浏览器就会抛 InvalidAddonPath。换个网络重建，或用" >&2; \
              echo "--build-arg CAMOUFOX_REQUIRE_ADDONS=0 明确构建一个不带插件的镜像。" >&2; \
              exit 1; }; \
          done; \
        else \
          for a in /opt/cache/camoufox/addons/*/; do \
            [ -d "$a" ] && [ ! -f "$a/manifest.json" ] && rm -rf "$a"; \
          done; \
          echo "CAMOUFOX_REQUIRE_ADDONS=0：已清掉未下载成功的插件目录，镜像不带插件"; \
        fi; \
        mkdir -p /opt/cache/camoufox/fontconfig; \
        chown -R ipclick:ipclick /opt/cache/camoufox/fontconfig ;; \
    esac

# 配置目录（可通过 -v 挂载 ipclick.toml 进来）
RUN mkdir -p /home/ipclick/.ipclick /app/data \
    && chown -R ipclick:ipclick /home/ipclick/.ipclick /app/data

USER ipclick

EXPOSE 9528 9527

# 走 grpc.health.v1 标准健康检查，而不是只探 TCP 端口通不通——
# 端口能连上不代表服务真的在可服务状态（例如正在优雅停机）。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ipclick health --host 127.0.0.1 --port 9528 --timeout 3 || exit 1

CMD ["ipclick", "run"]

# 构建：docker build -f docker/Dockerfile -t ipclick:latest .
# 注意上下文是仓库根目录，不是 docker/ 目录。

# ============================ 构建阶段 ============================
FROM python:3.13-slim AS builder

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

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /bin/uv

# 先只复制依赖定义，让依赖层能被 Docker 缓存复用
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# 再复制源码并安装项目本身
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ============================ 运行阶段 ============================
FROM python:3.13-slim AS runtime

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# 以非 root 用户运行
RUN groupadd --system --gid 1000 ipclick \
    && useradd --system --uid 1000 --gid ipclick --create-home ipclick

# 只搬运虚拟环境和源码，编译工具链留在构建阶段
COPY --from=builder --chown=ipclick:ipclick /app/.venv /app/.venv
COPY --from=builder --chown=ipclick:ipclick /app/src /app/src

# 配置目录（可通过 -v 挂载 config.toml 进来）
RUN mkdir -p /home/ipclick/.ipclick /app/data \
    && chown -R ipclick:ipclick /home/ipclick/.ipclick /app/data

USER ipclick

EXPOSE 9527

# gRPC 服务没有 HTTP 健康检查端点，这里用一次 TCP 连接探活
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import socket,sys; s=socket.create_connection(('127.0.0.1',9527),3); s.close()" || exit 1

CMD ["ipclick", "run"]

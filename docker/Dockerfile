# 基础镜像
# FROM python:3.14-slim
FROM 311288365115.dkr.ecr.ap-east-1.amazonaws.com/ratus/python:3.14-slim

RUN cp /usr/share/zoneinfo/Asia/Shanghai /etc/localtime

WORKDIR /app

# 环境变量：
# 1. 优化 Python 运行
# 2. 将虚拟环境路径加入 PATH，确保能直接运行 ipclick 命令
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# 安装系统依赖
# curl-cffi 等库可能需要编译环境
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libffi-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 安装 uv：使用官方镜像复制二进制文件是最佳实践
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# 复制项目依赖定义文件
# 包含 uv.lock 以确保依赖版本一致
COPY pyproject.toml uv.lock README.md ./

# 安装依赖（利用 Docker 缓存）
# --frozen: 严格依照 lock 文件
# --no-install-project: 仅安装依赖，不安装项目本身
# --no-dev: 生产环境不需要开发依赖
RUN uv sync --frozen --no-install-project --no-dev

# 复制源代码
COPY src/ ./src/

# 安装项目本身
RUN uv sync --frozen --no-dev

# 创建数据目录
RUN mkdir -p /app/data

# 暴露端口
EXPOSE 9527

# 启动命令
CMD ["ipclick", "run"]

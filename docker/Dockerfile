# 基础镜像
FROM python:3.14-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libffi-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 安装 uv
RUN pip install uv

# 复制项目文件
COPY pyproject.toml README.md ./

# 使用 uv 安装依赖
RUN uv sync --only-dev

# 复制源代码
COPY src/ ./src/

# 安装项目本身
RUN uv sync

# 创建数据目录
RUN mkdir -p /app/data

# 暴露端口
EXPOSE 9527

# 启动命令
CMD ["ipclick", "run"]

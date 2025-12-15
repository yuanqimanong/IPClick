# 基础镜像
FROM python:3.14-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libffi-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 安装 Poetry
RUN pip install poetry -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制依赖文件和 README
COPY pyproject.toml poetry.lock poetry.toml README.md ./

# 配置 Poetry
RUN poetry config virtualenvs.create false

# 只安装依赖，不安装项目本身
RUN poetry install --only main --no-interaction --no-ansi --no-root

# 复制项目代码
COPY src/ ./src/

# 安装项目本身
RUN poetry install --only main --no-interaction --no-ansi

# 创建数据目录
RUN mkdir -p /app/data

# 暴露端口
EXPOSE 9527

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:9527/health || exit 1

# 启动命令
CMD ["ipclick", "run"]
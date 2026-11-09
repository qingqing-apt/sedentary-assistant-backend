# syntax=docker/dockerfile:1
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 依赖单独一层：改业务代码不会触发重装
COPY requirements.txt ./
RUN python -m pip install -r requirements.txt

COPY app ./app

# 命名卷首次挂载会继承这个目录的属主，非 root 用户才写得进 SQLite
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data \
    && chown app:app /app/data

USER app

EXPOSE 8000

# 探测接口本身，不是进程存活
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

# SQLite 单写者 + 进程内 ContextVar 身份，只能单 worker
# 用 opentelemetry-instrument 包一层：自动发现已安装的 instrumentation，
# 没有配置 OTEL_EXPORTER_OTLP_ENDPOINT 时只是不导出，不影响启动。
CMD ["opentelemetry-instrument", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

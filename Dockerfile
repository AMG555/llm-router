# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

RUN useradd --system --uid 10001 --home /nonexistent --shell /usr/sbin/nologin appuser \
    && mkdir -p /data && chown -R appuser:appuser /data /app

ENV DB_PATH=/data/router.db \
    PORT=7860
USER appuser
EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','7860')+'/health',timeout=3)"

CMD ["sh","-c","exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860} --proxy-headers --forwarded-allow-ips=* --no-access-log --log-level warning"]

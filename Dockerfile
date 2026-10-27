FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN mkdir -p /data
ENV DB_PATH=/data/router.db PORT=7860
EXPOSE 7860

CMD ["sh","-c","uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]

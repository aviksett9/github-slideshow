FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Kolkata \
    DATA_DIR=/data \
    PORT=8080

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY scanner ./scanner

RUN useradd --create-home --uid 10001 app && mkdir -p /data && chown app:app /data
USER app
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5)"

CMD ["python", "-m", "scanner.server"]

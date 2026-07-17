FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY fifa_monitor/ ./fifa_monitor/
COPY config.json ./

# state.json, monitor.log, proxies.txt and .env are mounted/provided at runtime.
VOLUME ["/app/data"]

CMD ["python", "-m", "fifa_monitor", "--config", "config.json"]

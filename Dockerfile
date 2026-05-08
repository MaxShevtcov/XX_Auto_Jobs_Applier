FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    ca-certificates \
    wget \
    unzip \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN pip install --no-cache-dir playwright
RUN playwright install chromium
RUN playwright install-deps chromium

RUN useradd -m -u 1000 appuser
RUN mkdir -p /home/appuser/.cache && cp -r /root/.cache/ms-playwright /home/appuser/.cache/ && chown -R appuser:appuser /home/appuser/.cache

COPY main.py ./
COPY src/ ./src/

RUN mkdir -p /app/data_folder /app/logs && chown -R appuser:appuser /app

USER appuser

CMD ["python", "main.py"]

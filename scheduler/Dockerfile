FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends libreoffice \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY scheduler/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Shared modules from backend — single source of truth
COPY backend/parsers.py backend/categorizer.py .
# Scheduler entrypoint
COPY scheduler/scheduler.py .

RUN mkdir -p /watch/done /watch/error /data

CMD ["python", "scheduler.py"]

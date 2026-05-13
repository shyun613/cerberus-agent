FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends nodejs npm && \
    npm install -g @openai/codex && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY docker-entrypoint.sh .

RUN mkdir -p /app/data && \
    chmod +x /app/docker-entrypoint.sh

CMD ["/app/docker-entrypoint.sh"]

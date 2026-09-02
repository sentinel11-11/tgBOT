FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot
COPY tools ./tools

# config.yaml, .env и credentials.json пробрасываются томом:
#   docker run -d --name tgbot \
#     -v $PWD/config.yaml:/app/config.yaml:ro \
#     -v $PWD/credentials.json:/app/credentials.json:ro \
#     -v $PWD/data:/app/data \
#     --env-file .env --restart unless-stopped tgbot
VOLUME ["/app/data", "/app/logs"]

CMD ["python", "-m", "bot"]

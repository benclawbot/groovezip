FROM node:22-bookworm-slim AS javascript
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0

COPY --from=javascript /usr/local/bin/node /usr/local/bin/node

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py index.html ./
COPY designs/backgrounds/music-waves.png ./designs/backgrounds/music-waves.png

EXPOSE 8787
CMD ["python", "app.py"]

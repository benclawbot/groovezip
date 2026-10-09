FROM node:26-bookworm-slim AS pot-provider-build

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        g++ \
        libgif-dev \
        libjpeg-dev \
        libpango1.0-dev \
        librsvg2-dev \
        make \
        pkg-config \
        python3 \
    && rm -rf /var/lib/apt/lists/* \
    && git clone --filter=blob:none https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /src/bgutil-ytdlp-pot-provider \
    && git -C /src/bgutil-ytdlp-pot-provider checkout 26475e9d3665b972c4aeb83b4dbaafcf64b88438
WORKDIR /src/bgutil-ytdlp-pot-provider/server
RUN npm ci --no-audit --no-fund \
    && npx tsc \
    && npm prune --omit=dev --no-audit --no-fund

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libcairo2 \
        libgif7 \
        libjpeg62-turbo \
        libpango-1.0-0 \
        librsvg2-2 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=pot-provider-build /usr/local/bin/node /usr/local/bin/node
COPY --from=pot-provider-build /src/bgutil-ytdlp-pot-provider/server/package.json /app/bgutil-provider/package.json
COPY --from=pot-provider-build /src/bgutil-ytdlp-pot-provider/server/build /app/bgutil-provider/build
COPY --from=pot-provider-build /src/bgutil-ytdlp-pot-provider/server/node_modules /app/bgutil-provider/node_modules

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py index.html ./
COPY designs/backgrounds/music-waves.png ./designs/backgrounds/music-waves.png

EXPOSE 8787
CMD ["python", "app.py"]

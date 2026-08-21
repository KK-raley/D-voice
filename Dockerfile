# Vocalis - all-in-one runtime (backend + HUD)
FROM node:20-alpine AS hud
WORKDIR /app
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app

# Audio playback helpers (optional) and build deps for webrtcvad
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ portaudio19-dev mpv \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY vocalis ./vocalis
RUN pip install --no-cache-dir -e ".[all]"

COPY --from=hud /app/dist ./hud

ENV VOCALIS_HOME=/data
VOLUME /data
EXPOSE 8642

CMD ["python", "-m", "uvicorn", "vocalis.server.app:app", "--host", "0.0.0.0", "--port", "8642"]

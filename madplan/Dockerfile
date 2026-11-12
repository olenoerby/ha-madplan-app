# MadPlan as a Home Assistant app: the pages are built first, then served by the Python backend,
# which also runs the background jobs. One process, with SQLite in /data.

FROM node:22-alpine AS pages
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-alpine
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MADPLAN_APP_ENV=production \
    MADPLAN_DATA_DIR=/data \
    MADPLAN_STATIC_DIR=/app/static
WORKDIR /app
COPY backend/requirements.txt ./
RUN apk add --no-cache tzdata \
    && pip install --no-cache-dir -r requirements.txt
COPY backend/ ./
COPY --from=pages /build/out ./static
COPY run.sh /run.sh
RUN chmod a+x /run.sh

LABEL io.hass.type="app" io.hass.name="MadPlan"
CMD ["/run.sh"]

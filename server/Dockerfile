FROM python:3.12-slim

LABEL maintainer="sri"
LABEL description="RTO Tracker"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Kolkata
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Secrets are injected at runtime via .env file (docker-compose env_file).
ENV TEAMS_WEBHOOK=""
ENV SERVER_URL=""

COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Build context is the repo root (see docker-compose.yml) so notifier.py
# can be pulled in from shared/ instead of being duplicated in server/.

# Python server files
COPY server/config.py server/database.py server/detection.py server/main.py server/deps.py server/schemas.py server/segments.py server/analytics.py ./
COPY shared/notifier.py ./
COPY server/routers/ ./routers/

# Static + template files
COPY server/rto-ui.html server/admin.html ./
COPY server/templates/ ./templates/
COPY server/static/ ./static/

# sky-logo.png is optional
COPY server/sky-logo.pn[g] ./

RUN mkdir -p /app/data
RUN [ -f /app/sky-logo.png ] && mv /app/sky-logo.png /app/static/ || true

EXPOSE 9999

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:9999/health')"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9999"]
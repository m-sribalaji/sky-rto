FROM python:3.12-slim

LABEL maintainer="sri"
LABEL description="RTO Tracker Server"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Kolkata
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Secrets are injected at runtime via .env file (docker-compose env_file).
# Never hardcode TEAMS_WEBHOOK or SERVER_URL here.
ENV TEAMS_WEBHOOK=""
ENV SERVER_URL=""

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Python server files
COPY config.py database.py detection.py main.py segments.py notifier.py analytics.py ./

# Static + template files
COPY rto-ui.html admin.html ./
COPY templates/ ./templates/

# sky-logo.png is optional
COPY sky-logo.pn[g] ./

RUN mkdir -p /app/data /app/static
RUN [ -f /app/sky-logo.png ] && mv /app/sky-logo.png /app/static/ || true

EXPOSE 8989

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8989/health')"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8989"]
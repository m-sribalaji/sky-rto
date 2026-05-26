FROM python:3.12-slim

LABEL maintainer="sri"
LABEL description="RTO Tracker Server"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Kolkata
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Teams webhook for server-side notifications (leave, overrides)
# Override at runtime: docker run -e TEAMS_WEBHOOK -e SERVER_URL
ENV TEAMS_WEBHOOK=""
ENV SERVER_URL="http://10.131.80.141:8989"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Python server files
COPY config.py database.py detection.py main.py segments.py notifier.py ./

# Static + template files
COPY rto-ui.html admin.html ./
COPY templates/ ./templates/

# sky-logo.png is optional - copy only if present
COPY sky-logo.pn[g] ./

RUN mkdir -p /app/data /app/static
# Move sky-logo to static dir if it exists
RUN [ -f /app/sky-logo.png ] && mv /app/sky-logo.png /app/static/ || true

EXPOSE 8989

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8989/health')"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8989"]

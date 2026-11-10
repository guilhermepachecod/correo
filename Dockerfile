FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libffi-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /app/database /app/static /app/templates /app/accounts \
    && groupadd -r correo && useradd -r -g correo -d /app correo \
    && chown -R correo:correo /app \
    && chmod +x docker-entrypoint.sh
EXPOSE 15002
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:15002/healthz')" || exit 1
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["gunicorn", "-w", "1", "--threads", "64", "-b", "0.0.0.0:15002", "app:app"]

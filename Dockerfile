FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Nicht als root. /data wird per Volume gemountet, deshalb hier schon anlegen
# und uebereignen — sonst gehoert das Verzeichnis beim ersten Start root.
RUN useradd --system --uid 10001 --create-home meshfeed \
    && mkdir -p /data && chown meshfeed:meshfeed /data
USER meshfeed

VOLUME ["/data"]
EXPOSE 8080

# Der Healthcheck prueft nur den Dienst selbst, nicht den Funkverkehr:
# ein ruhiges Netz ist kein Fehler.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]

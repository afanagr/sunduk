# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tools ./tools
COPY run.py ./

# No USER on purpose: the container manages directories that the administrator
# picks anywhere on the server (they are mounted at /host and may belong to any
# owner), so it runs as root.  It stays confined by the hardening in
# docker-compose.yml: a read-only root filesystem, no-new-privileges, a 64 MB
# tmpfs for /tmp and cap_drop: ALL with only three capabilities added back
# (CHOWN, DAC_OVERRIDE, FOWNER) so it can write into directories owned by other
# users.  Everything else (NET_RAW, MKNOD, SETFCAP, ...) stays dropped.
EXPOSE 8080 8081

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request;[urllib.request.urlopen('http://127.0.0.1:'+p+'/healthz',timeout=3).read() for p in ('8080','8081')]" || exit 1

CMD ["python", "run.py"]


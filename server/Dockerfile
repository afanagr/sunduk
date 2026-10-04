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
# owner), so it runs as root.  The hardened stacks (docker-compose.yml in the
# repository root, deploy/docker-compose.yml) additionally pin it down there: a
# read-only root filesystem, no-new-privileges, a 64 MB tmpfs for /tmp and
# cap_drop: ALL with only three capabilities added back (CHOWN, DAC_OVERRIDE,
# FOWNER) so it can write into directories owned by other users.  Everything
# else (NET_RAW, MKNOD, SETFCAP, ...) stays dropped there.
#
# Those switches only exist at run time, so the minimal stack
# (deploy/docker-compose.dockge.yml) relies on the container defaults instead.
EXPOSE 8080 8081

# Read the ports through the application's own loader, so the check follows
# whatever config/config.yml or the environment actually says.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request as u; from app.config import load_config; c = load_config(); [u.urlopen('http://127.0.0.1:%d/healthz' % p, timeout=3).read() for p in (c.admin_port, c.public_port)]" || exit 1

CMD ["python", "run.py"]


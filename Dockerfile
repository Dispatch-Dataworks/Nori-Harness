# Nori: a self-hosted household assistant. Stdlib HTTP server, one SQLite
# database, no other services required.
#
#   docker compose up -d
#
# See docs/deployment-and-watchdog.md for operating this long-term, and
# IMPORT.md if you're bringing an existing non-Docker install's data in.

FROM python:3.12-slim

# tzdata's OS package gives the C library a real timezone database too;
# the pip package below covers Python's own zoneinfo lookups. Belt and
# braces cheaply -- both are tiny.
RUN apt-get update && apt-get install -y --no-install-recommends \
      tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persistent state lives under these three directories -- see
# docker-compose.yml for the named volumes mounted here. NORI_LIVE=1 is
# the same guard nori_ctl.ps1 sets for the real service (store.py
# refuses to open a data directory without it); NORI_NO_LOGFILE=1 keeps
# stdout/stderr as normal process streams instead of redirecting to
# .nori.log/.nori.err, so `docker compose logs` actually shows
# something. NORI_BIND_HOST=0.0.0.0 is safe here specifically because
# the container boundary is the isolation, not loopback-only binding --
# see .env.example's own note on this.
ENV NORI_LIVE=1 \
    NORI_NO_LOGFILE=1 \
    NORI_BIND_HOST=0.0.0.0 \
    NORI_PORT=8877

EXPOSE 8877

CMD ["python3", "server.py"]

ARG BUILD_FROM=ghcr.io/home-assistant/base:latest
FROM ${BUILD_FROM}

# HA base image = Alpine (musl): python3/pip müssen explizit installiert werden.
RUN apk add --no-cache python3 py3-pip

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/

# venv vermeidet PEP-668-Fehler (externally-managed-environment) auf Alpine.
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir .

# init: false in config.yaml -> s6 nicht als EntryPoint; /run.sh direkt ausführen.
ENTRYPOINT []
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TMPDIR=/tmp

RUN mkdir -p /data/logs /data/db /tmp/hems_cache

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]

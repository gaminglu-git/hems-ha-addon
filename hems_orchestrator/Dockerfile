ARG BUILD_FROM=python:3.12-slim-bookworm
FROM ${BUILD_FROM}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TMPDIR=/tmp

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

RUN mkdir -p /data/logs /data/db /tmp/hems_cache

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]

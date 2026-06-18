FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts

RUN pip install --no-cache-dir '.[playwright]' \
    && playwright install --with-deps chromium \
    && chmod +x /app/scripts/docker-entrypoint.sh

VOLUME ["/app/data"]

ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["cian-rent-alerts"]

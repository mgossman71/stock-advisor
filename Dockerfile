# Stock Advisor.  Python 3.11 matches the interpreter the test suite runs on.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Absolute path into the /data volume.  Must not be the repo-relative
    # sqlite:///./data/... default, or the database lands on the container layer
    # and disappears when the container is removed.
    DATABASE_URL=sqlite:////data/stock_advisor.db

# ca-certificates: yfinance, Nasdaq and FRED are all HTTPS.  curl: container
# health checks and debugging from inside the container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies in their own layer, so editing source does not reinstall the world.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY alembic.ini pyproject.toml ./
COPY app ./app
COPY migrations ./migrations
COPY web ./web
COPY scripts ./scripts
# Offline fallback for the S&P 500 universe: Wikipedia is rate-limited/unreachable
# from containers fairly often, and this CSV is why that is not fatal.
COPY data/sp500.csv ./data/sp500.csv
# Offline fallback for the US-listed scan universe (Nasdaq screener cache).
COPY data/us_listed.csv ./data/us_listed.csv

# Unprivileged user; /data is the SQLite volume and must be writable by it.
RUN chmod +x ./scripts/docker-entrypoint.sh \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 9000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/healthz', timeout=3)"

ENTRYPOINT ["./scripts/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000"]

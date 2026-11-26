FROM python:3.12-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DOCBREAKER_DATA_DIR=/data
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils poppler-data fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 docbreaker && useradd -u 10001 -g 10001 -M docbreaker \
    && mkdir /data && chown 10001:10001 /data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY docbreaker ./docbreaker
COPY deploy/healthcheck.py deploy/smoke.py ./deploy/
ARG BUILD_SHA=development
ENV DOCBREAKER_BUILD=$BUILD_SHA
FROM base AS test
COPY tests ./tests
COPY tools/loadtest.py ./tools/loadtest.py
RUN python -m unittest discover -s tests -v

FROM base AS runtime
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=3 CMD ["python", "deploy/healthcheck.py"]
CMD ["python", "-m", "docbreaker"]

ARG PYTHON_IMAGE=python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
FROM ${PYTHON_IMAGE} AS dependencies
WORKDIR /build
COPY requirements.lock .
RUN python -m pip install --no-cache-dir --only-binary=:all: --target=/packages -r requirements.lock

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/packages:/app
WORKDIR /app
RUN python -m pip uninstall --yes pip && groupadd --gid 10001 platform && useradd --uid 10001 --gid 10001 --no-create-home platform && mkdir .local && chown 10001:10001 .local
COPY --from=dependencies /packages /packages
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini container_health.py ./
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 CMD ["python", "container_health.py"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-proxy-headers"]

# syntax=docker/dockerfile:1.7

FROM node:24-bookworm-slim AS frontend
WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.11-slim-bookworm AS python-build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
RUN python -m pip wheel --wheel-dir /wheels .

FROM python:3.11-slim-bookworm AS runtime
ARG QUANTOS_BUILD_COMMIT=UNKNOWN
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    QUANTOS_HOST=0.0.0.0 \
    PORT=8000

RUN groupadd --system quantos && useradd --system --gid quantos --home /app quantos
WORKDIR /app
COPY --from=python-build /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels quantos \
    && rm -rf /wheels
COPY --from=frontend /build/web/dist /app/web/dist
RUN python -c "import json,os,pathlib,quantos; pathlib.Path('/app/release-manifest.json').write_text(json.dumps({'git_commit':os.environ.get('QUANTOS_BUILD_COMMIT','UNKNOWN'),'quantos_version':quantos.__version__},sort_keys=True)+'\\n',encoding='utf-8')" \
    && chown -R quantos:quantos /app

USER quantos
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import json,urllib.request; value=json.load(urllib.request.urlopen('http://127.0.0.1:' + __import__('os').environ.get('PORT','8000') + '/v1/health',timeout=2)); raise SystemExit(0 if value.get('status') == 'ok' else 1)"

CMD ["sh", "-c", "exec quantos start --project-root /app --demo --public-preview --host \"${QUANTOS_HOST}\" --port \"${PORT}\" --no-browser"]

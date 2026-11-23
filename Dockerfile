FROM node:24-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    FRONTEND_DIST=/app/frontend/dist \
    ALLOW_PRIVATE_CONTAINER=true \
    AUTH_REQUIRED=true
RUN python -m venv /opt/venv && useradd --create-home studio
WORKDIR /app
COPY pyproject.toml ./
COPY requirements.lock ./
COPY src/ src/
RUN python -m pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -c requirements.lock .
COPY --from=frontend /build/dist frontend/dist
USER studio
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["python", "-m", "uvicorn", "gpt_live_python.web:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "30"]

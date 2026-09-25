# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.12.6
FROM python:${PYTHON_VERSION}-slim as base

ENV PYTHONDONTWRITEBYTECODE=1
ENV DOCKER_BUILDKIT=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

ARG UID=10001
RUN adduser \
    --disabled-password \
    --gecos "" \
    --home "/nonexistent" \
    --shell "/sbin/nologin" \
    --no-create-home \
    --uid "${UID}" \
    appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

USER appuser

COPY . .

EXPOSE 8080

# Cloud Run already logs every request; uvicorn's access log would duplicate it.
# Keep-alive above Cloud Run's front-end idle timeout so connections are reused.
CMD ["uvicorn", "src.main:api", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--timeout-keep-alive", "75"]

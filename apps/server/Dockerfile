# SentinelAI backend image — one image, two entrypoints (http | worker).
# NOTE: For production, the base image MUST be swapped to an approved/hardened
# base per docs/deployment-architecture.md Part 5 and signed with cosign before
# deploy. python:3.12-slim is a development default only.

# ---- build stage -----------------------------------------------------------
FROM python:3.12-slim-bookworm AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install .

# ---- runtime stage ---------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/opt/venv/bin:$PATH"
# Run as non-root (security-architecture.md; deployment-architecture.md).
RUN useradd --system --uid 1001 --create-home sentinel
COPY --from=build /opt/venv /opt/venv
COPY --from=build /app/src /app/src
WORKDIR /app
USER sentinel
EXPOSE 8000

# Default entrypoint is the HTTP API; the worker overrides CMD (see
# docker-compose.dev.yml). Both processes share this single image.
CMD ["uvicorn", "sentinelai.entrypoints.http.main:app", "--host", "0.0.0.0", "--port", "8000"]

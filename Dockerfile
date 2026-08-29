FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Brussels

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata curl ffmpeg util-linux \
 && rm -rf /var/lib/apt/lists/* \
 && ln -fs /usr/share/zoneinfo/$TZ /etc/localtime

WORKDIR /app

# Dependencies come from the pinned lockfile, not from a list restated here.
# The previous version duplicated pyproject.toml's dependencies inline with
# `>=`-only bounds, so the two could drift and no two builds were alike.
# Copied on its own first so a source-only change does not re-run the install.
COPY requirements.txt /app/
RUN pip install --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml alembic.ini /app/
# `app/migrations/` comes with it -- the app runs `alembic upgrade head` at
# startup, so the revision scripts have to be in the image.
COPY app /app/app

COPY entrypoint.sh /usr/local/bin/entrypoint.sh

# Deliberately no `USER` here. The entrypoint starts as root, works out which
# uid owns the bind-mounted /data, and drops to it before exec'ing the app.
# A hardcoded user cannot write a NAS bind mount owned by another account —
# that is what produced "attempt to write a readonly database" on Synology.
RUN useradd --create-home --shell /bin/bash doorbird \
 && mkdir -p /data/mp3 /data/logs /data/ulaw \
 && chown -R doorbird:doorbird /data /app \
 && sed -i 's/\r$//' /usr/local/bin/entrypoint.sh \
 && chmod +x /usr/local/bin/entrypoint.sh

ENV DATA_DIR=/data \
    HOST=0.0.0.0 \
    PORT=8080

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://localhost:8080/healthz || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]

# One image, three jobs: the CLI, a scheduled run, and the webhook service.
# Which one it does is decided by the command, not by building a different image.

FROM python:3.13-slim

# Don't run as root, and don't buffer logs — a crashed container with no output
# is the worst possible thing to debug on someone else's infrastructure.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency layer first, so source edits don't reinstall the world.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[server]"

COPY config ./config
COPY examples ./examples

RUN useradd --create-home --uid 10001 gtm && chown -R gtm:gtm /app
USER gtm

# The page cache, analysis cache, queue, and run state all live here. Mount a
# volume at this path to keep them across restarts; without one, a redeploy
# simply re-scrapes and re-enriches, which costs money but breaks nothing.
VOLUME ["/app/.cache"]

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://localhost:{os.getenv(\"PORT\",8000)}/health').read()" || exit 1

# Webhook service by default. For a scheduled run, override with e.g.
#   docker run ... gtm-enrich run --source hubspot --filter config/filters/new-prospects.yaml
CMD ["gtm-enrich", "serve"]

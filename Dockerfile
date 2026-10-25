FROM python:3.12-slim-bookworm AS builder
ARG POETRY_VERSION=2.3.4
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 POETRY_NO_INTERACTION=1 POETRY_VIRTUALENVS_IN_PROJECT=true
WORKDIR /app
RUN pip install --no-cache-dir "poetry==$POETRY_VERSION" && python -m venv .venv
COPY pyproject.toml poetry.lock poetry.toml ./
RUN poetry install --only main --no-root
COPY README.md ./
COPY components ./components
RUN poetry install --only main

FROM python:3.12-slim-bookworm AS runtime
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN groupadd --gid 10001 bagel && useradd --uid 10001 --gid bagel --no-create-home bagel
COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv
COPY --chown=10001:10001 components ./components
COPY --chown=10001:10001 tools ./tools
COPY --chown=10001:10001 supabase/migrations ./supabase/migrations
USER 10001:10001
EXPOSE 9090
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c \
    "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('BAGEL_HEALTH_PORT','9090')+'/health/live',timeout=3)"
ENTRYPOINT ["bagel-market-data"]
CMD ["run"]

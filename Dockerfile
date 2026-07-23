FROM python:3.13-slim

ARG APP_UID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir .

COPY . .

RUN groupadd --gid "${APP_UID}" appuser \
    && useradd --no-log-init --create-home --uid "${APP_UID}" --gid appuser appuser \
    && install -d -o appuser -g appuser /app/media \
    && python manage.py collectstatic --noinput
USER appuser

EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]

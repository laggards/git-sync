FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates git openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY wsgi.py ./wsgi.py

RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin git-sync \
    && mkdir -p /state /workspace \
    && chown -R 10001:10001 /state /workspace

USER 10001:10001
EXPOSE 8080

CMD ["gunicorn", "--workers", "1", "--threads", "8", "--bind", "0.0.0.0:8080", "--access-logfile", "-", "--error-logfile", "-", "wsgi:app"]

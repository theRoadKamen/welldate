FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    DATA_ROOT=/data \
    SOFFICE=/usr/bin/soffice

RUN apt-get update \
    && apt-get install -y --no-install-recommends libreoffice-calc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py ./app.py
COPY static ./static

RUN mkdir -p /data/raw

EXPOSE 8080

CMD ["python", "app.py"]

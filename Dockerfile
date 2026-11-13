FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# OCR de excepción para PDFs escaneados (Plan V3.0 §5.6): Tesseract con idioma español.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-spa \
    && rm -rf /var/lib/apt/lists/*
ENV OCR_ENGINE=tesseract

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh && chmod 755 /usr/local/bin/docker-entrypoint.sh

# Usuario sin privilegios. En AKS escucha en PORT=80 (el runtime permite puertos bajos sin root).
RUN groupadd --gid 10001 api && useradd --create-home --uid 10001 --gid 10001 api && chown -R api:api /app
# UID numérico: el chart de AKS exige runAsNonRoot y solo puede verificarlo con un número.
USER 10001:10001

ENV APP_ROLE=api PORT=8000 STORAGE_BACKEND=memory
EXPOSE 8000 80
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD [ "$APP_ROLE" != "api" ] || python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/healthz', timeout=3)" || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

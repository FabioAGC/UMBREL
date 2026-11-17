FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# Tesseract com português (o laudo-check usa lang="por")
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-por libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY laudo_check/ laudo_check/
RUN pip install --no-cache-dir . tzdata
COPY server.py ./
COPY kb-seed/ /app/kb-seed/

RUN useradd -r -u 10001 app && mkdir -p /data/kb && chown -R app /data
USER app
VOLUME /data

EXPOSE 8000
# Na 1ª subida o volume recebe o config.yaml padrão; depois, o que está no volume prevalece.
CMD ["sh", "-c", "(cp -rn /app/kb-seed/. \"$KB_PATH\"/ || true) && exec uvicorn server:app --host 0.0.0.0 --port 8000 --workers 1"]

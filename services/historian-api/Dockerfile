FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY services/historian-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/historian-api/app.py services/historian-api/paineis.yaml ./
RUN useradd -r -u 10001 app
USER app
EXPOSE 8000
# 1 worker: as threads de leitura vivem dentro do processo (com 2 workers o Historian levaria o dobro de consultas)
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

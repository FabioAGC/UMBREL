FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY services/etl-mes/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/shared/mes_client.py services/etl-mes/app.py ./
RUN useradd -r -u 10001 app && mkdir -p /data && chown app /data
USER app
VOLUME /data
EXPOSE 8000
# 1 worker só: o agendador vive dentro do processo
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

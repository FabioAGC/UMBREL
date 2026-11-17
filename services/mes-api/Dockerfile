FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY services/mes-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/shared/mes_client.py services/mes-api/app.py ./
RUN useradd -r -u 10001 app
USER app
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]

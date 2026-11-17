FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# Driver ODBC 18 da Microsoft (o "SQL Server" do Windows não existe no Linux)
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl gnupg ca-certificates unixodbc \
 && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
 && curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list -o /etc/apt/sources.list.d/mssql-release.list \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
 && apt-get purge -y curl gnupg && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt tzdata
COPY db.py etl_log_acesso.py scheduler_pausas.py dashboard_template.html ./

# O scheduler escreve dashboard.html e logs/ ao lado do código, e os parquets em data/
RUN useradd -r -u 10001 app && mkdir -p /app/data /app/logs && chown -R app /app
USER app
VOLUME /app/data

EXPOSE 8000
CMD ["python", "scheduler_pausas.py"]

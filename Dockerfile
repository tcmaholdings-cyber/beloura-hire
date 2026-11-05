FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /app/data /app/logs && useradd --system --home /app --shell /usr/sbin/nologin hire && chown -R hire:hire /app
USER hire
ENV HIRE_DB_PATH=/app/data/hire.db HOST=0.0.0.0 PORT=8140
EXPOSE 8140
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8140/healthz', timeout=4).status==200 else 1)"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8140", "--proxy-headers", "--forwarded-allow-ips", "*"]

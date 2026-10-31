FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

RUN pip install --no-cache-dir fastapi "uvicorn[standard]" httpx tzdata

COPY app/main.py /app/main.py

EXPOSE 8317
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8317/healthz',timeout=4).status==200 else 1)"

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8317", "--log-level", "info"]

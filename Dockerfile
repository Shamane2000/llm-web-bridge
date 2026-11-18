FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir . && rm -rf /root/.cache/pip

COPY gemini_http_worker/ ./gemini_http_worker/

EXPOSE 8000

CMD ["uvicorn", "gemini_http_worker.server:app", "--host", "0.0.0.0", "--port", "8000"]

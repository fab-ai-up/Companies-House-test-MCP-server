# Slim Python base — small image, official, well-supported
FROM python:3.12-slim

# Don't buffer stdout/stderr (matters for Railway logs)
# Don't write .pyc files (smaller image, cleaner container)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install deps first so this layer caches when only app code changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Then app code
COPY server.py .

# Railway injects $PORT at runtime; document the intent here.
# Locally you can override with `docker run -e PORT=8000 -p 8000:8000 ...`
ENV PORT=8000
EXPOSE 8000

# Run the server directly. server.py reads $PORT and binds 0.0.0.0.
CMD ["python", "server.py"]

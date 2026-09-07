# VeteranDesk Quantitative Trading Engine - Koyeb Production Image
FROM python:3.12-slim

# Prevent Python from writing .pyc files and enable unbuffered streaming logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PORT=8000

WORKDIR /app

# Install curl for container health checks
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency specifications first for optimal Docker layer caching
COPY requirements.txt pyproject.toml README.md ./

# Upgrade pip and install all Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir -e .

# Copy application source code
COPY . .

# Expose default HTTP port for Koyeb health checks
EXPOSE 8000

# Container healthcheck instruction
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

# Default run command: launches background trading engine worker with health endpoint
CMD ["python", "worker.py"]

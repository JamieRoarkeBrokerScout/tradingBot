FROM python:3.11-slim

# Install git, Node.js 20, and build tools
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates git && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Force full rebuild — increment to bust Railway's layer cache
ARG CACHEBUST=v42
RUN echo "Cache bust: $CACHEBUST"

# Copy everything so any file change invalidates subsequent layers
COPY . .

# Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Build frontend
RUN cd frontend && npm ci && npm run build

# Data directory — mount a Railway volume at /app/data for persistence
RUN mkdir -p data

EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--timeout", "120", "--access-logfile", "-", "api.server:app"]

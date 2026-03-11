FROM python:3.11-slim

# Install git, Node.js 20, and build tools
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates git && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy everything first so any file change busts the cache correctly
COPY . .

# Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Build frontend
RUN cd frontend && npm ci && npm run build

# Database directory (mount a Railway volume here for persistence)
RUN mkdir -p database

EXPOSE 8080

CMD ["python", "api/server.py"]

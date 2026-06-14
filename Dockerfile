# =============================================================================
# SunnyAI - Combined Backend + Frontend Docker Image
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: Build Frontend
# -----------------------------------------------------------------------------
FROM node:20-alpine AS frontend-builder

WORKDIR /app/frontend

# Copy package files
COPY frontend/package*.json ./

# Install dependencies
RUN npm ci

# Copy frontend source
COPY frontend/ ./

# Set API URL for build (will connect to backend on same container)
ENV NEXT_PUBLIC_API_URL=http://localhost:8000/api

# Build the Next.js application
RUN npm run build

# -----------------------------------------------------------------------------
# Stage 2: Production Image with Backend + Frontend
# -----------------------------------------------------------------------------
FROM python:3.12-slim

# Install Node.js for running Next.js and other dependencies
# Using multiple apt-get update calls to handle transient mirror issues
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    gnupg \
    supervisor \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# -----------------------------------------------------------------------------
# Setup Backend
# -----------------------------------------------------------------------------
COPY backend/requirements.txt /app/backend/
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# Copy backend source
COPY backend/ /app/backend/

# -----------------------------------------------------------------------------
# Setup Frontend (from builder stage)
# -----------------------------------------------------------------------------
COPY --from=frontend-builder /app/frontend/.next /app/frontend/.next
COPY --from=frontend-builder /app/frontend/public /app/frontend/public
COPY --from=frontend-builder /app/frontend/package*.json /app/frontend/
COPY --from=frontend-builder /app/frontend/node_modules /app/frontend/node_modules

# -----------------------------------------------------------------------------
# Supervisor Configuration
# -----------------------------------------------------------------------------
RUN mkdir -p /var/log/supervisor

COPY <<EOF /etc/supervisor/conf.d/sunnyai.conf
[supervisord]
nodaemon=true
logfile=/var/log/supervisor/supervisord.log
pidfile=/var/run/supervisord.pid

[program:backend]
command=python -m uvicorn main:app --host 0.0.0.0 --port 8000
directory=/app/backend
autostart=true
autorestart=true
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0
environment=PYTHONUNBUFFERED=1

[program:frontend]
command=npm start -- -p 3000
directory=/app/frontend
autostart=true
autorestart=true
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0
environment=NEXT_PUBLIC_API_URL=http://localhost:8000/api
EOF

# -----------------------------------------------------------------------------
# Startup Script
# -----------------------------------------------------------------------------
COPY <<EOF /app/start.sh
#!/bin/bash
set -e

echo "========================================="
echo "  SunnyAI - Starting Services"
echo "========================================="
echo ""

# Initialize the database
cd /app/backend
echo "Initializing database..."
python -c "import asyncio; from database import init_db; asyncio.run(init_db())"
echo "Database initialized."
echo ""
echo "Starting services..."
echo "  - Backend API: http://localhost:8000"
echo "  - Frontend UI: http://localhost:3000"
echo "========================================="
echo ""

# Start supervisor (manages both backend and frontend)
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/sunnyai.conf
EOF

RUN chmod +x /app/start.sh

# -----------------------------------------------------------------------------
# Volume for SQLite persistence
# -----------------------------------------------------------------------------
VOLUME /app/backend

# Create non-root user
RUN useradd --create-home --shell /bin/bash sunnyai
USER sunnyai

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV NEXT_PUBLIC_API_URL=http://localhost:8000/api

# Expose ports
EXPOSE 3000 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/docs || exit 1

# Start the application
CMD ["/app/start.sh"]

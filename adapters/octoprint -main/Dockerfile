###############################################################################
# HoneyPrint - Additive Manufacturing Honeypot
# Main Dockerfile
#
# Builds the combined HoneyPrint service containing:
# - OctoPrint web simulation (port 80)
# - Telnet server (port 23)
# - Printer simulator & Marlin firmware emulator
# - Logging client
###############################################################################

FROM python:3.12-slim

# Metadata
LABEL maintainer="HoneyPrint Project"
LABEL description="HoneyPrint - Additive Manufacturing Honeypot Framework"

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc6-dev \
    tini \
    && rm -rf /var/lib/apt/lists/*

# Create app user (don't run as root internally)
RUN useradd -m -s /bin/bash honeyprint

# Setup application directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY honeyprint/ /app/honeyprint/
COPY config/ /app/config/
COPY scripts/ /app/scripts/

# Create data directories
RUN mkdir -p /app/data /app/logs && \
    chown -R honeyprint:honeyprint /app

# Switch to non-root user
USER honeyprint

# Expose ports
EXPOSE 80 23

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:80/api/version')" || exit 1

# Use tini as init
ENTRYPOINT ["tini", "--"]

# Start HoneyPrint
CMD ["python", "-m", "honeyprint.main"]

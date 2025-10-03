# GPS Receivers Scheduler - Production Docker Image
FROM ubuntu:24.04

# Install system dependencies
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    git \
    sudo \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create gpsops user
RUN useradd --system --home-dir /opt/receivers --shell /bin/bash \
    --comment "GPS Receivers Service" gpsops

# Create directory structure
RUN mkdir -p /opt/receivers \
    /opt/gps-config-data \
    /etc/gpsconfig \
    /var/cache/gps_receivers/{logs,tmp} \
    /mnt/gpsdata

# Set working directory
WORKDIR /opt/receivers

# Copy receivers package
COPY . /opt/receivers/

# Install Python packages
RUN python3 -m venv venv && \
    venv/bin/pip install --upgrade pip setuptools wheel && \
    venv/bin/pip install gtimes && \
    venv/bin/pip install git+https://github.com/bennigo/gps_parser.git && \
    venv/bin/pip install -e /opt/receivers

# Set ownership
RUN chown -R gpsops:gpsops /opt/receivers \
    /opt/gps-config-data \
    /etc/gpsconfig \
    /var/cache/gps_receivers \
    /mnt/gpsdata

# Copy entrypoint script
COPY deployment/docker/entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint.sh

# Switch to gpsops user
USER gpsops

# Environment variables
ENV PATH="/opt/receivers/venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

# Expose ports (if needed for monitoring)
EXPOSE 8080

# Health check
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD receivers scheduler status || exit 1

# Entrypoint
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["scheduler", "start"]

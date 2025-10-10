# GPS Receivers Scheduler - Simplified Docker Image
FROM ubuntu:24.04

# Install system dependencies and basic tools
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    git \
    sudo \
    curl \
    vim \
    nano \
    less \
    iputils-ping \
    net-tools \
    dnsutils \
    htop \
    procps \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Create gpsops user with sudo privileges
RUN useradd --system --home-dir /opt/receivers --create-home --shell /bin/bash \
    --comment "GPS Receivers Service" gpsops && \
    echo "gpsops ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# Create directory structure
RUN mkdir -p /opt/receivers \
    /opt/gps-config-data \
    /opt/gtimes \
    /opt/gps_parser \
    /etc/gpsconfig \
    /var/cache/gps_receivers/logs \
    /var/cache/gps_receivers/tmp \
    /mnt/gpsdata

# Set ownership
RUN chown -R gpsops:gpsops /opt/receivers \
    /opt/gps-config-data \
    /opt/gtimes \
    /opt/gps_parser \
    /etc/gpsconfig \
    /var/cache/gps_receivers \
    /mnt/gpsdata

# Copy install and entrypoint scripts
COPY deployment/docker-dev/install.sh /opt/receivers/
COPY deployment/docker-dev/entrypoint.sh /usr/local/bin/
RUN chmod +x /opt/receivers/install.sh /usr/local/bin/entrypoint.sh

# Switch to gpsops user
USER gpsops
WORKDIR /opt/receivers

# Environment variables
ENV PATH="/opt/receivers/venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV GPS_CONFIG_PATH=/etc/gpsconfig

# Health check
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD receivers scheduler status || exit 1

# Entrypoint
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["scheduler", "start", "--verbose"]

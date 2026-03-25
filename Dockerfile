FROM python:3.11-slim-bookworm

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONHASHSEED=random
ENV MALLOC_ARENA_MAX=2
ENV PYTHONMALLOC=malloc
ENV TZ=UTC

WORKDIR /app

# Install needed system packages + Tini
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    libcurl4 \
    libssl-dev \
    make \
    cmake \
    automake \
    autoconf \
    m4 \
    build-essential \
    iproute2 \
    iputils-ping \
    net-tools \
    iptables \
    sudo \
    procps \
    tini \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Create non-root user for the workload
RUN useradd -ms /bin/bash mhddos_user && \
    echo "mhddos_user ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir "showrunner-sdk[full] @ git+https://github.com/rdwr-taly/showrunner-sdk.git@main"

# Copy the application workload
COPY config.json .
COPY start.py .
COPY main.py .
COPY files ./files/

# Create config mount point
RUN mkdir -p /config

# Set permissions
RUN chown -R mhddos_user:mhddos_user /app /config

EXPOSE 9090

# Health check against ShowRunner SDK metrics/health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:9090/healthz || exit 1

# Switch to non-root user
USER mhddos_user

# Use Tini as init, run main.py directly
ENTRYPOINT ["/usr/bin/tini", "--", "python", "main.py"]

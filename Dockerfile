FROM python:3.11-slim-bookworm

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONHASHSEED=random
ENV MALLOC_ARENA_MAX=2
ENV PYTHONMALLOC=malloc
ENV TZ=UTC

WORKDIR /app

# Install needed system packages
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
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Create non-root user for the workload
RUN useradd -ms /bin/bash mhddos_user && \
    echo "mhddos_user ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application workload
COPY config.json .
COPY start.py .
COPY main.py .
COPY files ./files/

# Create config mount point
RUN mkdir -p /config

# SR3: writable dir for the report ShowRunner pulls (/report/report.json).
RUN mkdir -p /report

# Set permissions
RUN chown -R mhddos_user:mhddos_user /app /config /report

EXPOSE 9090

# Health check against ShowRunner SDK metrics/health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:9090/healthz || exit 1

# Switch to non-root user
USER mhddos_user

# Run the SDK entry point directly so it receives the initial SIGHUP reload.
ENTRYPOINT ["python", "main.py"]

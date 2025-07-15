FROM python:3.11-slim-bookworm

# Set environment variables, etc.
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONHASHSEED=random
ENV MALLOC_ARENA_MAX=2
ENV PYTHONMALLOC=malloc
ENV TZ=UTC
ENV CCC_CONFIG_FILE=/app/config.yaml

WORKDIR /app

# Install needed system packages + Tini
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
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
RUN pip install --no-cache-dir -r requirements.txt

# Clone Container Control Core v2.0 from GitHub
RUN git clone --branch v1.0.0 --depth 1 https://github.com/rdwr-taly/container-control.git /tmp/container-control && \
    cp /tmp/container-control/container_control_core.py . && \
    cp /tmp/container-control/app_adapter.py . && \
    rm -rf /tmp/container-control

# Copy the application workload
COPY config.json .
COPY start.py .
COPY files ./files/

# Copy application-specific adapter
COPY mhddos_adapter.py .
COPY config.yaml .

# Set permissions
RUN chown -R mhddos_user:mhddos_user /app

EXPOSE 8080

# Use Tini as the entrypoint to manage the uvicorn process
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "uvicorn", "container_control_core:app", "--host", "0.0.0.0", "--port", "8080", "--loop", "uvloop"]

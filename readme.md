# MHDDoS API Controller

[![License](https://img.shields.io/badge/License-MIT%20(MHDDoS)%20/%20[Specify%20License]%20(Controller)-blue.svg)](LICENSE)
<!-- Add other badges as needed: build status, coverage, etc. -->

A Dockerized Flask application providing a RESTful API to control, schedule, and monitor MHDDoS attack processes, including network shaping (bandwidth/latency) capabilities within the container.

## Key Features

* **Remote Control via REST API:** Manage MHDDoS tasks with a simple Flask-based API.
* **MHDDoS Execution:** Runs the underlying `start.py` script as a dedicated non-root user (`mhddos_user`).
* **Network Shaping:** Apply per-task or default bandwidth (Mbps) and latency (ms) settings via `tc` and `iptables`.
* **Resource Limiting:** Container limits via Docker ENV and process limits using Python’s `resource` module.
* **Flexible Execution Modes:** Immediate single-task, immediate multi-task, scheduled multi-task, or update scheduled tasks.
* **Detailed Monitoring:** View JSON metrics at `/api/metrics` and Prometheus-compatible metrics at `/metrics`.
* **Configurable Auto-Removal & Graceful Shutdown:** Container can self-remove, and graceful termination is supported via `tini`.

## Prerequisites

* **Docker:** See [Docker Installation Guide](https://docs.docker.com/engine/install/).
* **API Client:** Tools like `curl`, Postman, or programming libraries (e.g. Python’s `requests`).

## Installation

1. **Clone the Repository (if applicable):**
   ```bash
   # git clone <your-repo-url>
   # cd <path-to>/containers/mhddos/
   ```
2. **Build the Docker Image:**
   ```bash
   docker build -t mhddos-api-controller .
   ```
   *(You can substitute `mhddos-api-controller` with your preferred image tag.)*

## Usage / User Guide

### 1. Running the Container

Start the container (mapping API port 8080):
```bash
docker run -d \
  -p 8080:8080 \
  --name mhddos-controller \
  --cap-add=NET_ADMIN \
  mhddos-api-controller
```
**Flags Explained:**
- `-d`: Run in detached mode.
- `-p 8080:8080`: Map host port 8080 to container port 8080.
- `--name`: Assign a container name.
- `--cap-add=NET_ADMIN`: Required for network shaping via `tc` and `iptables`.

### 2. API Endpoints

Interact with the API at `http://localhost:8080` (or your Docker host's IP).

#### GET /api/health
Checks if the API server is running.

**Example:**
```bash
curl http://localhost:8080/api/health
```
**Response:**
```json
{
  "status": "healthy"
}
```

#### POST /api/network/update
Updates the default network settings (bandwidth and latency).

**Request Body (JSON):**
```json
{
  "bandwidth": 50,
  "latency": 20
}
```
**Example:**
```bash
curl -X POST -H "Content-Type: application/json" \
  -d '{"bandwidth": 50, "latency": 20}' \
  http://localhost:8080/api/network/update
```
**Response (200 OK):**
```json
{
  "status": "success",
  "message": "Network settings updated",
  "settings": {
    "bandwidth": "50 Mbps",
    "latency": "20 ms"
  }
}
```
**Error Response (500):**
```json
{
  "status": "error",
  "message": "Failed to apply network settings"
}
```

#### GET /api/metrics
Retrieves detailed metrics in JSON format.

**Example:**
```bash
curl http://localhost:8080/api/metrics
```
**Response (200 OK):**  
*Includes values for timestamp, app_status, network, system, and task metrics.*

*Application statuses:*  
- initializing  
- running  
- stopped  
- active

#### GET /metrics
Provides Prometheus-formatted metrics.

**Example:**
```bash
curl http://localhost:8080/metrics
```
**Response (200 OK, text/plain):**  
*Example output with HELP and TYPE declarations for CPU, memory, network, etc.*

#### POST /api/start
Starts or schedules MHDDoS tasks. (Any running task/schedule is stopped before a new one starts.)

**Common Parameter:**  
- `auto_remove`:  
  - `true` for immediate modes  
  - `false` for scheduled mode

**Mode 1: Immediate Single-Task**

**Request Body (JSON):**
```json
{
  "Method": "GET",
  "Target URL": "http://target-domain.com",
  "Type": "HTTP",
  "Threads": "1000",
  "Proxy List File": "proxies.txt",
  "RPC": "2000",
  "Duration (seconds)": "120",
  "throughput_in_mbps": 100,
  "latency_in_ms": 50,
  "auto_remove": true
}
```
**Example:**
```bash
curl -X POST -H "Content-Type: application/json" \
  -d '{ "Method": "GET", "Target URL": "http://target-domain.com", "Type": "HTTP", "Threads": "1000", "Proxy List File": "proxies.txt", "RPC": "2000", "Duration (seconds)": "120", "throughput_in_mbps": 100, "latency_in_ms": 50, "auto_remove": true }' \
  http://localhost:8080/api/start
```
**Response:**
```json
{
  "message": "Application started"
}
```

**Mode 2: Immediate Multi-Task**

**Request Body (JSON):**
```json
{
  "sub_tasks": [
    {
      "name": "AttackPhase1-GET",
      "Method": "GET",
      "Target URL": "http://target1.com",
      "Type": "HTTP",
      "Threads": "500",
      "Proxy List File": "proxies1.txt",
      "RPC": "1000",
      "Duration (seconds)": "60",
      "throughput_in_mbps": 50,
      "latency_in_ms": 10
    },
    {
      "name": "AttackPhase2-TCP",
      "Method": "TCP",
      "Target URL": "target2.com:80",
      "Type": "",
      "Threads": "800",
      "Proxy List File": "proxies2.txt",
      "RPC": "1500",
      "Duration (seconds)": "90"
    }
  ],
  "auto_remove": true
}
```
**Example:**
```bash
curl -X POST -H "Content-Type: application/json" \
  -d '{ "sub_tasks": [ { "name": "AttackPhase1-GET", "Method": "GET", "Target URL": "http://target1.com", "Type": "HTTP", "Threads": "500", "Proxy List File": "proxies1.txt", "RPC": "1000", "Duration (seconds)": "60", "throughput_in_mbps": 50, "latency_in_ms": 10 }, { "name": "AttackPhase2-TCP", "Method": "TCP", "Target URL": "target2.com:80", "Threads": "800", "Proxy List File": "proxies2.txt", "RPC": "1500", "Duration (seconds)": "90" } ], "auto_remove": true }' \
  http://localhost:8080/api/start
```
**Response:**
```json
{
  "message": "Attacks started in immediate mode"
}
```

**Mode 3: Scheduled Multi-Task**

**Request Body (JSON):**
```json
{
  "cron_schedule": "1h",
  "start_time": "2024-08-15T09:00:00",
  "sub_tasks": [
    {
      "name": "HourlyCheck-GET",
      "Method": "GET",
      "Target URL": "http://monitor-target.com",
      "Type": "HTTP",
      "Threads": "100",
      "Proxy List File": "proxies-monitor.txt",
      "RPC": "500",
      "Duration (seconds)": "30",
      "throughput_in_mbps": 20,
      "latency_in_ms": 5
    },
    {
      "name": "HourlyCheck-POST",
      "Method": "POST",
      "Target URL": "http://monitor-target.com/api",
      "Type": "HTTP",
      "Threads": "50",
      "Proxy List File": "proxies-monitor.txt",
      "RPC": "300",
      "Duration (seconds)": "45"
    }
  ],
  "auto_remove": false
}
```
**Example:**
```bash
curl -X POST -H "Content-Type: application/json" \
  -d '{ "cron_schedule": "1h", "start_time": "2024-08-15T09:00:00", "sub_tasks": [ { "name": "HourlyCheck-GET", ... }, { "name": "HourlyCheck-POST", ... } ], "auto_remove": false }' \
  http://localhost:8080/api/start
```
**Response:**
```json
{
  "message": "Cron job scheduled: 1h starting at 2024-08-15T09:00:00"
}
```

**Mode 4: Update Scheduled Tasks**

**Request Body (JSON):**
```json
{
  "sub_tasks": [
    {
      "name": "UpdatedAttack-STRESS",
      "Method": "STRESS",
      "Target URL": "target-stress.com:443",
      "Type": "",
      "Threads": "1500",
      "Proxy List File": "proxies-stress.txt",
      "RPC": "3000",
      "Duration (seconds)": "180",
      "throughput_in_mbps": 200
    }
  ]
}
```
**Example:**
```bash
curl -X POST -H "Content-Type: application/json" \
  -d '{ "sub_tasks": [ { "name": "UpdatedAttack-STRESS", "Method": "STRESS", "Target URL": "target-stress.com:443", "Threads": "1500", "Proxy List File": "proxies-stress.txt", "RPC": "3000", "Duration (seconds)": "180", "throughput_in_mbps": 200 } ] }' \
  http://localhost:8080/api/start
```
**Response:**
```json
{
  "message": "Cron job sub-task configurations updated."
}
```

#### POST /api/stop
Stops the current MHDDoS task or cancels an active schedule.

**Example:**
```bash
curl -X POST http://localhost:8080/api/stop
```
**Response:**
```json
{
  "message": "Application stopped"
  // or "Cron mode stopped" depending on the state
}
```

### 3. Graceful Shutdown

Stop the container gracefully:
```bash
docker stop mhddos-controller
```

## Configuration

**Network Shaping:**  
- Defaults: 10 Mbps bandwidth, 0 ms latency.  
- Override via `/api/network/update` or task-specific settings in `/api/start`.

**Memory Limits:**  
- Container limits set via Docker environment variables.  
- Process limits set in `container_control.py`.

**MHDDoS Execution User:**  
- Executes `start.py` using `sudo -u mhddos_user`.  
- Requires passwordless sudo configuration.

**Timezone:**  
- Set to UTC on startup.

## Monitoring

- **GET /api/metrics:** Detailed JSON metrics.
- **GET /metrics:** Prometheus text metrics.

To configure Prometheus, add the following to your `prometheus.yml`:
```yaml
scrape_configs:
  - job_name: 'mhddos_controller'
    static_configs:
      - targets: ['<docker_host_ip>:8080']
```

## Project Structure

```
containers/mhddos/
├── Dockerfile             # Defines the Docker image build process
├── container_control.py   # Main Flask application logic (API, scheduling, process control)
├── entrypoint.sh          # Init script run by Tini to start container_control.py
├── requirements.txt       # Python dependencies for MHDDoS and the controller
└── readme.md              # This documentation file
```
*(Note: The MHDDoS tool itself is cloned into `/app` during the Docker build.)*

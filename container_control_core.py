# container_control_core.py (v2.0 - with optional core services)
from __future__ import annotations

import importlib
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from string import Template
from types import ModuleType
from typing import Any, Dict, List, Optional

import psutil
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, RootModel
from ruamel.yaml import YAML

# ---------- Logging (UTC) -------------------------------------------------- #
logging.Formatter.converter = time.gmtime  # type: ignore[attr-defined]
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)sZ %(levelname)s %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("container_control_core")

# ---------- Load YAML configuration --------------------------------------- #
try:
    CFG_PATH = Path(os.getenv("CCC_CONFIG_FILE", "config.yaml"))
    CFG = YAML(typ="safe").load(CFG_PATH.read_text())
except FileNotFoundError:
    log.critical(f"Configuration file not found at '{CFG_PATH}'. Exiting.")
    sys.exit(1)
except Exception as e:
    log.critical(f"Error loading or parsing config file: {e}")
    sys.exit(1)

# --- Core Adapter Config ---
ADAPTER_CFG = CFG.get("adapter", {})
ADAPTER_PATH = ADAPTER_CFG.get("class")
PRIMARY_KEY = ADAPTER_CFG.get("primary_payload_key")
RUN_AS_USER = ADAPTER_CFG.get("run_as_user")

if not ADAPTER_PATH:
    log.critical("`adapter.class` not defined in config.yaml. Exiting.")
    sys.exit(1)

# --- Optional Core Service Configurations ---
PROC_MAN_CFG = CFG.get("process_management", {})
METRICS_CFG = CFG.get("metrics", {})
TC_CFG = CFG.get("traffic_control", {})
PRIV_CMD_CFG = CFG.get("privileged_commands", {})


# ---------- Dynamic import of the Adapter ---------------------------------- #
modname, clsname = ADAPTER_PATH.rsplit(".", 1)
try:
    mod: ModuleType = importlib.import_module(modname)
    AdapterCls = getattr(mod, clsname)
except Exception as exc:  # noqa: BLE001
    log.critical("Cannot import adapter %s: %s", ADAPTER_PATH, exc)
    sys.exit(1)

adapter = AdapterCls(ADAPTER_CFG)


# ---------- Runtime state -------------------------------------------------- #
state: Dict[str, Any] = {
    "app_status": "initializing",
    "container_status": "running",
    "core_managed_process_pid": None,
    "last_start_payload": None,
}
# Opaque handle for adapter-managed processes or the core's Popen object
current_handle: Optional[Any] = None


# ---------- Helpers -------------------------------------------------------- #
def _now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _ensure_user(cmd: list[str]) -> list[str]:
    """Prepend sudo command if dropping privileges is required."""
    if RUN_AS_USER and os.geteuid() == 0:
        return ["sudo", "-E", "-u", RUN_AS_USER, "--"] + cmd
    return cmd


def _thread(fn, *args):  # fire-and-forget helper
    t = threading.Thread(target=fn, args=args, daemon=True)
    t.start()
    return t


# ---------- Core Service Logic --------------------------------------------- #

def _run_privileged_commands(stage: str):
    """Run pre-start or post-stop commands defined in the config."""
    commands = PRIV_CMD_CFG.get(stage, [])
    if not commands:
        return
    log.info(f"Running privileged '{stage}' commands...")
    for cmd in commands:
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            log.info(f"Successfully ran: {' '.join(cmd)}")
        except subprocess.CalledProcessError as e:
            log.error(f"Error running privileged command: {' '.join(cmd)}\n{e.stderr}")
        except Exception as e:
            log.error(f"Failed to run privileged command {' '.join(cmd)}: {e}")


def _apply_traffic_control(payload: dict):
    """Apply tc qdisc rules based on config and payload."""
    if not TC_CFG.get("enabled"):
        return

    # Allow adapter to modify payload before applying TC
    if hasattr(adapter, "on_before_core_traffic_control"):
        payload = adapter.on_before_core_traffic_control(payload)

    bw_key = TC_CFG.get("bandwidth_mbps_key")
    lat_key = TC_CFG.get("latency_ms_key")

    bandwidth = payload.get(bw_key, TC_CFG.get("default_bandwidth_mbps"))
    latency = payload.get(lat_key, TC_CFG.get("default_latency_ms", 0))

    if not bandwidth:
        return

    iface = TC_CFG.get("interface", "eth0")
    try:
        # Clear existing rules first for idempotency
        subprocess.run(["tc", "qdisc", "del", "dev", iface, "root"], check=False, stderr=subprocess.PIPE)
        # Apply new rules
        burst = int(bandwidth * 1000 / 8) # Sensible default burst
        cmd = [
            "tc", "qdisc", "add", "dev", iface, "root", "tbf",
            "rate", f"{bandwidth}mbit", "burst", f"{burst}k", "latency", f"{latency}ms"
        ]
        subprocess.run(cmd, check=True)
        log.info(f"Applied traffic control to {iface}: {bandwidth}Mbps, {latency}ms latency.")
    except Exception as e:
        log.warning(f"Failed to apply traffic control: {e}")


def _remove_traffic_control():
    """Remove tc qdisc rules."""
    if not TC_CFG.get("enabled"):
        return
    iface = TC_CFG.get("interface", "eth0")
    try:
        subprocess.run(["tc", "qdisc", "del", "dev", iface, "root"], check=False, stderr=subprocess.PIPE)
        log.info(f"Removed traffic control from {iface}.")
    except Exception as e:
        log.warning(f"Failed to remove traffic control: {e}")


def _monitor_core_process(proc: subprocess.Popen):
    """Thread target to wait for process exit and call adapter hook."""
    return_code = proc.wait()
    log.info(f"Core-managed process PID {proc.pid} exited with code {return_code}.")
    state["core_managed_process_pid"] = None
    state["app_status"] = "stopped" if return_code == 0 else "error"
    if hasattr(adapter, "on_core_process_exit"):
        try:
            # Reading stdout/stderr might not be possible depending on how Popen was called
            stdout, stderr = proc.communicate()
            adapter.on_core_process_exit(return_code, stdout, stderr)
        except Exception as e:
            log.error(f"Error in adapter's on_core_process_exit hook: {e}")


def _start_core_managed_process(payload: dict) -> subprocess.Popen:
    """Handles the creation of a process managed by the core."""
    cmd_factory = PROC_MAN_CFG.get("command_factory")
    cmd_template = PROC_MAN_CFG.get("command")

    final_cmd: Optional[List[str]] = None
    if cmd_factory:
        log.info(f"Using adapter's command factory: {cmd_factory}")
        try:
            modname, funcname = cmd_factory.rsplit(".", 1)
            factory_mod = importlib.import_module(modname)
            factory_func = getattr(factory_mod, funcname)
            # Check if it is a method of the adapter instance
            if hasattr(adapter, funcname):
                 final_cmd = getattr(adapter, funcname)(payload)
            else: # Static or module-level function
                 final_cmd = factory_func(payload)
        except Exception as e:
            raise RuntimeError(f"Failed to execute command_factory: {e}") from e
    elif cmd_template:
        log.info("Using command template from config")
        try:
            # Simple substitution from payload's top-level keys
            # e.g., command: ["my-app", "--host", "{host}", "--port", "{port}"]
            template = Template(" ".join(cmd_template))
            rendered_cmd = template.safe_substitute(payload)
            final_cmd = rendered_cmd.split()
        except Exception as e:
            raise RuntimeError(f"Failed to render command template: {e}") from e

    if not final_cmd:
        raise RuntimeError("Process management is enabled, but no valid command could be built.")

    secure_cmd = _ensure_user(final_cmd)
    log.info(f"Executing core-managed command: {' '.join(secure_cmd)}")
    proc = subprocess.Popen(secure_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    state["core_managed_process_pid"] = proc.pid
    _thread(_monitor_core_process, proc)
    return proc


# ---------- FastAPI App ---------------------------------------------------- #
app = FastAPI(title="Container Control Core", version="2.0")

class StartBody(RootModel[Dict[str, Any]]): pass
class UpdateBody(RootModel[Dict[str, Any]]): pass
class StopBody(BaseModel): force: Optional[bool] = False


# ---------- Lifecycle glue ------------------------------------------------- #
def _start(payload: dict):
    global current_handle
    state["last_start_payload"] = payload
    try:
        # 1. Run privileged pre-start commands (if any)
        _run_privileged_commands("pre_start")

        # 2. Apply Traffic Control (if enabled)
        _apply_traffic_control(payload)

        # 3. Call adapter's traditional pre-start hooks
        adapter.pre_start_hooks(payload)

        # 4. Start the main application process
        if PROC_MAN_CFG.get("enabled"):
            log.info("Core process management is enabled.")
            current_handle = _start_core_managed_process(payload)
        else:
            log.info("Delegating start to adapter.")
            current_handle = adapter.start(payload, ensure_user=_ensure_user)

        state["app_status"] = "running"
        log.info("Start sequence completed successfully.")
    except Exception:
        log.exception("Start failed")
        state["app_status"] = "error"
        # Ensure cleanup on failed start
        _stop(is_cleanup=True)


def _stop(is_cleanup: bool = False):
    global current_handle
    if not is_cleanup:
        log.info("Stop sequence initiated.")

    try:
        # 1. Stop the main application process
        if PROC_MAN_CFG.get("enabled") and isinstance(current_handle, subprocess.Popen):
            if current_handle.poll() is None:
                log.info(f"Terminating core-managed process PID {current_handle.pid}...")
                current_handle.terminate()
                try:
                    current_handle.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    log.warning(f"Process {current_handle.pid} did not terminate, killing.")
                    current_handle.kill()
        elif hasattr(adapter, "stop"):
            log.info("Delegating stop to adapter.")
            adapter.stop()

        # 2. Call adapter's traditional post-stop hooks
        adapter.post_stop_hooks()

        # 3. Remove Traffic Control
        _remove_traffic_control()

        # 4. Run privileged post-stop commands
        _run_privileged_commands("post_stop")

        state["app_status"] = "stopped"
        state["core_managed_process_pid"] = None
        current_handle = None
        if not is_cleanup:
            log.info("Stop sequence completed.")

    except Exception:
        log.exception("Stop failed")
        state["app_status"] = "error"


# ---------- API endpoints -------------------------------------------------- #
@app.get("/api/health")
async def health():
    return {"status": "healthy", "app_status": state["app_status"]}


@app.post("/api/start")
async def api_start(body: StartBody):
    payload = body.root
    if PRIMARY_KEY and PRIMARY_KEY not in payload:
        raise HTTPException(400, f"Missing primary payload key '{PRIMARY_KEY}'")
    if state["app_status"] == "running":
        _stop()
        time.sleep(1) # Give time for resources to be released
    state["app_status"] = "initializing"
    _thread(_start, payload)
    return {"message": "start initiated"}


@app.post("/api/update")
async def api_update(body: UpdateBody):
    payload = body.root
    if state["app_status"] != "running":
        raise HTTPException(400, "Application not running, cannot update.")

    try:
        # The adapter is always responsible for handling updates.
        updated = adapter.update(payload)
        if updated:
            return {"message": "update applied"}
        raise HTTPException(409, "adapter declined update")
    except NotImplementedError:
        raise HTTPException(409, "live-update not supported by this adapter") from None
    except Exception as exc:
        log.exception("adapter.update failed")
        raise HTTPException(500, str(exc))


@app.post("/api/stop")
async def api_stop(_: StopBody):
    if state["app_status"] != "running":
        return {"message": "application not running, nothing to stop"}
    _thread(_stop)
    return {"message": "stop initiated"}


@app.get("/api/metrics")
async def api_metrics():
    base_metrics = {
        "timestamp": _now(),
        "app_status": state["app_status"],
        "container_status": state["container_status"],
    }

    # --- Core-provided metrics ---
    # System metrics are always included
    cpu = psutil.cpu_percent()
    mem = psutil.virtual_memory()
    base_metrics["system"] = dict(
        cpu_percent=round(cpu, 1),
        memory_percent=round(mem.percent, 1),
        memory_available_mb=round(mem.available / 1_048_576, 2),
        memory_used_mb=round(mem.used / 1_048_576, 2),
    )

    # Core-managed network metrics
    if METRICS_CFG.get("network_monitoring", {}).get("enabled"):
        net = psutil.net_io_counters()
        base_metrics["network"] = dict(
            bytes_sent=net.bytes_sent, bytes_recv=net.bytes_recv,
            packets_sent=net.packets_sent, packets_recv=net.packets_recv,
        )

    # Core-managed process metrics
    proc_pid = state.get("core_managed_process_pid")
    if METRICS_CFG.get("process_monitoring", {}).get("enabled") and proc_pid:
        try:
            proc = psutil.Process(proc_pid)
            base_metrics["process"] = {
                "pid": proc.pid,
                "cpu_percent": proc.cpu_percent(),
                "memory_mb": round(proc.memory_info().rss / 1_048_576, 2),
                "threads": proc.num_threads(),
                "status": proc.status(),
            }
        except psutil.NoSuchProcess:
            state["core_managed_process_pid"] = None # Stale PID
            base_metrics["process"] = {"status": "terminated"}

    # --- Adapter-provided metrics ---
    base_metrics["adapter_metrics"] = adapter.get_metrics() or {}

    return JSONResponse(base_metrics)


@app.get("/metrics")
async def prom_metrics():
    # Prometheus format metrics
    lines = []
    
    # System Metrics
    mem, cpu = psutil.virtual_memory(), psutil.cpu_percent()
    lines.extend([
        "# HELP container_cpu_percent CPU usage %", f"container_cpu_percent {cpu}",
        "# HELP container_memory_percent Mem usage %", f"container_memory_percent {mem.percent}",
        "# HELP container_memory_used_bytes Used bytes", f"container_memory_used_bytes {mem.used}",
    ])
    
    # Core-managed Network Metrics
    if METRICS_CFG.get("network_monitoring", {}).get("enabled"):
        net = psutil.net_io_counters()
        lines.extend([
            "# HELP container_network_bytes_sent_total Bytes sent", f"container_network_bytes_sent_total {net.bytes_sent}",
            "# HELP container_network_bytes_recv_total Bytes recv", f"container_network_bytes_recv_total {net.bytes_recv}",
        ])

    # Adapter-specific Prometheus metrics
    if hasattr(adapter, "prometheus_metrics"):
        lines.extend(adapter.prometheus_metrics())
        
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


# ---------- Graceful shutdown --------------------------------------------- #
def _handle_exit_signal(_s, _f):
    log.info("Exit signal received, initiating graceful shutdown.")
    if state["app_status"] == "running":
        _stop()
    sys.exit(0)


signal.signal(signal.SIGTERM, _handle_exit_signal)
signal.signal(signal.SIGINT, _handle_exit_signal)


if __name__ == "__main__":
    uvicorn.run("container_control_core:app", host="0.0.0.0", port=8080, loop="uvloop")

"""
MHDDoS Plus — ShowRunner-managed entry point.

Replaces the old container-control adapter with showrunner-sdk:
- Config from /config/app.json (reloaded on SIGHUP)
- Prometheus metrics on :9090/metrics
- Health on :9090/healthz
- Subprocess management and cron scheduling preserved from adapter
"""

from __future__ import annotations
import os
import json
import subprocess
import psutil
import threading
import time
import signal
import logging
import schedule
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

from showrunner_sdk import config, metrics, health

import report_writer

CONFIG_PATH = "/config/app.json"
STARTUP_CONFIG_WAIT_SECONDS = 10
# SR3: run-stats sink that start.py subprocesses write; folded into the report.
RUN_STATS_PATH = os.getenv("SR_RUN_STATS_PATH", "/report/run_stats.json")

# ── Logging ──
logging.basicConfig(
    format="[%(asctime)s - %(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger("mhddos-plus")

# ── App-specific Prometheus metrics ──
incoming_mbps_gauge = metrics.gauge(
    "mhddos_incoming_throughput_mbps", "Incoming throughput in Mbps"
)
outgoing_mbps_gauge = metrics.gauge(
    "mhddos_outgoing_throughput_mbps", "Outgoing throughput in Mbps"
)
cron_active_gauge = metrics.gauge(
    "mhddos_cron_active", "Whether cron scheduling is active"
)
sub_tasks_count_gauge = metrics.gauge(
    "mhddos_sub_tasks_count", "Number of configured sub-tasks"
)
running_task_info = metrics.gauge(
    "mhddos_running_task", "1 if a task is currently running, 0 otherwise"
)
metrics.set_app_info(name="mhddos-plus", version="2.0.0")

# ── Lifecycle ──
shutdown_event = threading.Event()


class MhddosManager:
    """
    Manages MHDDoS attack subprocesses, cron scheduling, and traffic control.

    Ported from the old MhDosAdapter, but runs standalone instead of being
    wrapped by the container-control framework.
    """

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.auto_remove: bool = True
        self.cron_schedule: str | None = None
        self.cron_active: bool = False
        self.next_run_time: datetime | None = None
        self.sub_tasks: List[Dict[str, Any]] = []
        self.running_task_name: str | None = None
        self.stop_requested: bool = False
        self.cron_thread: threading.Thread | None = None
        self._prev_net_io = None
        self._prev_time = None
        # ── SR3 report accumulators (session-wide volume) ──
        self._session_requests: int = 0
        self._session_bytes: int = 0
        self._peak_outgoing_mbps: float = 0.0
        self._last_target: str | None = None
        self._report_written: bool = False

    # ------------------------------------------------------------------ #
    # Start / Stop / Update
    # ------------------------------------------------------------------ #

    def start(self, cfg: Dict[str, Any]) -> None:
        """Begin attacks based on the provided config."""
        log.info("Start request received.")
        self.stop_requested = False

        # Backward compat: wrap bare payload into sub_tasks list
        if "sub_tasks" not in cfg:
            cfg = {"sub_tasks": [cfg]}

        sub_tasks = cfg.get("sub_tasks")
        if not isinstance(sub_tasks, list):
            if sub_tasks is not None:
                log.warning("'sub_tasks' is not a list, ignoring value")
            sub_tasks = []
        self.sub_tasks = sub_tasks
        sub_tasks_count_gauge.set(len(self.sub_tasks))

        cron_schedule = cfg.get("cron_schedule")
        start_time = cfg.get("start_time")

        # Mode decision: Cron vs Immediate
        if cron_schedule and start_time:
            health.set_status("active")
            self.auto_remove = cfg.get("auto_remove", False)
            if not self._setup_cron(cron_schedule, start_time):
                health.set_status("error", reason="Failed to schedule cron job")
                raise RuntimeError("Failed to schedule cron job")
        else:
            self.auto_remove = cfg.get("auto_remove", True)
            self.cron_active = False
            cron_active_gauge.set(0)
            schedule.clear()

            health.set_status("running")
            attack_thread = threading.Thread(
                target=self._run_attacks,
                args=(self.sub_tasks,),
                daemon=True,
            )
            attack_thread.start()

    def stop(self) -> None:
        """Stop all running attacks and cron scheduling."""
        log.info("Stop request received.")
        self.stop_requested = True
        health.set_status("stopped")

        # Stop cron
        if self.cron_active:
            self.cron_active = False
            cron_active_gauge.set(0)
            schedule.clear()
            log.info("Cron scheduler stopped.")
            if self.cron_thread and self.cron_thread.is_alive():
                self.cron_thread.join(timeout=2)
            self.cron_thread = None
            self.next_run_time = None

        # Stop running process
        if self.proc and self.proc.poll() is None:
            try:
                log.info("Terminating process PID %d", self.proc.pid)
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.warning("Process did not terminate gracefully, killing.")
                self.proc.kill()
                self.proc.wait(timeout=2)
            except Exception as e:
                log.error("Error during process termination: %s", e)

        self.proc = None
        self.running_task_name = None
        running_task_info.set(0)
        log.info("Stop request processed.")

        # Auto-remove: exit the container
        if self.auto_remove and not self.cron_active:
            log.info("auto_remove=True, container will self-terminate.")
            threading.Thread(target=self._self_terminate, daemon=True).start()

    def update_config(self, new_cfg: Dict[str, Any]) -> None:
        """Handle a config reload (SIGHUP). Update sub_tasks if cron is active."""
        new_sub_tasks = new_cfg.get("sub_tasks")
        if self.cron_active and isinstance(new_sub_tasks, list):
            self.sub_tasks = new_sub_tasks
            sub_tasks_count_gauge.set(len(self.sub_tasks))
            log.info("Cron sub-tasks updated with %d tasks.", len(new_sub_tasks))
        elif not self.cron_active:
            # Full restart: stop current, start with new config
            log.info("Config reloaded while not in cron mode — restarting attacks.")
            self.stop()
            self.start(new_cfg)
        else:
            log.warning("Config reload did not contain valid 'sub_tasks' list.")

    # ------------------------------------------------------------------ #
    # Private: subprocess management
    # ------------------------------------------------------------------ #

    def _format_start_args(self, json_data: dict) -> list:
        return [
            str(json_data.get("Method", "")),
            str(json_data.get("Target URL", "")),
            str(json_data.get("Type", "")),
            str(json_data.get("Threads", "")),
            str(json_data.get("Proxy List File", "")),
            str(json_data.get("RPC", "")),
            str(json_data.get("Duration (seconds)", "")),
        ]

    def _launch_main_process(self, args: List[str]) -> bool:
        try:
            cmd = ["python3", "start.py"] + args
            self.proc = subprocess.Popen(cmd)
            log.info("Started process PID=%d (args=%s).", self.proc.pid, args)
            return True
        except Exception as e:
            log.error("Failed to launch process: %s", e)
            self.proc = None
            return False

    def _run_attacks(self, sub_tasks: List[Dict]) -> None:
        health.set_status("running")
        running_task_info.set(1)
        for task_config in sub_tasks:
            if self.stop_requested:
                log.info("Stop requested, aborting attack sequence.")
                break

            self.running_task_name = task_config.get("name", "Unnamed Task")
            # SR3: remember the target for the report's reachability probe.
            target_url = task_config.get("Target URL") or task_config.get("target")
            if target_url:
                self._last_target = str(target_url)
            log.info("Starting task: %s", self.running_task_name)

            # Apply per-task traffic control before launching
            self._apply_per_task_traffic_control(task_config)

            args = self._format_start_args(task_config)
            if not self._launch_main_process(args):
                log.error("Failed to launch attack, stopping sequence.")
                break

            # Wait for process to finish or stop requested
            while self.proc and self.proc.poll() is None:
                if self.stop_requested:
                    self.stop()
                    break
                self._update_throughput_metrics()
                time.sleep(0.5)

            if self.proc:
                log.info(
                    "Task '%s' finished with code %s.",
                    self.running_task_name,
                    self.proc.poll(),
                )
                self.proc = None

            # SR3: fold this phase's volume totals into the session report.
            self._fold_run_stats()

        # Cleanup after all attacks
        self.running_task_name = None
        running_task_info.set(0)
        if not self.stop_requested:
            if self.cron_active:
                health.set_status("active")
                log.info("Attack sequence finished, cron remains active.")
            else:
                health.set_status("stopped")
                log.info("Immediate attack sequence finished.")
                if self.auto_remove:
                    self._self_terminate()
        else:
            health.set_status("stopped")

    # ------------------------------------------------------------------ #
    # Private: cron scheduling
    # ------------------------------------------------------------------ #

    def _setup_cron(self, schedule_str: str, start_time_str: str) -> bool:
        schedule.clear()
        try:
            start_time = datetime.fromisoformat(
                start_time_str.replace("Z", "+00:00")
            )
            if start_time.tzinfo:
                start_time = start_time.astimezone(timezone.utc).replace(tzinfo=None)

            # Parse schedule string: e.g. "30m", "1h", "5s", "1d"
            unit = schedule_str[-1]
            val = int(schedule_str[:-1])

            job = schedule.every(val)
            if unit == "s":
                job = job.seconds
            elif unit == "m":
                job = job.minutes
            elif unit == "h":
                job = job.hours
            elif unit == "d":
                job = job.days
            else:
                raise ValueError("Invalid schedule unit: %s" % unit)

            # Align to start time for daily/hourly schedules
            at_str = None
            if unit == "d":
                at_str = f"{start_time.hour:02d}:{start_time.minute:02d}"
            elif unit == "h":
                at_str = f":{start_time.minute:02d}"

            if at_str:
                job.at(at_str)

            job.do(self._run_attacks, sub_tasks=self.sub_tasks)

            # Force the first run to respect the provided start time
            next_run = start_time
            if next_run.tzinfo:
                next_run = next_run.astimezone(timezone.utc).replace(tzinfo=None)
            now = datetime.utcnow()
            delta = timedelta(**{job.unit: job.interval})
            while next_run < now:
                next_run += delta
            job.next_run = next_run

            self.next_run_time = None
            nr = schedule.next_run()
            if nr:
                if nr.tzinfo:
                    nr = nr.astimezone(timezone.utc)
                else:
                    nr = nr.replace(tzinfo=timezone.utc)
                self.next_run_time = nr
            self.cron_active = True
            self.cron_schedule = schedule_str
            cron_active_gauge.set(1)
            health.set_status("active")
            log.info(
                "Cron job scheduled: %s. Next run: %s",
                schedule_str,
                self.next_run_time,
            )

            if not self.cron_thread or not self.cron_thread.is_alive():
                self.cron_thread = threading.Thread(
                    target=self._run_cron, daemon=True
                )
                self.cron_thread.start()
            return True

        except Exception as e:
            log.error("Error scheduling cron job: %s", e)
            health.set_status("error", reason=str(e))
            return False

    def _run_cron(self) -> None:
        log.info("Cron thread started.")
        while self.cron_active:
            schedule.run_pending()
            if not self.proc and not self.stop_requested:
                health.set_status("active")
            self.next_run_time = None
            nr = schedule.next_run()
            if nr:
                if nr.tzinfo:
                    nr = nr.astimezone(timezone.utc)
                else:
                    nr = nr.replace(tzinfo=timezone.utc)
                self.next_run_time = nr
            time.sleep(1)
        log.info("Cron thread finished.")

    # ------------------------------------------------------------------ #
    # Private: traffic control
    # ------------------------------------------------------------------ #

    def _apply_per_task_traffic_control(self, task_config: Dict[str, Any]) -> None:
        """Apply tc bandwidth/latency shaping for a specific task."""
        bandwidth = task_config.get("throughput_in_mbps", 10)
        latency = task_config.get("latency_in_ms", 0)

        if not bandwidth:
            return

        iface = "eth0"
        try:
            # Clear existing rules first
            subprocess.run(
                ["tc", "qdisc", "del", "dev", iface, "root"],
                check=False,
                stderr=subprocess.PIPE,
            )

            # Apply new rules
            burst = max(32, int(bandwidth * 1000 / 8))  # Sensible burst
            latency_val = max(1, int(latency))
            cmd = [
                "tc", "qdisc", "add", "dev", iface, "root", "tbf",
                "rate", f"{bandwidth}mbit",
                "burst", f"{burst}k",
                "latency", f"{latency_val}ms",
            ]
            subprocess.run(cmd, check=True)
            log.info(
                "Applied per-task traffic control: %sMbps, %sms latency.",
                bandwidth,
                latency_val,
            )
        except subprocess.CalledProcessError as e:
            log.warning("Failed to apply per-task traffic control: %s", e)
        except Exception as e:
            log.warning("Error applying per-task traffic control: %s", e)

    # ------------------------------------------------------------------ #
    # Private: throughput metrics
    # ------------------------------------------------------------------ #

    def _calculate_throughput(self) -> tuple:
        try:
            now = time.time()
            current_io = psutil.net_io_counters()
            if self._prev_time is None or self._prev_net_io is None:
                self._prev_time = now
                self._prev_net_io = current_io
                return None, None

            delta = now - self._prev_time
            if delta == 0:
                return 0.0, 0.0

            sent_mbps = (
                (current_io.bytes_sent - self._prev_net_io.bytes_sent) * 8
            ) / (delta * 1_000_000)
            recv_mbps = (
                (current_io.bytes_recv - self._prev_net_io.bytes_recv) * 8
            ) / (delta * 1_000_000)

            self._prev_time = now
            self._prev_net_io = current_io
            return round(recv_mbps, 2), round(sent_mbps, 2)
        except Exception:
            return None, None

    def _update_throughput_metrics(self) -> None:
        incoming, outgoing = self._calculate_throughput()
        if incoming is not None:
            incoming_mbps_gauge.set(incoming)
        if outgoing is not None:
            outgoing_mbps_gauge.set(outgoing)
            # SR3: track the peak for the report.
            if outgoing > self._peak_outgoing_mbps:
                self._peak_outgoing_mbps = outgoing

    # ------------------------------------------------------------------ #
    # Private: SR3 report — fold subprocess volume + write /report/report.json
    # ------------------------------------------------------------------ #

    def _fold_run_stats(self) -> None:
        """Fold a start.py subprocess's run-stats file into session totals.

        start.py rewrites RUN_STATS_PATH once per second with its cumulative
        REQUESTS_SENT / BYTES_SEND, so this captures the phase's volume even if
        the subprocess was killed mid-run. The file is consumed (unlinked) so a
        subsequent phase (which starts its counters from zero) is summed, never
        double-counted.
        """
        try:
            path = Path(RUN_STATS_PATH)
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            self._session_requests += int(data.get("requests_sent", 0) or 0)
            self._session_bytes += int(data.get("bytes_sent", 0) or 0)
            tgt = data.get("target")
            if tgt:
                self._last_target = str(tgt)
            path.unlink(missing_ok=True)
        except Exception as e:  # never let reporting affect the run
            log.debug("Failed to fold run stats: %s", e)

    def write_final_report(self) -> bool:
        """Write /report/report.json for ShowRunner to pull at window close.

        Idempotent, best-effort, never raises. Called on every exit path.
        """
        if self._report_written:
            return True
        self._report_written = True
        # Fold any in-flight phase's stats left behind by a mid-run kill.
        self._fold_run_stats()

        reachable = report_writer.REACHABLE_UNKNOWN
        try:
            reachable = report_writer.probe_reachable(self._last_target)
        except Exception as e:  # pragma: no cover - probe is best-effort
            log.debug("Reachability probe failed: %s", e)

        ok = report_writer.write_report(
            requests_sent=self._session_requests,
            bytes_sent=self._session_bytes,
            outgoing_throughput_mbps=self._peak_outgoing_mbps,
            target_reachable=reachable,
            target=self._last_target,
        )
        if ok:
            log.info(
                "SR3 report written (requests=%d, bytes=%d, peak_mbps=%.2f, "
                "target_reachable=%s).",
                self._session_requests,
                self._session_bytes,
                self._peak_outgoing_mbps,
                reachable,
            )
        return ok

    # ------------------------------------------------------------------ #
    # Private: self-terminate
    # ------------------------------------------------------------------ #

    @staticmethod
    def _self_terminate() -> None:
        time.sleep(2)
        log.info("Auto-remove triggered — requesting clean shutdown.")
        shutdown_event.set()


# ══════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════

def main() -> None:
    manager = MhddosManager()

    # ── 1. Load config from SDK ──
    cfg = config.load()

    # ── 2. Start metrics/health server on :9090 ──
    metrics.start_server()

    # ── 3. Handle SIGTERM/SIGINT for graceful shutdown ──
    def handle_shutdown(signum, frame):
        log.info("Received signal %d — shutting down.", signum)
        manager.stop()
        shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    try:
        _run_lifecycle(manager, cfg)
    finally:
        # ── SR3: write /report/report.json on ANY exit path (normal
        # completion, SIGTERM/SIGINT stop, or error). ShowRunner pulls it. ──
        manager.write_final_report()

    log.info("Shutdown complete.")


def _run_lifecycle(manager: "MhddosManager", cfg: Dict[str, Any] | None) -> None:
    # ── 4. Start attacks from initial config ──
    if not cfg and STARTUP_CONFIG_WAIT_SECONDS > 0:
        log.info(
            "No config present at startup -- waiting up to %ss for /config/app.json",
            STARTUP_CONFIG_WAIT_SECONDS,
        )
        deadline = time.time() + STARTUP_CONFIG_WAIT_SECONDS
        while time.time() < deadline and not shutdown_event.is_set():
            shutdown_event.wait(timeout=0.5)
            if not Path(CONFIG_PATH).exists():
                continue
            cfg = config.load()
            if cfg:
                break

    # ── 5. Register live reload handling after initial startup config is settled ──
    config.on_reload(manager.update_config)

    if cfg:
        health.set_status("starting")
        manager.start(cfg)
    else:
        log.warning("No config found at startup — waiting for SIGHUP with config.")
        health.set_status("idle", reason="No config at startup")

    # ── 6. Keep main thread alive until shutdown is requested ──
    while not shutdown_event.is_set():
        manager._update_throughput_metrics()
        shutdown_event.wait(timeout=1)


if __name__ == "__main__":
    main()

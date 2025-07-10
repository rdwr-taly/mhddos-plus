from __future__ import annotations
import subprocess
import psutil
import threading
import time
import logging
import schedule
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from app_adapter import ApplicationAdapter

log = logging.getLogger(__name__)

class MhDosAdapter(ApplicationAdapter):
    """
    Adapter to control the MHDDoS Plus application, replicating the logic
    from the original Flask-based container_control.py.
    """

    def __init__(self, static_cfg: Dict[str, Any] | None = None) -> None:
        super().__init__(static_cfg)
        # --- Runtime State ---
        self.proc: subprocess.Popen | None = None
        self.app_status = "initializing"
        self.auto_remove = True
        self.cron_schedule: str | None = None
        self.cron_active = False
        self.next_run_time: datetime | None = None
        self.sub_tasks: List[Dict[str, Any]] = []
        self.running_task_name: str | None = None
        self.stop_requested = False
        self.cron_thread: threading.Thread | None = None
        self.grace_period = 5
        self._prev_net_io = None
        self._prev_time = None
        
        # Initial network settings
        self._apply_network_settings(10, 0)


    # -------------------------------------------------------------------- #
    # --- Lifecycle Methods (called by container_control_core) --------- #
    # -------------------------------------------------------------------- #

    def start(self, payload: Dict[str, Any], *, ensure_user) -> Any:
        log.info("Start request received.")
        self.stop_requested = False

        # --- Backward Compatibility: adapt old single-attack payload ---
        if "sub_tasks" not in payload:
            log.info("Payload does not contain 'sub_tasks', adapting to new format.")
            payload = {"sub_tasks": [payload]}

        self.sub_tasks = payload.get("sub_tasks", [])
        cron_schedule = payload.get("cron_schedule")
        start_time = payload.get("start_time")

        # --- Mode Decision: Cron vs. Immediate ---
        if cron_schedule and start_time:
            self.auto_remove = payload.get("auto_remove", False)
            self._setup_cron(cron_schedule, start_time, ensure_user)
            return self.cron_thread
        else:
            self.auto_remove = payload.get("auto_remove", True)
            self.cron_active = False
            schedule.clear()
            
            attack_thread = threading.Thread(
                target=self._run_attacks,
                args=(self.sub_tasks, ensure_user),
                daemon=True
            )
            attack_thread.start()
            return attack_thread

    def stop(self) -> None:
        log.info("Stop request received.")
        self.stop_requested = True
        self.app_status = "stopped"

        # --- Stop Cron ---
        if self.cron_active:
            self.cron_active = False
            schedule.clear()
            log.info("Cron scheduler stopped.")
            if self.cron_thread and self.cron_thread.is_alive():
                # The thread will exit gracefully because cron_active is False
                self.cron_thread.join(timeout=2)
            self.cron_thread = None
            self.next_run_time = None

        # --- Stop Process ---
        if self.proc and self.proc.poll() is None:
            try:
                log.info(f"Terminating process PID {self.proc.pid}")
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.warning("Process did not terminate gracefully, killing.")
                self.proc.kill()
                self.proc.wait(timeout=2)
            except Exception as e:
                log.error(f"Error during process termination: {e}")
        
        self.proc = None
        self.running_task_name = None
        log.info("Stop request processed.")

        # --- Auto-remove logic ---
        if self.auto_remove and not self.cron_active:
            log.info("auto_remove=True, container will self-terminate.")
            threading.Thread(target=self._self_terminate, daemon=True).start()

    def update(self, payload: Dict[str, Any]) -> bool:
        if not self.cron_active:
            log.warning("Update request received but cron is not active.")
            return False
        
        new_sub_tasks = payload.get("sub_tasks")
        if isinstance(new_sub_tasks, list):
            self.sub_tasks = new_sub_tasks
            log.info(f"Cron job sub-tasks updated with {len(new_sub_tasks)} new tasks.")
            return True
        else:
            log.error("Update payload did not contain a valid 'sub_tasks' list.")
            return False

    # -------------------------------------------------------------------- #
    # --- Metrics Methods (called by container_control_core) ----------- #
    # -------------------------------------------------------------------- #

    def get_metrics(self) -> Dict[str, Any]:
        incoming_mbps, outgoing_mbps = self._calculate_throughput()
        next_run_str = "Not scheduled"
        if self.next_run_time:
            next_run_str = self.next_run_time.astimezone(timezone.utc).isoformat()

        return {
            "app_status": self.app_status,
            "incoming_throughput_mbps": incoming_mbps,
            "outgoing_throughput_mbps": outgoing_mbps,
            "running_task": self.running_task_name,
            "next_run_time": next_run_str,
            "cron_active": self.cron_active,
            "cron_schedule": self.cron_schedule,
        }

    def prometheus_metrics(self) -> List[str]:
        incoming, outgoing = self._calculate_throughput()
        lines = [
            f"mhddos_incoming_throughput_mbps {incoming or 0}",
            f"mhddos_outgoing_throughput_mbps {outgoing or 0}",
            f"mhddos_cron_active {1 if self.cron_active else 0}",
        ]
        return lines

    # -------------------------------------------------------------------- #
    # --- Private Helpers (Ported from old container_control.py) ------- #
    # -------------------------------------------------------------------- #

    def _self_terminate(self):
        time.sleep(2)
        log.info("Exiting container now.")
        os._exit(0)

    def _apply_network_settings(self, bandwidth: int, latency: int) -> bool:
        try:
            iface = "eth0"
            # Clear existing rules first
            subprocess.run(["tc", "qdisc", "del", "dev", iface, "root"], check=False, stderr=subprocess.PIPE)
            
            # Apply new rules
            log.info(f"Applying network settings: {bandwidth}Mbps, {latency}ms latency.")
            cmd_tbf = [
                "tc", "qdisc", "add", "dev", iface, "root", "tbf",
                "rate", f"{bandwidth}mbit", "burst", "32k", "latency", f"{latency}ms"
            ]
            subprocess.run(cmd_tbf, check=True)
            return True
        except subprocess.CalledProcessError as e:
            log.error(f"Failed to apply network settings: {e.stderr.decode() if e.stderr else e}")
            return False
        except Exception as e:
            log.error(f"Unknown error applying network settings: {e}")
            return False

    def _format_start_args(self, json_data: dict) -> list:
        return [
            str(json_data.get("Method", "")),
            str(json_data.get("Target URL", "")),
            str(json_data.get("Type", "")),
            str(json_data.get("Threads", "")),
            str(json_data.get("Proxy List File", "")),
            str(json_data.get("RPC", "")),
            str(json_data.get("Duration (seconds)", ""))
        ]

    def _launch_main_process(self, args: List[str], ensure_user) -> bool:
        try:
            cmd = ["python3", "start.py"] + args
            # Use the ensure_user helper to drop privileges
            self.proc = subprocess.Popen(ensure_user(cmd))
            log.info(f"Started main process with PID={self.proc.pid} (args={args}).")
            return True
        except Exception as e:
            log.error(f"Failed to launch main process: {e}")
            self.proc = None
            return False

    def _run_attacks(self, sub_tasks: List[Dict], ensure_user):
        self.app_status = "running"
        for config in sub_tasks:
            if self.stop_requested:
                log.info("Stop requested, aborting attack sequence.")
                break

            bw = int(config.get("throughput_in_mbps", 10))
            lat = int(config.get("latency_in_ms", 0))
            self._apply_network_settings(bw, lat)

            self.running_task_name = config.get("name", "Unnamed Task")
            log.info(f"Starting task: {self.running_task_name}")

            args = self._format_start_args(config)
            if not self._launch_main_process(args, ensure_user):
                log.error("Failed to launch attack, stopping sequence.")
                break
            
            # Wait for process to finish or stop to be requested
            while self.proc and self.proc.poll() is None:
                if self.stop_requested:
                    self.stop() # Trigger termination
                    break
                time.sleep(0.5)
            
            if self.proc:
                log.info(f"Task '{self.running_task_name}' finished with code {self.proc.poll()}.")
                self.proc = None

        # --- Cleanup after all attacks ---
        self.running_task_name = None
        if not self.stop_requested:
            if self.cron_active:
                self.app_status = "active"
                log.info("Attack sequence finished, cron remains active.")
            else:
                self.app_status = "stopped"
                log.info("Immediate attack sequence finished.")
                if self.auto_remove:
                    self._self_terminate()
        else:
            self.app_status = "stopped"

    def _setup_cron(self, schedule_str: str, start_time_str: str, ensure_user):
        schedule.clear()
        try:
            start_time_utc = datetime.fromisoformat(
                start_time_str.replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            
            now_utc = datetime.now(timezone.utc)
            
            # Simplified scheduling logic from old implementation
            unit = schedule_str[-1]
            val = int(schedule_str[:-1])
            job = schedule.every(val)

            if unit == 's': job.seconds
            elif unit == 'm': job.minutes
            elif unit == 'h': job.hours
            elif unit == 'd': job.days
            else: raise ValueError(f"Invalid schedule unit: {unit}")

            # Align to the start time's components
            at_str = None
            if unit == 'd':
                at_str = f"{start_time_utc.hour:02d}:{start_time_utc.minute:02d}"
            elif unit == 'h':
                at_str = f":{start_time_utc.minute:02d}"
            
            if at_str:
                job.at(at_str)

            job.do(self._run_attacks, sub_tasks=self.sub_tasks, ensure_user=ensure_user)
            
            # Calculate first run
            while job.next_run and job.next_run < now_utc:
                 job.run() # This advances next_run
                 schedule.clear() # a bit of a hack to reset and recalculate
                 job.do(self._run_attacks, sub_tasks=self.sub_tasks, ensure_user=ensure_user)


            self.next_run_time = schedule.next_run
            self.cron_active = True
            self.cron_schedule = schedule_str
            self.app_status = "active"
            log.info(f"Cron job scheduled: {schedule_str}. Next run: {self.next_run_time}")

            if not self.cron_thread or not self.cron_thread.is_alive():
                self.cron_thread = threading.Thread(target=self._run_cron, daemon=True)
                self.cron_thread.start()

        except Exception as e:
            log.error(f"Error scheduling cron job: {e}")
            self.app_status = "error"

    def _run_cron(self):
        log.info("Cron thread started.")
        while self.cron_active:
            schedule.run_pending()
            self.next_run_time = schedule.next_run
            time.sleep(1)
        log.info("Cron thread finished.")

    def _calculate_throughput(self) -> tuple[float | None, float | None]:
        try:
            now = time.time()
            current_io = psutil.net_io_counters()
            if self._prev_time is None or self._prev_net_io is None:
                self._prev_time = now
                self._prev_net_io = current_io
                return None, None

            delta = now - self._prev_time
            if delta == 0: return 0.0, 0.0

            sent_mbps = ((current_io.bytes_sent - self._prev_net_io.bytes_sent) * 8) / (delta * 1_000_000)
            recv_mbps = ((current_io.bytes_recv - self._prev_net_io.bytes_recv) * 8) / (delta * 1_000_000)

            self._prev_time = now
            self._prev_net_io = current_io
            return round(recv_mbps, 2), round(sent_mbps, 2)
        except Exception:
            return None, None

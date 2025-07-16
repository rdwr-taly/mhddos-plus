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
    Adapter to control the MHDDoS Plus application.
    
    v2.0 - Simplified to leverage core services for:
    - Traffic control (bandwidth/latency) via core traffic_control service  
    - Network metrics via core metrics service
    - Process lifecycle remains in adapter for complex multi-task scenarios
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

        # Ensure sub_tasks is always a list
        sub_tasks = payload.get("sub_tasks")
        if not isinstance(sub_tasks, list):
            if sub_tasks is not None:
                log.warning("'sub_tasks' payload is not a list, ignoring value")
            sub_tasks = []
        self.sub_tasks = sub_tasks
        cron_schedule = payload.get("cron_schedule")
        start_time = payload.get("start_time")

        # --- Mode Decision: Cron vs. Immediate ---
        if cron_schedule and start_time:
            self.auto_remove = payload.get("auto_remove", False)
            if not self._setup_cron(cron_schedule, start_time, ensure_user):
                raise RuntimeError("Failed to schedule cron job")
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
        # Core v2.0 provides network metrics automatically via /api/metrics
        # We focus on application-specific metrics here
        next_run_str = "Not scheduled"
        if self.next_run_time:
            next_run_str = self.next_run_time.astimezone(timezone.utc).isoformat()

        metrics = {
            "app_status": self.app_status,
            "running_task": self.running_task_name,
            "next_run_time": next_run_str,
            "cron_active": self.cron_active,
            "cron_schedule": self.cron_schedule,
            "sub_tasks_count": len(self.sub_tasks or []),
        }

        # Optionally include custom throughput calculation for compatibility
        # This will be in addition to core's network metrics
        incoming_mbps, outgoing_mbps = self._calculate_throughput()
        if incoming_mbps is not None and outgoing_mbps is not None:
            metrics.update({
                "incoming_throughput_mbps": incoming_mbps,
                "outgoing_throughput_mbps": outgoing_mbps,
            })

        return metrics

    def prometheus_metrics(self) -> List[str]:
        incoming, outgoing = self._calculate_throughput()
        lines = [
            f"# HELP mhddos_incoming_throughput_mbps incoming throughput calculation",
            f"mhddos_incoming_throughput_mbps {incoming or 0}",
            f"# HELP mhddos_outgoing_throughput_mbps outgoing throughput calculation", 
            f"mhddos_outgoing_throughput_mbps {outgoing or 0}",
            f"# HELP mhddos_cron_active Whether cron scheduling is active",
            f"mhddos_cron_active {1 if self.cron_active else 0}",
            f"# HELP mhddos_sub_tasks_count Number of configured sub-tasks",
            f"mhddos_sub_tasks_count {len(self.sub_tasks or [])}",
        ]
        return lines

    # -------------------------------------------------------------------- #
    # --- Core Service Hooks (NEW in v2.0) ----------------------------- #
    # -------------------------------------------------------------------- #

    def on_before_core_traffic_control(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Hook called before core applies traffic control.
        Allows us to extract bandwidth/latency from sub_tasks if needed.
        """
        # If payload doesn't have direct TC keys, try to get from first sub-task
        if "throughput_in_mbps" not in payload and self.sub_tasks:
            first_task = self.sub_tasks[0]
            if "throughput_in_mbps" in first_task:
                payload["throughput_in_mbps"] = first_task["throughput_in_mbps"]
            if "latency_in_ms" in first_task:
                payload["latency_in_ms"] = first_task["latency_in_ms"]
        
        return payload

    # -------------------------------------------------------------------- #
    # --- Private Helpers (Ported from old container_control.py) ------- #
    # -------------------------------------------------------------------- #

    def _self_terminate(self):
        time.sleep(2)
        log.info("Exiting container now.")
        os._exit(0)

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

            # Note: Traffic control is now handled by the core service
            # based on throughput_in_mbps and latency_in_ms keys in the config
            
            self.running_task_name = config.get("name", "Unnamed Task")
            log.info(f"Starting task: {self.running_task_name}")

            # Apply per-task traffic control before launching process
            self._apply_per_task_traffic_control(config)

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

    def _setup_cron(self, schedule_str: str, start_time_str: str, ensure_user) -> bool:
        schedule.clear()
        try:
            start_time = datetime.fromisoformat(
                start_time_str.replace("Z", "+00:00")
            )
            if start_time.tzinfo:
                start_time = start_time.astimezone(timezone.utc).replace(
                    tzinfo=None
                )
            
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
                at_str = f"{start_time.hour:02d}:{start_time.minute:02d}"
            elif unit == 'h':
                at_str = f":{start_time.minute:02d}"
            
            if at_str:
                job.at(at_str)

            job.do(self._run_attacks, sub_tasks=self.sub_tasks, ensure_user=ensure_user)
            
            # Ensure the first execution is not before the provided start time
            while job.next_run:
                jrun = job.next_run
                if jrun.tzinfo:
                    jrun = jrun.astimezone(timezone.utc).replace(tzinfo=None)
                s_time = start_time
                if s_time.tzinfo:
                    s_time = s_time.astimezone(timezone.utc).replace(tzinfo=None)
                if jrun >= s_time:
                    break
                delta = timedelta(**{job.unit: job.interval})
                job.next_run += delta


            self.next_run_time = None
            if schedule.next_run:
                nr = schedule.next_run
                if nr.tzinfo:
                    nr = nr.astimezone(timezone.utc)
                else:
                    nr = nr.replace(tzinfo=timezone.utc)
                self.next_run_time = nr
            self.cron_active = True
            self.cron_schedule = schedule_str
            self.app_status = "active"
            log.info(f"Cron job scheduled: {schedule_str}. Next run: {self.next_run_time}")

            if not self.cron_thread or not self.cron_thread.is_alive():
                self.cron_thread = threading.Thread(target=self._run_cron, daemon=True)
                self.cron_thread.start()
            return True

        except Exception as e:
            log.error(f"Error scheduling cron job: {e}")
            self.app_status = "error"
            return False

    def _run_cron(self):
        log.info("Cron thread started.")
        while self.cron_active:
            schedule.run_pending()
            self.next_run_time = None
            if schedule.next_run:
                nr = schedule.next_run
                if nr.tzinfo:
                    nr = nr.astimezone(timezone.utc)
                else:
                    nr = nr.replace(tzinfo=timezone.utc)
                self.next_run_time = nr
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

    def _apply_per_task_traffic_control(self, task_config: Dict[str, Any]) -> None:
        """
        Apply traffic control for a specific task.
        This manually applies TC since the core service only applies it once at start.
        For true per-task TC, we'd need to extend the core or handle it here.
        """
        bandwidth = task_config.get("throughput_in_mbps", 10)
        latency = task_config.get("latency_in_ms", 0)
        
        if not bandwidth:
            return
            
        iface = "eth0"
        try:
            # Clear existing rules first
            subprocess.run(["tc", "qdisc", "del", "dev", iface, "root"], 
                          check=False, stderr=subprocess.PIPE)
            
            # Apply new rules  
            burst = max(32, int(bandwidth * 1000 / 8))  # Sensible burst calculation
            latency_val = max(1, int(latency))
            cmd = [
                "tc", "qdisc", "add", "dev", iface, "root", "tbf",
                "rate", f"{bandwidth}mbit", "burst", f"{burst}k", "latency", f"{latency_val}ms"
            ]
            subprocess.run(cmd, check=True)
            log.info(
                f"Applied per-task traffic control: {bandwidth}Mbps, {latency_val}ms latency."
            )
        except subprocess.CalledProcessError as e:
            log.warning(f"Failed to apply per-task traffic control: {e}")
        except Exception as e:
            log.warning(f"Error applying per-task traffic control: {e}")

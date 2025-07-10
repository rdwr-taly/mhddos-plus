"""
Adapter interface for Showrunner "Ultimate Container Control" v2.0.

Copy this file unchanged into each application image.  
Your real adapter must **sub-class `ApplicationAdapter`** and implement the
minimal required methods (`start`, `stop`, `get_metrics`).  
All other hooks are optional.

NEW in v2.0: The core now provides optional services that can handle common
operations (process management, metrics, traffic control, privileged commands).
This means your adapters can be much simpler!
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, Dict, List


class ApplicationAdapter(ABC):
    """
    Abstract base class that hides app-specific complexity from the core.

    ● Lifecycle:
        - `start(payload, *, ensure_user)`  : launch the workload.  
          Return any opaque handle (thread, Popen, etc.).
        - `stop()`                          : idempotent shutdown.
        - `update(payload)`                 : live config tweak (return True if
                                              applied, False / raise if not).
    ● Observability:
        - `get_metrics()`                   : dict merged into /api/metrics.
        - `prometheus_metrics()`            : list[str] lines for /metrics (opt).
    ● Privileged hooks (run as root):
        - `pre_start_hooks(payload)`        : e.g. tc/iptables setup.
        - `post_stop_hooks()`               : cleanup.
    
    ● NEW v2.0 optional hooks for core services:
        - `on_before_core_traffic_control(payload)` : modify payload before TC.
        - `on_core_process_exit(return_code, stdout, stderr)` : process exit hook.
        - Command factory methods for process management (see config.yaml).
    """

    # ---------- lifecycle -------------------------------------------------- #
    def __init__(self, static_cfg: Dict[str, Any] | None = None) -> None:
        self.static_cfg = static_cfg or {}

    @abstractmethod
    def start(self, start_payload: Dict[str, Any], *, ensure_user) -> Any: ...

    @abstractmethod
    def stop(self) -> None: ...

    def update(self, update_payload: Dict[str, Any]) -> bool:
        """
        Apply in-place configuration changes at runtime.
        Return True if handled, False (or raise NotImplementedError) if not.
        """
        raise NotImplementedError("live update not supported")

    # ---------- metrics ---------------------------------------------------- #
    def get_metrics(self) -> Dict[str, Any]:
        return {}

    def prometheus_metrics(self) -> List[str]:
        return []

    # ---------- privileged hooks ------------------------------------------- #
    def pre_start_hooks(self, start_payload: Dict[str, Any]) -> None: ...

    def post_stop_hooks(self) -> None: ...

    # ---------- NEW v2.0 optional hooks for core services ----------------- #
    def on_before_core_traffic_control(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Optional hook called before the core applies traffic control.
        Can modify the payload to adjust TC parameters.
        Return the modified payload.
        """
        return payload

    def on_core_process_exit(self, return_code: int, stdout: str, stderr: str) -> None:
        """
        Optional hook called when a core-managed process exits.
        Only called if process_management is enabled.
        """
        pass

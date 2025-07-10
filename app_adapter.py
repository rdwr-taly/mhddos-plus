"""
Adapter interface for Showrunner “Ultimate Container Control”.

Copy this file unchanged into each application image.  
Your real adapter must **sub-class `ApplicationAdapter`** and implement the
minimal required methods (`start`, `stop`, `get_metrics`).  
All other hooks are optional.
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

    # ---------- privileged hooks ------------------------------------------ #
    def pre_start_hooks(self, start_payload: Dict[str, Any]) -> None: ...

    def post_stop_hooks(self) -> None: ...

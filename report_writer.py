"""SR3 report writer — emit ``/report/report.json`` for ShowRunner to pull.

ShowRunner v3.0 pulls this file out of the container at window close (via the
Docker API) and projects its ``measures`` into the demo report + runbook. The
app declares this contract in its ``.showrunner/appspec.json`` ``sdk`` block, so
ShowRunner knows the path and what measures to expect.

Honesty note (mhddos-plus specifically): this is a volumetric L4/L7 flood tool.
Most methods are raw-socket floods with **no HTTP response parsing**, so there is
no truthful per-status-code map or block/mitigation ratio to report. We surface
VOLUME only — how much traffic the tool actually pushed — plus a coarse target
reachability probe. We deliberately do NOT fabricate a block_ratio or a
per-code breakdown this app cannot measure.

Fully optional and non-fatal: if the path is not writable the run is unaffected
(ShowRunner simply degrades to Tier-0, i.e. Prometheus metrics + logs). The file
is written atomically (tmp + rename) with ``status: "final"`` so ShowRunner never
observes a half-written report.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

LOGGER = logging.getLogger(__name__)

DEFAULT_REPORT_PATH = "/report/report.json"

# target.reachable enum — the only values this app can honestly emit.
REACHABLE_ONLINE = "online"
REACHABLE_OFFLINE = "offline"
REACHABLE_UNKNOWN = "unknown"


def probe_reachable(target: str | None, timeout: float = 3.0) -> str:
    """Best-effort coarse reachability probe of ``target`` (URL or host[:port]).

    Attempts a single TCP connect. This is an *external observation* taken at
    report time, not proof the flood was effective — a target can be reachable
    yet unaffected, or unreachable for reasons unrelated to the attack. Returns
    one of ``online`` / ``offline`` / ``unknown`` and never raises.
    """
    if not target:
        return REACHABLE_UNKNOWN
    host: str | None = None
    port: int | None = None
    try:
        raw = target.strip()
        if "://" not in raw:
            raw = "//" + raw  # let urlparse treat it as netloc, not scheme
        parsed = urlparse(raw)
        host = parsed.hostname
        port = parsed.port
        if port is None:
            scheme = urlparse(target).scheme.lower()
            port = 443 if scheme == "https" else 80
    except Exception:  # pragma: no cover - defensive parse guard
        LOGGER.debug("Failed to parse target %r for reachability probe", target, exc_info=True)
        return REACHABLE_UNKNOWN
    if not host:
        return REACHABLE_UNKNOWN
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return REACHABLE_ONLINE
    except (socket.timeout, ConnectionRefusedError, OSError):
        # Refused/timeout/unreachable — coarse "offline" signal.
        return REACHABLE_OFFLINE
    except Exception:  # pragma: no cover - never let the probe break the report
        return REACHABLE_UNKNOWN


def _mbps(throughput_mbps: Any) -> float:
    try:
        return round(float(throughput_mbps or 0.0), 2)
    except (TypeError, ValueError):
        return 0.0


def _count(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def build_report(
    requests_sent: Any = 0,
    bytes_sent: Any = 0,
    outgoing_throughput_mbps: Any = 0.0,
    target_reachable: str = REACHABLE_UNKNOWN,
    target: str | None = None,
) -> dict[str, Any]:
    """Build the SR3 report document from volume metrics gathered during the run."""
    reqs = _count(requests_sent)
    sent = _count(bytes_sent)
    peak = _mbps(outgoing_throughput_mbps)
    reachable = target_reachable if target_reachable in (
        REACHABLE_ONLINE,
        REACHABLE_OFFLINE,
        REACHABLE_UNKNOWN,
    ) else REACHABLE_UNKNOWN

    if reqs or sent:
        summary = (
            f"Generated volumetric load: {reqs} request(s), "
            f"{sent} byte(s) sent, peak {peak} Mbps outgoing. "
            f"Target reachability at window close: {reachable}."
        )
    else:
        summary = (
            "No measurable volume was generated during this window. "
            f"Target reachability at window close: {reachable}."
        )

    findings = [
        {
            "severity": "info",
            "title": "Volume-only signal (no mitigation verdict)",
            "category": "measurement",
            "detail": (
                "mhddos-plus is a volumetric L4/L7 flood tool; most methods are "
                "raw-socket floods with no HTTP response parsing. This report "
                "surfaces traffic volume and a coarse reachability probe only. It "
                "does NOT contain a per-status-code map or a block/mitigation "
                "ratio, because this app cannot truthfully measure them."
            ),
        }
    ]

    return {
        "schema_version": 1,
        "status": "final",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "measures": {
            "volume.requests_sent": reqs,
            "volume.bytes_sent": sent,
            "volume.outgoing_throughput_mbps": peak,
            "target.reachable": reachable,
        },
        "summary": summary,
        "findings": findings,
    }


def write_report(
    requests_sent: Any = 0,
    bytes_sent: Any = 0,
    outgoing_throughput_mbps: Any = 0.0,
    target_reachable: str = REACHABLE_UNKNOWN,
    target: str | None = None,
    path: str | None = None,
) -> bool:
    """Atomically write the SR3 report. Returns True on success, never raises."""
    target_path = Path(path or os.getenv("SR_REPORT_PATH", DEFAULT_REPORT_PATH))
    try:
        report = build_report(
            requests_sent=requests_sent,
            bytes_sent=bytes_sent,
            outgoing_throughput_mbps=outgoing_throughput_mbps,
            target_reachable=target_reachable,
            target=target,
        )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = target_path.with_name(target_path.name + ".tmp")
        tmp.write_text(json.dumps(report, indent=2), encoding="utf-8")
        tmp.replace(target_path)  # atomic rename on the same filesystem
        LOGGER.info("SR3 report written to %s", target_path)
        return True
    except Exception:  # pragma: no cover - degrade to Tier-0, never affect the run
        LOGGER.debug(
            "SR3 report write failed; ShowRunner will degrade to Tier-0", exc_info=True
        )
        return False


__all__ = [
    "build_report",
    "write_report",
    "probe_reachable",
    "DEFAULT_REPORT_PATH",
    "REACHABLE_ONLINE",
    "REACHABLE_OFFLINE",
    "REACHABLE_UNKNOWN",
]

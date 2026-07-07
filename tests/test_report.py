from __future__ import annotations

import json

from report_writer import (
    REACHABLE_OFFLINE,
    REACHABLE_ONLINE,
    REACHABLE_UNKNOWN,
    build_report,
    probe_reachable,
    write_report,
)


def test_build_report_volume_measures() -> None:
    report = build_report(
        requests_sent=12000,
        bytes_sent=45_000_000,
        outgoing_throughput_mbps=87.5,
        target_reachable=REACHABLE_ONLINE,
        target="http://victim.example/",
    )
    assert report["schema_version"] == 1
    assert report["status"] == "final"
    m = report["measures"]
    assert m["volume.requests_sent"] == 12000
    assert m["volume.bytes_sent"] == 45_000_000
    assert m["volume.outgoing_throughput_mbps"] == 87.5
    assert m["target.reachable"] == "online"
    # Honest: no fabricated per-code map / block ratio present.
    assert "responses.by_code" not in m
    assert "block_ratio" not in json.dumps(m)
    assert "12000 request" in report["summary"]
    # The volume-only honesty note is surfaced as an informational finding.
    assert report["findings"][0]["severity"] == "info"


def test_build_report_no_traffic_defaults() -> None:
    report = build_report()
    m = report["measures"]
    assert m["volume.requests_sent"] == 0
    assert m["volume.bytes_sent"] == 0
    assert m["volume.outgoing_throughput_mbps"] == 0.0
    assert m["target.reachable"] == REACHABLE_UNKNOWN
    assert report["status"] == "final"
    assert "No measurable volume" in report["summary"]


def test_build_report_coerces_and_guards_bad_inputs() -> None:
    report = build_report(
        requests_sent="not-a-number",
        bytes_sent=None,
        outgoing_throughput_mbps="oops",
        target_reachable="bogus-enum",
    )
    m = report["measures"]
    assert m["volume.requests_sent"] == 0
    assert m["volume.bytes_sent"] == 0
    assert m["volume.outgoing_throughput_mbps"] == 0.0
    # Unknown enum values fall back to "unknown", never leak through.
    assert m["target.reachable"] == REACHABLE_UNKNOWN


def test_write_report_atomic_and_sealed(tmp_path) -> None:
    target = tmp_path / "report" / "report.json"
    ok = write_report(
        requests_sent=5,
        bytes_sent=1024,
        outgoing_throughput_mbps=1.25,
        target_reachable=REACHABLE_ONLINE,
        path=str(target),
    )
    assert ok is True
    assert target.exists()
    # No leftover tmp file from the atomic rename.
    assert not (tmp_path / "report" / "report.json.tmp").exists()
    data = json.loads(target.read_text())
    assert data["status"] == "final"  # sealed — the portal only projects final
    assert data["measures"]["volume.requests_sent"] == 5
    assert data["measures"]["volume.bytes_sent"] == 1024


def test_write_report_unwritable_path_degrades() -> None:
    # A path under a file (not a dir) can't be created -> returns False, no raise.
    assert write_report(requests_sent=1, path="/dev/null/nope/report.json") is False


def test_probe_reachable_online_localhost() -> None:
    import socket
    import threading

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def _accept() -> None:
        srv.settimeout(2.0)
        try:
            conn, _ = srv.accept()
            conn.close()
        except OSError:
            pass

    t = threading.Thread(target=_accept, daemon=True)
    t.start()
    try:
        assert probe_reachable(f"127.0.0.1:{port}", timeout=2.0) == REACHABLE_ONLINE
    finally:
        stop.set()
        srv.close()
        t.join(timeout=2.0)


def test_probe_reachable_offline_closed_port() -> None:
    # Port 1 is reserved and virtually never listening -> offline (refused).
    assert probe_reachable("127.0.0.1:1", timeout=2.0) == REACHABLE_OFFLINE


def test_probe_reachable_none_is_unknown() -> None:
    assert probe_reachable(None) == REACHABLE_UNKNOWN
    assert probe_reachable("") == REACHABLE_UNKNOWN

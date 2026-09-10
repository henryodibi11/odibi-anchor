"""AXI #6: non-fatal silent failures are recorded and surfaced in anchor('status')."""
import odibi_anchor._utils._session_state as ss
import odibi_anchor._dispatcher._compliance as comp


def test_record_and_get_degraded():
    ss._SESSION_DEGRADED.clear()
    ss.record_degraded("memory_health", ValueError("boom"))
    d = ss.get_degraded()
    assert len(d) == 1
    assert d[0]["component"] == "memory_health"
    assert "boom" in d[0]["reason"]
    ss._SESSION_DEGRADED.clear()


def test_reason_is_truncated():
    ss._SESSION_DEGRADED.clear()
    ss.record_degraded("x", "y" * 1000)
    assert len(ss.get_degraded()[0]["reason"]) <= 300
    ss._SESSION_DEGRADED.clear()


def test_status_surfaces_degraded(tmp_path, monkeypatch):
    monkeypatch.setattr(comp, "_SESSION_TIMINGS", [{"action": "status", "elapsed_ms": 1, "error": None}])
    monkeypatch.setattr(comp, "_SESSION_FILES_CHANGED", set())
    monkeypatch.setattr(comp, "_SESSION_FILES_CREATED", set())
    ss._SESSION_DEGRADED.clear()
    ss.record_degraded("memory_health", "db locked")
    ctx = comp._status(str(tmp_path), {}, None, {}, output_format="dict")
    assert ctx["metrics"]["degraded_count"] >= 1
    assert any("non-fatal failure" in a for a in ctx["suggested_next_actions"])
    ss._SESSION_DEGRADED.clear()


def test_status_clean_when_no_degraded(tmp_path, monkeypatch):
    monkeypatch.setattr(comp, "_SESSION_TIMINGS", [{"action": "status", "elapsed_ms": 1, "error": None}])
    monkeypatch.setattr(comp, "_SESSION_FILES_CHANGED", set())
    monkeypatch.setattr(comp, "_SESSION_FILES_CREATED", set())
    ss._SESSION_DEGRADED.clear()
    ctx = comp._status(str(tmp_path), {}, None, {}, output_format="dict")
    assert ctx["metrics"]["degraded_count"] == 0
    assert not any("non-fatal failure" in a for a in ctx["suggested_next_actions"])


def test_status_has_frame_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr(comp, "_SESSION_TIMINGS", [{"action": "status", "elapsed_ms": 1, "error": None}])
    monkeypatch.setattr(comp, "_SESSION_FILES_CHANGED", set())
    monkeypatch.setattr(comp, "_SESSION_FILES_CREATED", set())
    ss._SESSION_DEGRADED.clear()
    ctx = comp._status(str(tmp_path), {}, None, {}, output_format="dict")
    # frame summary now exposed in the dict (not just markdown); None frame -> 0
    assert ctx["metrics"]["frame_findings"] == 0
    assert ctx["metrics"]["frame_risks"] == 0

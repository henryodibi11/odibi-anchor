"""_context_frame.py — Per-session shared accumulator for odibi_anchor.

Every anchor() call transparently records its findings, risks, and action log into
the ContextFrame. This turns independent tool outputs into a shared nervous
system — tools can query what other tools already discovered, and suggestions
become contextual instead of generic.

Architecture:
    Phase 1 (this module): Core dataclasses + FrameRecorder
    Phase 2 (agent_init.py): Dispatcher integration, anchor("frame"), status enhancement
    Phase 3 (tools): Individual tools read from frame for contextual suggestions

Usage (internal — managed by anchor() dispatcher):
    from odibi_anchor._utils._context_frame import ContextFrame

    # Create at session boot
    frame = ContextFrame.new(session_id="...", project="my-project", manifest={})

    # Record after every anchor() call (transparent to tools)
    frame.record("profile_table", tool_result_dict)

    # Inspect
    frame.findings         # list[Finding]
    frame.risks            # list[Risk]
    frame.actions          # list[Action] (ordered log)
    frame.data_context     # DataContext (tables profiled, columns analyzed)
    frame.code_context     # CodeContext (files changed, convention violations)
    frame.memory_context   # MemoryContext (loaded memories, known-bad matches)

Build order: Environment Manifest → Tool Registry → Context Frame (this file)
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "Finding",
    "Risk",
    "Decision",
    "Action",
    "DataContext",
    "CodeContext",
    "MemoryContext",
    "ComplianceContext",
    "ContextFrame",
    "FrameRecorder",
]


# ─── Primitive Event Types ────────────────────────────────────────────────────


@dataclass
class Finding:
    """A discrete observation produced by a tool."""

    source: str        # Tool that produced it (e.g. "profile_table", "microscope")
    category: str      # "data_quality" | "convention" | "performance" | "compliance" | "unknown"
    severity: str      # "info" | "warning" | "critical"
    message: str       # Human-readable description
    subject: str       # What it's about (file path, column name, table name)
    evidence: dict     # Supporting data (metrics, samples) — lightweight summary only
    timestamp: datetime

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "category": self.category,
            "severity": self.severity,
            "message": self.message,
            "subject": self.subject,
            "evidence": self.evidence,
            "timestamp": self.timestamp.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Finding":
        ts = d.get("timestamp")
        return cls(
            source=d.get("source", ""),
            category=d.get("category", "unknown"),
            severity=d.get("severity", "info"),
            message=d.get("message", ""),
            subject=d.get("subject", ""),
            evidence=d.get("evidence", {}),
            timestamp=datetime.fromisoformat(ts) if isinstance(ts, str) else datetime.now(timezone.utc),
        )


@dataclass
class Risk:
    """An identified risk surfaced by a tool."""

    source: str
    severity: str      # "low" | "medium" | "high"
    message: str
    mitigation: str    # Suggested fix / what to do about it
    timestamp: datetime

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "severity": self.severity,
            "message": self.message,
            "mitigation": self.mitigation,
            "timestamp": self.timestamp.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Risk":
        ts = d.get("timestamp")
        return cls(
            source=d.get("source", ""),
            severity=d.get("severity", "medium"),
            message=d.get("message", ""),
            mitigation=d.get("mitigation", ""),
            timestamp=datetime.fromisoformat(ts) if isinstance(ts, str) else datetime.now(timezone.utc),
        )


@dataclass
class Decision:
    """A decision made during this session (by agent, human, or gate)."""

    description: str
    rationale: str
    source: str        # "agent" | "human" | "gate"
    timestamp: datetime

    def to_dict(self) -> dict:
        return {
            "description": self.description,
            "rationale": self.rationale,
            "source": self.source,
            "timestamp": self.timestamp.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Decision":
        ts = d.get("timestamp")
        return cls(
            description=d.get("description", ""),
            rationale=d.get("rationale", ""),
            source=d.get("source", "agent"),
            timestamp=datetime.fromisoformat(ts) if isinstance(ts, str) else datetime.now(timezone.utc),
        )


@dataclass
class Action:
    """One anchor() call recorded in the session timeline."""

    tool: str                    # anchor() action name
    args_summary: str            # Brief description of args (truncated)
    elapsed_ms: float
    success: bool
    findings_produced: int       # How many findings were extracted from this call
    timestamp: datetime

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "args_summary": self.args_summary,
            "elapsed_ms": self.elapsed_ms,
            "success": self.success,
            "findings_produced": self.findings_produced,
            "timestamp": self.timestamp.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Action":
        ts = d.get("timestamp")
        return cls(
            tool=d.get("tool", ""),
            args_summary=d.get("args_summary", ""),
            elapsed_ms=d.get("elapsed_ms", 0.0),
            success=d.get("success", True),
            findings_produced=d.get("findings_produced", 0),
            timestamp=datetime.fromisoformat(ts) if isinstance(ts, str) else datetime.now(timezone.utc),
        )


# ─── Sub-Context Objects ──────────────────────────────────────────────────────


@dataclass
class DataContext:
    """Accumulates data-layer context: tables profiled, columns analyzed, quality."""

    tables_profiled: dict[str, dict] = field(default_factory=dict)
    # table_name -> profile summary (grain, nulls, freshness, row count)

    columns_analyzed: dict[str, dict] = field(default_factory=dict)
    # "table.column" -> microscope findings summary

    grain_detected: dict[str, list] = field(default_factory=dict)
    # table -> candidate grain column names

    freshness: dict[str, dict] = field(default_factory=dict)
    # table -> {max_date, lag_days, source}

    sensitive_columns: list[str] = field(default_factory=list)
    # Columns flagged as PII or sensitive (from profiler / conventions)

    join_relationships: list[dict] = field(default_factory=list)
    # Discovered join relationships: [{left, right, key, confidence}]

    data_quality_scores: dict[str, float] = field(default_factory=dict)
    # table -> overall quality score 0.0-1.0

    def has_profile(self, table: str) -> bool:
        """Return True if the table has been profiled this session."""
        return table in self.tables_profiled

    def get_profile(self, table: str) -> dict | None:
        """Return the profile summary for a table, or None."""
        return self.tables_profiled.get(table)

    def get_column_findings(self, table_column: str) -> dict | None:
        """Return microscope findings for 'table.column', or None."""
        return self.columns_analyzed.get(table_column)

    def to_dict(self) -> dict:
        return {
            "tables_profiled": self.tables_profiled,
            "columns_analyzed": self.columns_analyzed,
            "grain_detected": self.grain_detected,
            "freshness": self.freshness,
            "sensitive_columns": self.sensitive_columns,
            "join_relationships": self.join_relationships,
            "data_quality_scores": self.data_quality_scores,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DataContext":
        return cls(
            tables_profiled=d.get("tables_profiled", {}),
            columns_analyzed=d.get("columns_analyzed", {}),
            grain_detected=d.get("grain_detected", {}),
            freshness=d.get("freshness", {}),
            sensitive_columns=d.get("sensitive_columns", []),
            join_relationships=d.get("join_relationships", []),
            data_quality_scores=d.get("data_quality_scores", {}),
        )


@dataclass
class CodeContext:
    """Accumulates code-layer context: files changed, violations, import graph."""

    files_changed: set[str] = field(default_factory=set)
    files_created: set[str] = field(default_factory=set)
    codebase_map: dict | None = None              # From anchor("map")
    convention_violations: list[dict] = field(default_factory=list)  # From anchor("preflight")
    import_graph: dict | None = None              # From anchor("import_resolve")
    test_results: dict | None = None              # From anchor("test")
    preflight_baseline: set[str] | None = None    # Captured on first preflight run

    def files_with_violations(self) -> list[str]:
        """Return file paths that have convention violations."""
        return list({v.get("file", "") for v in self.convention_violations if v.get("file")})

    def to_dict(self) -> dict:
        return {
            "files_changed": sorted(self.files_changed),
            "files_created": sorted(self.files_created),
            "codebase_map": self.codebase_map,
            "convention_violations": self.convention_violations,
            "import_graph": self.import_graph,
            "test_results": self.test_results,
            "preflight_baseline": sorted(self.preflight_baseline) if self.preflight_baseline is not None else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CodeContext":
        _baseline_raw = d.get("preflight_baseline")
        return cls(
            files_changed=set(d.get("files_changed", [])),
            files_created=set(d.get("files_created", [])),
            codebase_map=d.get("codebase_map"),
            convention_violations=d.get("convention_violations", []),
            import_graph=d.get("import_graph"),
            test_results=d.get("test_results"),
            preflight_baseline=set(_baseline_raw) if _baseline_raw is not None else None,
        )


@dataclass
class MemoryContext:
    """Accumulates memory-layer context: loaded entries, known-bad matches."""

    loaded_memories: list[dict] = field(default_factory=list)
    # Memories surfaced during orientation (boot + anchor("memory"))

    known_bad_checked: bool = False
    # Whether anchor("known_bad") has run this session

    known_bad_matches: list[dict] = field(default_factory=list)
    # Memory entries that matched during known_bad check

    new_memories: list[dict] = field(default_factory=list)
    # Memories created this session via anchor("save") or anchor("learn")

    auto_promoted: list[str] = field(default_factory=list)
    # Entry IDs that were auto-promoted to confirmed this session

    audit_trend: dict = field(default_factory=dict)
    # From anchor("audit_history") — historical compliance trend

    def has_gotcha_for(self, subject: str) -> bool:
        """Return True if any loaded memory is a gotcha touching this subject."""
        subject_lower = subject.lower()
        for m in self.loaded_memories:
            if m.get("type") == "gotcha":
                content = m.get("content", "").lower()
                tags = " ".join(m.get("tags_list", [])).lower() if m.get("tags_list") else ""
                if subject_lower in content or subject_lower in tags:
                    return True
        return False

    def get_gotchas_for(self, subject: str) -> list[str]:
        """Return content strings for all gotcha memories touching this subject."""
        subject_lower = subject.lower()
        results: list[str] = []
        for m in self.loaded_memories:
            if m.get("type") == "gotcha":
                content = m.get("content", "")
                tags = " ".join(m.get("tags_list", [])).lower() if m.get("tags_list") else ""
                if subject_lower in content.lower() or subject_lower in tags:
                    results.append(content)
        return results

    def get_relevant_memories(self, file_or_table: str) -> list[dict]:
        """Return loaded memories whose content or tags mention this subject."""
        key = file_or_table.lower()
        return [
            m for m in self.loaded_memories
            if key in m.get("content", "").lower()
            or key in " ".join(m.get("tags_list", [])).lower()
        ]

    def to_dict(self) -> dict:
        return {
            "loaded_memories": self.loaded_memories,
            "known_bad_checked": self.known_bad_checked,
            "known_bad_matches": self.known_bad_matches,
            "new_memories": self.new_memories,
            "auto_promoted": self.auto_promoted,
            "audit_trend": self.audit_trend,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryContext":
        return cls(
            loaded_memories=d.get("loaded_memories", []),
            known_bad_checked=d.get("known_bad_checked", False),
            known_bad_matches=d.get("known_bad_matches", []),
            new_memories=d.get("new_memories", []),
            auto_promoted=d.get("auto_promoted", []),
            audit_trend=d.get("audit_trend", {}),
        )



@dataclass
class PlanContext:
    """Captures the active plan from anchor('task') for downstream verification."""

    task: str = ""
    mode: str = ""
    readiness_score: int = 0
    risks: "list[str]" = field(default_factory=list)
    acceptance_criteria: "list[str]" = field(default_factory=list)
    recorded_at: str = ""

    def to_dict(self) -> dict:
        return {
            "task": self.task,
            "mode": self.mode,
            "readiness_score": self.readiness_score,
            "risks": self.risks,
            "acceptance_criteria": self.acceptance_criteria,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PlanContext":
        return cls(
            task=d.get("task", ""),
            mode=d.get("mode", ""),
            readiness_score=d.get("readiness_score", 0),
            risks=d.get("risks", []),
            acceptance_criteria=d.get("acceptance_criteria", []),
            recorded_at=d.get("recorded_at", ""),
        )


@dataclass
class ComplianceContext:
    """Tracks running compliance state: score, gates, gaps."""

    running_score: int = 0
    max_score: int = 10
    gates_passed: list[str] = field(default_factory=list)
    # Which gates have cleared this session (e.g. ["status", "memory", "task"])

    gates_pending: list[str] = field(default_factory=list)
    # Which gates still need to run

    active_gaps: list[str] = field(default_factory=list)
    # Current compliance gaps from _compliance_audit()

    historical_trend: dict = field(default_factory=dict)
    # From session_audits table: avg_score, trend, top_gaps

    files_since_checkpoint: int = 0
    # Counter for ungated edit limit enforcement

    def is_compliant(self) -> bool:
        """Return True if no critical gates are pending."""
        return len(self.gates_pending) == 0

    def next_required_gate(self) -> str | None:
        """Return the next gate that must run, or None."""
        return self.gates_pending[0] if self.gates_pending else None

    def to_dict(self) -> dict:
        return {
            "running_score": self.running_score,
            "max_score": self.max_score,
            "gates_passed": self.gates_passed,
            "gates_pending": self.gates_pending,
            "active_gaps": self.active_gaps,
            "historical_trend": self.historical_trend,
            "files_since_checkpoint": self.files_since_checkpoint,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ComplianceContext":
        return cls(
            running_score=d.get("running_score", 0),
            max_score=d.get("max_score", 10),
            gates_passed=d.get("gates_passed", []),
            gates_pending=d.get("gates_pending", []),
            active_gaps=d.get("active_gaps", []),
            historical_trend=d.get("historical_trend", {}),
            files_since_checkpoint=d.get("files_since_checkpoint", 0),
        )


# ─── Main Frame ───────────────────────────────────────────────────────────────


@dataclass
class ContextFrame:
    """Living context that accumulates across a session.

    Every anchor() call writes its findings, risks, and actions here.
    Tools can read from the frame to avoid redundant work and produce
    contextual suggestions.

    Design principles:
        - Append-only: tools add to the frame, never remove
        - Transparent: the anchor() dispatcher manages the frame, not tools
        - Lightweight: stores summaries only, never DataFrames or large objects
        - Serializable: to_dict() / from_dict() for debugging and handoffs
        - <5ms overhead per tool call (no I/O, no scanning)
    """

    # Identity
    session_id: str
    project: str
    manifest: dict | None

    # Accumulated context (tools write here via record())
    findings: list[Finding] = field(default_factory=list)
    risks: list[Risk] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    data_context: DataContext = field(default_factory=DataContext)
    code_context: CodeContext = field(default_factory=CodeContext)
    memory_context: MemoryContext = field(default_factory=MemoryContext)
    compliance: ComplianceContext = field(default_factory=ComplianceContext)
    plan_context: "PlanContext | None" = None

    # Raw tool results (keyed by action name, each is a list of results)
    tool_results: dict[str, list[dict]] = field(default_factory=dict)

    # Timeline (ordered log of what happened)
    actions: list[Action] = field(default_factory=list)

    # Timestamps
    created: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # ─── Factory ──────────────────────────────────────────────────────────────

    @classmethod
    def new(
        cls,
        session_id: str | None = None,
        project: str = "",
        manifest: dict | None = None,
    ) -> "ContextFrame":
        """Create a fresh, empty frame for a new session."""
        return cls(
            session_id=session_id or str(uuid.uuid4())[:8],
            project=project,
            manifest=manifest,
        )

    # ─── Record ───────────────────────────────────────────────────────────────

    def record(self, action: str, result: Any) -> int:
        """Extract findings and risks from a tool result and append to the frame.

        Safe to call with any result type — non-dict and non-StandardContract
        results are silently skipped (no crash).

        Args:
            action: The anchor() action name (e.g. "profile_table", "microscope").
            result: The tool return value. Only dicts matching StandardContract
                    shape are processed; all others are skipped.

        Returns:
            Number of findings extracted (0 if result was skipped).
        """
        if not isinstance(result, dict):
            return 0

        now = datetime.now(timezone.utc)
        subject = result.get("subject", "")
        kind = result.get("kind", "unknown")
        findings_extracted = 0

        # Store raw result (lightweight — reference only)
        self.tool_results.setdefault(action, []).append(result)

        # ── Extract findings ──
        raw_findings = result.get("findings", [])
        if isinstance(raw_findings, list):
            for f in raw_findings:
                msg = f if isinstance(f, str) else str(f)
                if not msg:
                    continue
                # Infer category from kind and content
                category = _infer_category(kind, msg)
                severity = _infer_severity(msg)
                self.findings.append(Finding(
                    source=action,
                    category=category,
                    severity=severity,
                    message=msg,
                    subject=subject,
                    evidence=result.get("metrics", {}),
                    timestamp=now,
                ))
                findings_extracted += 1

        # ── Extract risks ──
        raw_risks = result.get("risks", [])
        if isinstance(raw_risks, list):
            for r in raw_risks:
                msg = r if isinstance(r, str) else str(r)
                if not msg:
                    continue
                self.risks.append(Risk(
                    source=action,
                    severity=_infer_risk_severity(msg),
                    message=msg,
                    mitigation="",  # Tools don't provide structured mitigation yet
                    timestamp=now,
                ))

        # ── Update sub-contexts from specific tool kinds ──
        self._update_sub_contexts(action, kind, result, subject)

        # ── Log action ──
        self.actions.append(Action(
            tool=action,
            args_summary=str(subject)[:80],
            elapsed_ms=0.0,  # Timing is set by the dispatcher separately
            success=True,
            findings_produced=findings_extracted,
            timestamp=now,
        ))

        self.last_updated = now
        return findings_extracted

    def _update_sub_contexts(
        self, action: str, kind: str, result: dict, subject: str
    ) -> None:
        """Update sub-context objects from recognized tool output shapes."""
        metrics = result.get("metrics", {}) if isinstance(result.get("metrics"), dict) else {}

        # ── DataContext: profiler, exploration, microscope ──
        if action in ("profile_table",) or kind in (
            "dataset_profile", "exploration_context", "table_profile"
        ):
            if subject:
                summary = {
                    "kind": kind,
                    "row_count": metrics.get("row_count"),
                    "column_count": metrics.get("column_count"),
                    "null_rate": metrics.get("null_rate"),
                    "grain": metrics.get("grain"),
                    "freshness": metrics.get("freshness"),
                    "data_quality_score": metrics.get("data_quality_score"),
                }
                self.data_context.tables_profiled[subject] = summary
                if isinstance(metrics.get("data_quality_score"), (int, float)):
                    self.data_context.data_quality_scores[subject] = float(metrics["data_quality_score"])
                if metrics.get("grain"):
                    self.data_context.grain_detected[subject] = metrics["grain"] if isinstance(metrics["grain"], list) else [metrics["grain"]]

        # ── CodeContext: map, preflight, import_resolve ──
        elif action == "map" or kind == "codebase_map":
            self.code_context.codebase_map = {
                "total_files": metrics.get("total_files"),
                "total_functions": metrics.get("total_functions"),
                "summary": result.get("summary", ""),
            }
        elif action == "preflight" or kind == "preflight_context":
            violations = result.get("samples", {})
            if isinstance(violations, dict):
                for fname, v in violations.items():
                    if isinstance(v, list):
                        for item in v:
                            self.code_context.convention_violations.append(
                                {"file": fname, "violation": item}
                            )
        elif action == "import_resolve" or kind == "import_resolve":
            self.code_context.import_graph = metrics

        # ── MemoryContext: memory, known_bad ──
        elif action == "memory" or kind == "memory_context":
            entries = result.get("entries", [])
            if isinstance(entries, list):
                self.memory_context.loaded_memories.extend(entries)
        elif action == "known_bad" or kind == "known_bad_context":
            self.memory_context.known_bad_checked = True
            matches = result.get("samples", {}).get("matches", [])
            if isinstance(matches, list):
                self.memory_context.known_bad_matches.extend(matches)

        # ── ComplianceContext: status, gate, task ──
        elif action == "status" or kind == "session_status":
            if "status" not in self.compliance.gates_passed:
                self.compliance.gates_passed.append("status")
        elif action == "task" or kind == "task_execution_context":
            if "task" not in self.compliance.gates_passed:
                self.compliance.gates_passed.append("task")
            # Record plan context for downstream verification
            readiness = result.get("readiness", {})
            raw_risks = result.get("risks", {})
            if isinstance(raw_risks, dict):
                risk_items = raw_risks.get("items", [])
            elif isinstance(raw_risks, list):
                risk_items = raw_risks
            else:
                risk_items = []
            raw_verification = result.get("verification", {})
            if isinstance(raw_verification, dict):
                ac_items = raw_verification.get("acceptance_criteria", [])
            else:
                ac_items = []
            self.plan_context = PlanContext(
                task=result.get("subject", ""),
                mode=result.get("mode", ""),
                readiness_score=readiness.get("score", 0) if isinstance(readiness, dict) else 0,
                risks=[r if isinstance(r, str) else str(r) for r in risk_items],
                acceptance_criteria=[c if isinstance(c, str) else str(c) for c in ac_items],
                recorded_at=datetime.now(timezone.utc).isoformat(),
            )
        elif action == "gate" or kind == "workflow_gate":
            if "gate" not in self.compliance.gates_passed:
                self.compliance.gates_passed.append("gate")
            gaps = result.get("gaps", [])
            if isinstance(gaps, list):
                self.compliance.active_gaps = gaps

    # ─── Querying ─────────────────────────────────────────────────────────────

    def findings_by_category(self) -> dict[str, int]:
        """Return finding counts by category."""
        counts: dict[str, int] = {}
        for f in self.findings:
            counts[f.category] = counts.get(f.category, 0) + 1
        return counts

    def findings_by_source(self) -> dict[str, int]:
        """Return finding counts by source tool."""
        counts: dict[str, int] = {}
        for f in self.findings:
            counts[f.source] = counts.get(f.source, 0) + 1
        return counts

    def risks_by_severity(self) -> dict[str, int]:
        """Return risk counts by severity level."""
        counts: dict[str, int] = {}
        for r in self.risks:
            counts[r.severity] = counts.get(r.severity, 0) + 1
        return counts

    def search_findings(self, query: str) -> list[Finding]:
        """Return findings whose message contains the query (case-insensitive)."""
        q = query.lower()
        return [f for f in self.findings if q in f.message.lower()]

    def get_section(self, section: str) -> dict:
        """Return a summary of a named section: data, code, memory, compliance, risks, actions."""
        section = section.lower()
        if section == "data":
            return self.data_context.to_dict()
        elif section == "code":
            return self.code_context.to_dict()
        elif section == "memory":
            return self.memory_context.to_dict()
        elif section == "compliance":
            return self.compliance.to_dict()
        elif section == "risks":
            return {"risks": [r.to_dict() for r in self.risks]}
        elif section == "actions":
            return {"actions": [a.to_dict() for a in self.actions]}
        elif section == "findings":
            return {"findings": [f.to_dict() for f in self.findings]}
        else:
            return {"error": f"Unknown section: {section!r}. Valid: data, code, memory, compliance, risks, actions, findings"}

    # ─── Serialization ────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Return a JSON-serializable snapshot of the frame."""
        return {
            "session_id": self.session_id,
            "project": self.project,
            "manifest": self.manifest,
            "findings": [f.to_dict() for f in self.findings],
            "risks": [r.to_dict() for r in self.risks],
            "decisions": [d.to_dict() for d in self.decisions],
            "data_context": self.data_context.to_dict(),
            "code_context": self.code_context.to_dict(),
            "memory_context": self.memory_context.to_dict(),
            "compliance": self.compliance.to_dict(),
            "plan_context": self.plan_context.to_dict() if self.plan_context else None,
            "tool_results_keys": list(self.tool_results.keys()),  # Keys only — not raw data
            "actions": [a.to_dict() for a in self.actions],
            "created": self.created.isoformat(),
            "last_updated": self.last_updated.isoformat(),
            # Derived stats (convenience)
            "total_findings": len(self.findings),
            "total_risks": len(self.risks),
            "total_actions": len(self.actions),
            "findings_by_category": self.findings_by_category(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ContextFrame":
        """Deserialize a frame from a snapshot dict."""
        def _ts(s: Any) -> datetime:
            if isinstance(s, str):
                return datetime.fromisoformat(s)
            return datetime.now(timezone.utc)

        frame = cls(
            session_id=d.get("session_id", str(uuid.uuid4())[:8]),
            project=d.get("project", ""),
            manifest=d.get("manifest"),
            findings=[Finding.from_dict(f) for f in d.get("findings", [])],
            risks=[Risk.from_dict(r) for r in d.get("risks", [])],
            decisions=[Decision.from_dict(dec) for dec in d.get("decisions", [])],
            data_context=DataContext.from_dict(d.get("data_context", {})),
            code_context=CodeContext.from_dict(d.get("code_context", {})),
            memory_context=MemoryContext.from_dict(d.get("memory_context", {})),
            compliance=ComplianceContext.from_dict(d.get("compliance", {})),
            plan_context=PlanContext.from_dict(d["plan_context"]) if d.get("plan_context") else None,
            tool_results={},  # Raw results are not round-tripped (keys only)
            actions=[Action.from_dict(a) for a in d.get("actions", [])],
            created=_ts(d.get("created")),
            last_updated=_ts(d.get("last_updated")),
        )
        return frame

    def __repr__(self) -> str:
        return (
            f"ContextFrame(session={self.session_id!r}, project={self.project!r}, "
            f"findings={len(self.findings)}, risks={len(self.risks)}, "
            f"actions={len(self.actions)})"
        )


# ─── FrameRecorder ────────────────────────────────────────────────────────────


class FrameRecorder:
    """Stateless helper that extracts and routes data from tool results.

    This is the glue between anchor() dispatcher output and the ContextFrame.
    It is separate from ContextFrame.record() to allow testing in isolation
    and to keep ContextFrame focused on storage.

    Usage:
        recorder = FrameRecorder()
        recorder.record(frame, "profile_table", result)
    """

    def record(self, frame: ContextFrame, action: str, result: Any) -> int:
        """Record a tool result into the frame. Returns finding count."""
        return frame.record(action, result)

    def is_recordable(self, result: Any) -> bool:
        """Return True if the result can be recorded (is a StandardContract dict)."""
        return (
            isinstance(result, dict)
            and ("kind" in result or "findings" in result or "risks" in result)
        )


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _infer_category(kind: str, message: str) -> str:
    """Infer a Finding category from tool kind and message content."""
    msg_lower = message.lower()

    # Explicit kind-based routing first
    kind_to_category = {
        "dataset_profile": "data_quality",
        "table_profile": "data_quality",
        "exploration_context": "data_quality",
        "preflight_context": "convention",
        "workflow_gate": "compliance",
        "session_status": "compliance",
        "task_execution_context": "compliance",
        "error_trace": "debugging",
        "failure_pattern": "debugging",
        "known_bad_context": "convention",
    }
    if kind in kind_to_category:
        return kind_to_category[kind]

    # Keyword-based fallback
    if any(w in msg_lower for w in ("null", "missing", "duplicate", "cardinality", "freshness", "stale")):
        return "data_quality"
    if any(w in msg_lower for w in ("convention", "style", "naming", "import", "lint", "violation")):
        return "convention"
    if any(w in msg_lower for w in ("slow", "performance", "timeout", "memory", "oom", "collect")):
        return "performance"
    if any(w in msg_lower for w in ("blocked", "gate", "planning", "compliance", "obligation")):
        return "compliance"
    if any(w in msg_lower for w in ("error", "exception", "traceback", "failed", "broken")):
        return "debugging"

    return "unknown"


def _infer_severity(message: str) -> str:
    """Infer Finding severity from message prefix/content."""
    msg_lower = message.lower()
    if any(p in msg_lower for p in ("must:", "blocked:", "critical", "error", "failed", "broken")):
        return "critical"
    if any(p in msg_lower for p in ("should:", "warning", "warn:", "high null", "missing")):
        return "warning"
    return "info"


def _infer_risk_severity(message: str) -> str:
    """Infer Risk severity from message content."""
    msg_lower = message.lower()
    if any(w in msg_lower for w in ("high", "critical", "blocked", "must", "error")):
        return "high"
    if any(w in msg_lower for w in ("medium", "should", "warning", "missing")):
        return "medium"
    return "low"

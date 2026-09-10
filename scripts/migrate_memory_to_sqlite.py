"""migrate_memory_to_sqlite.py — Import legacy memory sources into SQLite.

Reads from three sources (in priority order):
  1. .agent_memory.md    — primary, richest data (gotchas, patterns, decisions, conventions)
  2. .agent_memory.jsonl — secondary, structured entries with metadata
  3. failure_patterns.yaml — failure pattern entries with regex/fix/never_try

Writes to: .agent_memory.db (SQLite with FTS5)

Usage:
    PYTHONDONTWRITEBYTECODE=1 python scripts/migrate_memory_to_sqlite.py

    # Dry run (shows what would be imported):
    PYTHONDONTWRITEBYTECODE=1 python scripts/migrate_memory_to_sqlite.py --dry-run

    # Clear DB first (fresh import):
    PYTHONDONTWRITEBYTECODE=1 python scripts/migrate_memory_to_sqlite.py --fresh
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from odibi_anchor.codebase._memory_db import (
    get_db,
    insert_memory,
    entry_count,
    close_db,
    VALID_TYPES,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = str(PROJECT_ROOT / ".agent_memory.db")
MD_PATH = PROJECT_ROOT / ".agent_memory.md"
JSONL_PATH = PROJECT_ROOT / ".agent_memory.jsonl"
YAML_PATH = PROJECT_ROOT / "failure_patterns.yaml"

PROJECT = "odibi_anchor"

# Type mapping from markdown section headers
SECTION_TYPE_MAP = {
    "gotchas": "gotcha",
    "patterns": "pattern",
    "decisions": "decision",
    "conventions": "convention",
    "discoveries": "discovery",
    "preferences": "preference",
}


# ---------------------------------------------------------------------------
# Markdown Parser
# ---------------------------------------------------------------------------

def parse_markdown(md_path: Path) -> list[dict]:
    """Parse .agent_memory.md into structured entries.

    Format:
        ## Section Header (maps to entry type)
        - **label**: content text
          - Files: `glob1`, `glob2`
        - **label**: content text [tag1, tag2]

    Also handles multi-line entries and subsections (## v0.3.x ...)
    """
    if not md_path.exists():
        return []

    entries = []
    current_type = None
    current_section = ""

    with open(md_path) as f:
        lines = f.readlines()

    i = 0
    while i < len(lines):
        line = lines[i].rstrip()

        # H2 section header -> determines type
        if line.startswith("## "):
            header = line[3:].strip().lower()
            # Map known headers to types
            matched = False
            for key, entry_type in SECTION_TYPE_MAP.items():
                if key in header:
                    current_type = entry_type
                    current_section = header
                    matched = True
                    break
            if not matched:
                # Versioned sections (## v0.3.x ...) -> treat as "decision"
                if re.match(r"v\d+\.\d+", header):
                    current_type = "decision"
                    current_section = header
                # "gotchas & conventions" combo sections
                elif "gotcha" in header and "convention" in header:
                    current_type = "convention"
                    current_section = header
                else:
                    current_type = "decision"
                    current_section = header
            i += 1
            continue

        # Bullet entry (- **label**: content)
        if line.startswith("- ") and current_type:
            content = line[2:].strip()

            # Check for continuation lines (indented under this bullet)
            j = i + 1
            sub_files = []
            while j < len(lines):
                next_line = lines[j].rstrip()
                if next_line.startswith("  - Files:"):
                    # Extract file globs
                    files_text = next_line.split("Files:")[1].strip()
                    sub_files = [f.strip().strip("`") for f in files_text.split(",")]
                    j += 1
                elif next_line.startswith("  ") and not next_line.startswith("- "):
                    content += " " + next_line.strip()
                    j += 1
                else:
                    break
            i = j

            # Extract tags from content [tag1, tag2] pattern
            tags = []
            tag_match = re.search(r"\[([^\]]+)\]", content)
            # Only treat as tags if it looks like a tag list (short comma-separated words)
            if tag_match:
                potential_tags = tag_match.group(1)
                if "," in potential_tags and all(
                    len(t.strip()) < 30 for t in potential_tags.split(",")
                ):
                    tags = [t.strip().strip("'\"") for t in potential_tags.split(",")]

            # Clean up bold markers
            content = re.sub(r"\*\*([^*]+)\*\*", r"\1", content)

            # Extract confidence from (conf=X.X) pattern
            confidence = 0.7  # Default for confirmed entries from .md
            conf_match = re.search(r"\(conf[idence]*=(\d+\.?\d*)\)", content)
            if conf_match:
                confidence = float(conf_match.group(1))
                content = re.sub(r"\s*\(conf[idence]*=\d+\.?\d*\)", "", content)

            # Add section context as tag
            if current_section and current_section not in [t.lower() for t in tags]:
                section_tag = re.sub(r"[^a-z0-9_]", "_", current_section.split("(")[0].strip())
                section_tag = re.sub(r"_+", "_", section_tag).strip("_")
                if len(section_tag) > 3:
                    tags.append(section_tag)

            entries.append({
                "type": current_type,
                "content": content.strip(),
                "related_files": sub_files,
                "tags": tags,
                "confidence": confidence,
                "status": "confirmed",  # .md entries are vetted
                "source": "migration:.agent_memory.md",
            })
            continue

        i += 1

    return entries


# ---------------------------------------------------------------------------
# JSONL Parser
# ---------------------------------------------------------------------------

def parse_jsonl(jsonl_path: Path) -> list[dict]:
    """Parse .agent_memory.jsonl into entries."""
    if not jsonl_path.exists():
        return []

    entries = []
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue

            entry_type = raw.get("type", "discovery")
            if entry_type not in VALID_TYPES:
                entry_type = "discovery"

            entries.append({
                "type": entry_type,
                "content": raw.get("content", "").strip(),
                "related_files": raw.get("related_files", []),
                "tags": raw.get("tags", []),
                "confidence": raw.get("confidence", 0.5),
                "status": raw.get("status", "candidate"),
                "source": "migration:.agent_memory.jsonl",
                "evidence": raw.get("evidence", {}),
            })

    return entries


# ---------------------------------------------------------------------------
# YAML Parser (minimal — no pyyaml dependency)
# ---------------------------------------------------------------------------

def parse_failure_patterns(yaml_path: Path) -> list[dict]:
    """Parse failure_patterns.yaml without pyyaml dependency.

    Each entry becomes a failure_pattern type memory with:
    - content: "Pattern: {pattern} | Context: {context} | Root cause: {root_cause} | Fix: {fix}"
    - tags: extracted from pattern keywords
    - evidence: {never_try: [...]}
    """
    if not yaml_path.exists():
        return []

    with open(yaml_path) as f:
        text = f.read()

    entries = []
    # Split on top-level list items (lines starting with "- pattern:")
    blocks = re.split(r"^- pattern:", text, flags=re.MULTILINE)

    for block in blocks[1:]:  # Skip header comments
        lines = block.strip().split("\n")

        # First line is the pattern value
        pattern = lines[0].strip().strip('"').strip("'")

        # Parse remaining key: value pairs
        fields = {"pattern": pattern}
        current_key = None
        for line in lines[1:]:
            # Key-value pair
            kv_match = re.match(r"^\s{2}(\w+):\s*(.*)", line)
            if kv_match:
                current_key = kv_match.group(1)
                value = kv_match.group(2).strip().strip('"').strip("'")
                # Handle inline list
                if value.startswith("["):
                    try:
                        fields[current_key] = json.loads(value.replace("'", '"'))
                    except json.JSONDecodeError:
                        fields[current_key] = [
                            v.strip().strip('"').strip("'")
                            for v in value.strip("[]").split(",")
                        ]
                else:
                    fields[current_key] = value

        # Build content string
        parts = [f"Pattern: {fields.get('pattern', '')}"]
        if fields.get("context"):
            parts.append(f"Context: {fields['context']}")
        if fields.get("root_cause"):
            parts.append(f"Root cause: {fields['root_cause']}")
        if fields.get("fix"):
            parts.append(f"Fix: {fields['fix']}")
        content = " | ".join(parts)

        # Extract tags from pattern keywords
        tags = ["failure_pattern"]
        pattern_words = re.findall(r"[a-zA-Z_]+", pattern)
        # Add meaningful keywords as tags
        for word in pattern_words:
            if len(word) > 3 and word.lower() not in ("not", "found", "has", "the", "but"):
                tags.append(word.lower())
                if len(tags) >= 5:
                    break

        evidence = {}
        if fields.get("never_try"):
            evidence["never_try"] = fields["never_try"]

        entries.append({
            "type": "failure_pattern",
            "content": content,
            "related_files": [],
            "tags": tags[:5],
            "confidence": 0.9,  # Failure patterns are high-confidence
            "status": "confirmed",
            "source": "migration:failure_patterns.yaml",
            "evidence": evidence,
        })

    return entries


# ---------------------------------------------------------------------------
# Migration Runner
# ---------------------------------------------------------------------------

def run_migration(*, dry_run: bool = False, fresh: bool = False) -> dict:
    """Run the full migration.

    Returns:
        Summary dict with counts.
    """
    print("=" * 60)
    print("SQLite Memory Migration")
    print("=" * 60)
    print(f"  DB path:    {DB_PATH}")
    print(f"  Project:    {PROJECT}")
    print(f"  Dry run:    {dry_run}")
    print(f"  Fresh:      {fresh}")
    print()

    if fresh and not dry_run:
        # Remove existing DB
        if os.path.exists(DB_PATH):
            close_db(DB_PATH)
            os.remove(DB_PATH)
            print("  Removed existing DB")

    # Parse all sources
    print("Parsing sources...")
    md_entries = parse_markdown(MD_PATH)
    print(f"  .agent_memory.md:      {len(md_entries)} entries")

    jsonl_entries = parse_jsonl(JSONL_PATH)
    print(f"  .agent_memory.jsonl:   {len(jsonl_entries)} entries")

    fp_entries = parse_failure_patterns(YAML_PATH)
    print(f"  failure_patterns.yaml: {len(fp_entries)} entries")

    total_parsed = len(md_entries) + len(jsonl_entries) + len(fp_entries)
    print(f"  Total parsed:          {total_parsed}")
    print()

    if dry_run:
        print("--- DRY RUN: Entries that would be imported ---\n")
        for label, entries in [
            (".agent_memory.md", md_entries),
            (".agent_memory.jsonl", jsonl_entries),
            ("failure_patterns.yaml", fp_entries),
        ]:
            if entries:
                print(f"  {label}:")
                for e in entries[:5]:
                    print(f"    [{e['type']}] {e['content'][:70]}...")
                if len(entries) > 5:
                    print(f"    ... and {len(entries) - 5} more")
                print()
        return {"parsed": total_parsed, "inserted": 0, "deduped": 0}

    # Insert all entries
    print("Inserting into SQLite...")
    inserted = 0
    deduped = 0

    for label, entries in [
        (".agent_memory.md", md_entries),
        (".agent_memory.jsonl", jsonl_entries),
        ("failure_patterns.yaml", fp_entries),
    ]:
        source_inserted = 0
        source_deduped = 0
        for e in entries:
            result = insert_memory(
                DB_PATH,
                project=PROJECT,
                type=e["type"],
                content=e["content"],
                related_files=e.get("related_files"),
                tags=e.get("tags"),
                source=e.get("source", "migration"),
                confidence=e.get("confidence", 0.5),
                status=e.get("status", "candidate"),
                evidence=e.get("evidence"),
            )
            if result["action"] == "inserted":
                source_inserted += 1
            else:
                source_deduped += 1
        print(f"  {label}: {source_inserted} inserted, {source_deduped} deduped")
        inserted += source_inserted
        deduped += source_deduped

    # Final stats
    counts = entry_count(DB_PATH)
    print(f"\n{'=' * 60}")
    print(f"Migration complete!")
    print(f"  Inserted:  {inserted}")
    print(f"  Deduped:   {deduped}")
    print(f"  DB totals: {counts}")
    print(f"  DB size:   {os.path.getsize(DB_PATH) / 1024:.1f} KB")
    print(f"{'=' * 60}")

    return {"parsed": total_parsed, "inserted": inserted, "deduped": deduped, "db_counts": counts}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate memory sources to SQLite")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be imported")
    parser.add_argument("--fresh", action="store_true", help="Remove existing DB first")
    args = parser.parse_args()

    run_migration(dry_run=args.dry_run, fresh=args.fresh)

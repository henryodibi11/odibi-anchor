# Greenfield planning

> Preserved greenfield planning technique.

Evidence-gathering checklist for greenfield tasks — building a new tool, parser, extractor,
or utility from scratch. Execute this checklist BEFORE calling `anchor("task")`. Every item you
complete becomes a `known_fact` or `constraint` in your plan.

Use this local reference when greenfield planning is material to the managed work item.

## Core Principle

The #1 cause of tools that need multiple rewrites is **speccing against assumptions instead
of evidence.** A tool built after testing 5 real inputs will have 5x fewer bugs than one built
from a description of the input format. Research absorbs uncertainty. Specs lock decisions.
Implementation becomes mechanical.

**The experiment→annotate→spec→implement flow:**

```
Explore (notebooks against real inputs)
  → Annotate (test corpus with expected behavior)
    → Benchmark (library/approach selection on real data)
      → Spec (derived from discovered edge cases, not assumptions)
        → Implement (translation, not discovery)
```

## Evidence Checklist — Execute In Order

### 1. Define the Problem Space

Answer these concretely before anything else:

| Question | Record as |
|---|---|
| What does this tool accept as input? (file type, API response, data shape) | `known_fact` |
| What does this tool produce as output? (DataFrame, dict, dataclass, file) | `known_fact` |
| Who uses the output and how? (bronze layer, LLM context, human review) | `known_fact` |
| What does "correct" look like? (can you define it without seeing the data?) | `constraint` |

**Rule:** If you cannot describe the input format from firsthand observation, STOP.
You are not ready to continue — you need to see real inputs first.

### 2. Test Corpus Gate (HARD STOP)

This is the critical checkpoint. Do NOT proceed past this step without real-world inputs.

**Ask the user:**

> "Do you have real-world files/inputs for this tool? Where are they?"

| User Answer | Action | Record |
|---|---|---|
| "Yes, here they are" (path provided) | Point to location → use for all research | `known_fact: "Test corpus: {path}, N files"` |
| "I can get some" | **STOP** — research is blocked until they return with ≥5 representative inputs | Wait for user |
| "I can't get real files but I know the structure" | Create synthetic samples that cover known edge cases → annotate expected behavior | `known_fact: "Synthetic corpus: {path}, N files, covers {patterns}"` |
| "I don't know what the inputs look like" | **HARD STOP** — you cannot spec what you cannot see | Ask user to find examples |

**Minimum corpus size** scales with domain complexity:

| Domain complexity | Minimum inputs | Examples |
|---|---|---|
| Low (uniform format, predictable structure) | 3-5 | JSON API responses, CSV with fixed schema |
| Medium (multiple variants, some edge cases) | 10-20 | Excel files from different teams, config files |
| High (many producers, inconsistent formatting) | 30-50+ | PDFs from different utilities, scraped HTML, legacy exports |

**Ask the user:** "How many distinct patterns do you expect in production inputs?"
Their answer determines the minimum corpus size.

**Rule:** If the user cannot provide real inputs AND cannot describe the structure well
enough to build synthetic ones, the tool cannot be specced reliably. Surface this risk
explicitly — do not paper over it with assumptions.

### 3. Explore with Research Notebooks

Create throwaway notebooks to understand the input domain. This is NOT implementation — it's
discovery. The goal is to find edge cases, not to write reusable code.

**For each input in your corpus (or a representative sample of ≥5):**

```python
# What does the raw data look like?
# What are the structural patterns?
# What varies between inputs?
# What breaks your initial assumptions?
```

**Record as known_facts:**
- Structural patterns that are consistent (these become spec requirements)
- Variations between inputs (these become parameters or branching logic)
- Edge cases that surprised you (these become explicit test cases)
- Things that don't work as expected (these become bug-prevention rules)

**Record as risks:**
- Inputs that resist the general approach
- Ambiguous cases where "correct" behavior isn't clear
- Performance concerns discovered during exploration

**Minimum discovery threshold:** You must have explored enough inputs to find at least
ONE edge case you didn't predict. If everything matches your assumptions perfectly, either
your corpus is too small or your exploration isn't deep enough.

### 4. Library/Approach Selection

If this tool uses an external library, benchmark candidates against your REAL corpus.
Do NOT select a library based on documentation alone.

**For each candidate library:**

| Criterion | How to evaluate |
|---|---|
| Correctness | Run against ≥5 diverse inputs, verify output manually |
| Edge case handling | Run against your discovered edge cases specifically |
| Performance | Time against largest input in corpus |
| API ergonomics | Write the "happy path" code — is it readable? |
| Failure modes | What happens on malformed input? Silent failure vs clear error? |
| Dependencies | System deps (ghostscript, Java)? Databricks-compatible? Serverless? |

**Record as known_facts:**
- Winner and why (concrete evidence, not "docs look good")
- Eliminated candidates and why (concrete failures, not "seemed slower")
- Winner's limitations discovered during benchmarking
- Winner's unexpected strengths discovered during benchmarking

**If no library exists** (pure implementation):
- Record the approach you'll take and why
- Identify the hardest sub-problem and sketch a solution
- Verify the approach works on at least 3 real inputs before speccing

Use [integration](integration.md) for the detailed library research checklist
(API surface, gotchas, docs review). That skill covers library RESEARCH; this step adds
the BENCHMARK against real data that a routine integration technique does not enforce.

### 5. Annotate Expected Behavior

For each input in your corpus (or the representative sample), document what correct output
looks like. This becomes your test oracle.

**Annotation format (minimum):**

```
Input: filename.ext
Expected output:
  - [specific field]: [expected value]
  - [table shape]: [NxM]
  - [key decision]: [how to handle ambiguity X]
Notes: [anything surprising about this input]
```

**For complex domains**, create a structured annotations file:

```python
# test_bank_annotations.json or test_corpus_notes.md
{
  "file_1.pdf": {
    "tables": 3,
    "header_rows": 2,
    "edge_cases": ["merged cells on page 2", "footer bleeds into table"],
    "expected_columns": ["ID", "Name", "Status"],
  },
  ...
}
```

**Record as known_facts:**
- Annotation file location
- Number of inputs annotated
- Edge cases cataloged (list them)
- Decisions made about ambiguous cases

**Rule:** Every annotated decision about "correct behavior" becomes either a test case
or a spec requirement. Nothing should be annotated and then forgotten.

### 6. Catalog Discovered Edge Cases

Before spec writing, explicitly list every edge case found during exploration:

| Edge Case | Discovered In | Proposed Handling | Test Case? |
|---|---|---|---|
| [description] | [which input] | [approach] | Yes/No |

**This table becomes:**
- Spec requirements (the "Proposed Handling" column)
- Test cases (everything marked "Yes")
- Known limitations (things you decide NOT to handle, documented explicitly)

**Record as constraints:**
- Edge cases you WILL handle (these are requirements)
- Edge cases you WON'T handle (these are documented limitations)
- Edge cases you're unsure about (surface to user for decision)

### 7. Define Output Contract

Based on exploration, define the exact output shape:

```python
# What does the tool return?
# What are the field names and types?
# What guarantees does it make (non-null, non-empty, sorted)?
# What does it NOT guarantee (completeness, ordering)?
```

**Record as known_facts:**
- Output type (DataFrame, dataclass, dict, etc.)
- Field-by-field definition with types
- Invariants (things always true about the output)
- Non-guarantees (things that may vary)

### 8. Scope Decision

Based on all research above, decide what the FIRST VERSION handles:

| Category | Include in v1? | Why |
|---|---|---|
| [core functionality] | Yes | Covers 90% of corpus |
| [edge case X] | Yes | Found in 5+ inputs |
| [edge case Y] | No — v2 | Only 1 input, complex to handle |
| [feature Z] | No — never | Out of scope for this tool's purpose |

**Record as in_scope / out_of_scope:**
- v1 scope (what you're building now)
- v2 roadmap (what you'll add later, documented)
- Explicit exclusions (what this tool will never do)

## Escape Hatch — Trivial Tools

For tools with LOW domain complexity (uniform input, predictable structure, ≤3 inputs needed):

| Criterion | Threshold |
|---|---|
| Input format is fully specified (e.g., JSON schema, fixed CSV) | Skip step 3 (deep exploration) |
| Only one viable library exists | Skip step 4 (benchmarking) |
| Output is obvious from input (1:1 mapping) | Abbreviate step 5 (annotations) |

**You still MUST complete steps 1, 2, 6, 7, 8.** The test corpus gate and edge case
catalog are never skippable — even for "simple" tools.

## Output — What You Feed to `anchor("task")`

After completing this checklist, you should have:

```python
known_facts = [
    "Problem: Extract tables from utility PDFs as DataFrames for bronze layer",
    "Test corpus: /test_pdfs/ — 50 real PDFs from Dominion, FPL, GTC utilities",
    "Explored: 6 research notebooks, tested all 50 inputs",
    "Library: PyMuPDF — benchmarked against pdfplumber, camelot, tabula-py",
    "  Winner because: rotated page handling, bold flags, 3-10x speed, serverless OK",
    "  Eliminated: camelot (ghostscript dep), tabula-py (worst accuracy)",
    "Edge cases found: 14 — filename-as-title, page-number-at-heading-size, font clustering",
    "Output: PdfProfile dataclass → PdfSection tree → PdfTable.to_dataframe()",
    "Annotations: test_bank_annotations.json (298 PDFs cataloged, 20 priority verified)",
    "Header patterns: single-row (7 PDFs), double-row (4 PDFs), title+header (2 PDFs)",
]

constraints = [
    "Must handle all 14 discovered edge cases (not assumptions — real failures)",
    "Must work on Databricks serverless (no system deps like ghostscript/Java)",
    "Single dependency: pymupdf only",
    "Output must be DataFrames — downstream is spark.createDataFrame()",
    "Text-based PDFs only — image/OCR PDFs raise clear error (v2 scope)",
]

acceptance_criteria = [
    "All 50 test corpus PDFs extract without error",
    "Tables match annotated column counts for all 20 priority PDFs",
    "concat_page_tables produces correct row counts (verified at page boundaries)",
    "Edge cases 1-14 each have a dedicated test",
    "to_dataframe() produces valid pandas DataFrames",
]

in_scope = ["Text-based PDF extraction, table detection, multi-page concat"]
out_of_scope = ["OCR/image PDFs", "Form field extraction", "PDF generation"]

risks = [
    "Undiscovered edge case in production PDFs not in corpus (mitigation: expandable test bank)",
    "PyMuPDF API changes (mitigation: pin version, wrap all calls)",
]
```

Continue the accepted work item using the gathered evidence.

---

## Summary — The Research Investment Pays Off

| Phase | Time Investment | What It Prevents |
|---|---|---|
| Test corpus | 1-2 hours gathering files | Speccing against assumptions |
| Exploration notebooks | 2-4 hours of discovery | Discovering bugs during implementation |
| Library benchmark | 1-2 hours of comparison | Choosing wrong library, rewriting later |
| Annotations | 1-2 hours of documentation | Tests without oracles, undefined "correct" |
| Edge case catalog | 30 minutes of listing | Missing requirements in spec |

**Total research investment: ~1 day.** Compare to: 3-5 days of implementation rework
when edge cases surface one at a time during coding.
## Formal Spec compliance
If accepted task policy requires a formal Spec, use `writing-specs`, include its
compliance requirements, and review the exact linked Spec to a rating ≥ good
before consequential effects. Do not infer a Spec requirement from mode alone.

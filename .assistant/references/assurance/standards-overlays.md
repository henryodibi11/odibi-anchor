# Assurance standards overlays

- **Reference ID:** `assurance.standards-overlays`
- **Authored-reference version:** `1.0.0`
- **Catalog baseline:** `1.0.0`
- **Canonical overlay catalog SHA-256:** `0277bea95ab65a69255794fe9e4d651a3fcf95548b6b980aa63f93de7ccc473b`
- **Combined core and overlay catalog SHA-256:** `f58e6ca37d29134e0ace3f94b96315746aa1fd324a1cd120e5f5da3e0cba5d8c`
- **Captured and last reviewed:** 2026-08-20
- **Verification label:** `documented`

This Odibi Anchor (Anchor) reference defines eight conditional assurance overlays. It is
original Anchor guidance informed by the sources in the provenance registry below; it is not an
imported checklist. An overlay contributes result and evidence expectations only after its
structured predicate is met. It grants no effect, changes no authority, selects no native skill,
and establishes no external-framework status.

Repository authority and executable checks outrank this reference. Applicable law, contract,
explicit owner constraints, effect controls, and safety gates remain authoritative and
stricter-wins. A reviewed repository exception may strengthen an expectation or explain why a
control does not concern a change, but it cannot weaken effect safety, evidence truthfulness, or
an explicit owner requirement.

## Applicability contract

Selection uses normalized structured facts only: work type, execution mode, assurance tier,
domains, traits, already-authorized declared effects, quality attributes, reviewed changed-path
classes, and repository capabilities. Free text, filenames alone, agent identity, and skill
selection are not predicate inputs. A path class must derive from normalized repository-relative
paths plus inspected file type or content signals; suffix alone cannot activate a high-risk
overlay. Selection never authorizes an effect.

Within a predicate, `ANY(field,{values})` means that the field's normalized set intersects the
listed set. `AND` and `OR` have their ordinary Boolean meanings. A selected overlay contributes
all three controls. Positive reasons use `domain:<value>`, `trait:<value>`, `effect:<value>`,
`path_class:<value>`, and `tier:<value>`; false predicates emit `predicate_not_met` and no
controls. Ordering is lexically stable.

### `security.application/1.0.0`

**Predicate:** `ANY(domains,{application,web,api,identity}) OR
ANY(traits,{handles_secrets,handles_personal_data,authentication,authorization,untrusted_input,network_exposed,cryptography})
OR ANY(declared_effects,{external_publish,permission_change,secret_change}) OR
ANY(changed_path_classes,{auth_boundary,security_policy,request_handler,secret_configuration})`

**Sources:** `nist.ssdf`, `owasp.asvs`

- `SEC-INPUT-01/1.0.0` — Untrusted input is constrained at its trust boundary and failure
  behavior is tested.
- `SEC-AUTH-01/1.0.0` — Identity and authorization decisions deny unintended access in retained
  positive and negative tests.
- `SEC-SECRET-01/1.0.0` — Secrets are neither embedded nor exposed and changed secret handling
  has a revocation-safe path.

### `data.quality/1.0.0`

**Predicate:** `ANY(domains,{data,analytics}) OR
ANY(traits,{data_contract,data_transform,data_join,data_write,schema_change,personal_data}) OR
ANY(declared_effects,{data_mutation,schema_mutation}) OR
ANY(changed_path_classes,{schema,model,migration,pipeline,quality_rule})`

**Sources:** `iso.25012`, `uk.data-quality`

- `DAT-GRAIN-01/1.0.0` — The output grain and keys are explicit and checked for null or
  duplicate violations.
- `DAT-VALID-01/1.0.0` — Critical values are checked against owned validity, completeness,
  consistency, and timeliness rules.
- `DAT-TRACE-01/1.0.0` — Material transformations and source-to-output count differences are
  attributable and unexplained loss is surfaced.

### `ai.agent-risk/1.0.0`

**Predicate:** `ANY(domains,{ai,ml,agent}) OR
ANY(traits,{model_inference,prompt_or_context_change,tool_calling,retrieval,autonomous_action,human_facing_ai_output,sensitive_model_input})
OR ANY(changed_path_classes,{prompt,model_config,agent_policy,tool_boundary,retrieval_policy})`

**Sources:** `nist.ai-rmf`, `owasp.genai`

- `AIR-BOUNDARY-01/1.0.0` — Model-controlled content is treated as untrusted and cannot expand
  tool authority or bypass policy.
- `AIR-EVAL-01/1.0.0` — Material behavior changes have retained task-representative evaluations,
  including misuse or failure cases.
- `AIR-HUMAN-01/1.0.0` — Consequential outputs expose limitations and retain the required human
  decision or safe-stop boundary.

### `accessibility.user-interface/1.0.0`

**Predicate:** `ANY(domains,{frontend,user_interface,documentation}) AND
(ANY(traits,{user_facing_interaction,visual_content,audio_video,document_export}) OR
ANY(changed_path_classes,{ui_component,style,template,user_document}))`

**Sources:** `w3c.wcag`

- `A11Y-OPERATE-01/1.0.0` — Changed user interactions are operable by keyboard with visible
  focus and no keyboard trap.
- `A11Y-PERCEIVE-01/1.0.0` — Changed non-text content, structure, status, and contrast have
  programmatic or textual equivalents appropriate to the medium.
- `A11Y-ROBUST-01/1.0.0` — Changed interfaces retain meaningful names, roles, values, labels,
  and error identification under supported assistive technology checks.

### `architecture.change/1.0.0`

**Predicate:** `ANY(traits,{new_component,component_boundary_change,public_contract_change,persistence_change,deployment_topology_change,cross_system_dependency})
OR ANY(declared_effects,{schema_mutation,infrastructure_mutation}) OR
ANY(changed_path_classes,{architecture_description,public_api,infrastructure,migration})`

**Sources:** `c4.model`, `arc42.template`, `adr.nygard`

- `ARC-CONTEXT-01/1.0.0` — Affected people, external systems, trust boundaries, and
  system/component responsibilities are represented at the smallest useful level.
- `ARC-DECISION-01/1.0.0` — A consequential or hard-to-reverse design choice records context,
  decision, alternatives, consequences, and status.
- `ARC-CONSIST-01/1.0.0` — Architecture descriptions, accepted decisions, interfaces, and
  implementation do not materially contradict one another.

An architecture-description path selects this overlay only when the task changes or repairs an
authoritative architecture description. Incidental architecture vocabulary is not a positive
fact.

### `operations.reliability/1.0.0`

**Predicate:** `ANY(domains,{operations,reliability}) OR
ANY(traits,{production_runtime,scheduled_work,service_level,operational_dependency,on_call_change,capacity_change})
OR ANY(declared_effects,{deployment,infrastructure_mutation,production_configuration}) OR
ANY(changed_path_classes,{deployment,observability,runbook,scheduler,service_configuration})`

**Sources:** `google.sre`

- `OPS-SLI-01/1.0.0` — A user-relevant success signal and failure condition exist for the
  changed production behavior.
- `OPS-FAIL-01/1.0.0` — Expected failure modes have bounded impact, actionable observation, and
  a tested recovery or safe degradation path.
- `OPS-RUN-01/1.0.0` — Operator-facing steps name prerequisites, expected result, escalation,
  verification, and rollback.

### `supply-chain.software/1.0.0`

**Predicate:** `ANY(traits,{dependency_change,build_change,release_change,artifact_production,third_party_action})
OR ANY(declared_effects,{external_publish,release}) OR
ANY(changed_path_classes,{dependency_manifest,lockfile,build_pipeline,release_pipeline,artifact_manifest})`

**Sources:** `nist.ssdf`, `slsa`

- `SUP-PROV-01/1.0.0` — Released artifacts are traceable to a specific source revision and
  declared build process.
- `SUP-DEP-01/1.0.0` — Added or changed dependencies have reviewed origin, version, license,
  integrity, compatibility, and known-vulnerability evidence.
- `SUP-BUILD-01/1.0.0` — The build does not silently consume undeclared mutable inputs and
  retained metadata identifies material dependencies.

### `change-safety.high-risk/1.0.0`

**Predicate:** `assurance_tier = T3 AND
(ANY(traits,{safety_critical,regulated_process,irreversible_change,hazardous_process,large_blast_radius,control_logic_change})
OR ANY(declared_effects,{production_data_destructive,schema_destructive,permission_change,infrastructure_mutation}))`

**Sources:** `iec.fmea`, `iec.hazop`, `osha.moc`

- `HRC-CHANGE-01/1.0.0` — The proposed change identifies owner, reason, affected baseline,
  prerequisites, authorization boundary, communication, verification, and restoration path.
- `HRC-FAILURE-01/1.0.0` — Credible failure modes identify local effect, wider consequence,
  existing controls, detection, and an owned treatment without converting ordinal judgments
  into false precision.
- `HRC-DEVIATION-01/1.0.0` — For hazardous process or control-logic changes, a qualified review
  considers deviations from intended parameters, causes, consequences, safeguards, and actions
  before execution.

Selection requires both `tier:T3` and at least one consequence trait or declared consequence
effect; both reasons remain visible.

## Result evidence

Every applicable control has exactly one assessment state:

| State | Meaning | Shadow projection |
| --- | --- | --- |
| `satisfied` | Every required result is present, current, scoped to the change, and passing. | pass |
| `failed` | A required result ran and shows that the control outcome is not met. | finding |
| `unavailable` | A required tool, host, permission, fixture, or result is absent, stale, unsupported, or scoped elsewhere. | open uncertainty |
| `not_assessed` | Applicable evidence has not yet been attempted. | open obligation |
| `not_applicable` | The predicate is false, or a reviewed structured decision proves that the control does not concern the change. | no obligation |
| `excepted` | A governed exception identifies control/version, rationale, owner, scope, expiry, and compensating evidence. | visible open risk |

Invocation, elapsed time, an unparsed zero exit status, prose assertions, and loading this
reference are not result evidence. An evidence record identifies producer, result kind,
target/scope, capture time, tool or source version, immutable digest, status, and limitations.
Scope-mismatched or stale evidence is `unavailable`, never `satisfied`. Requirements are
conjunctive unless a catalog version explicitly names typed, semantically equivalent
alternatives. Aggregate counts are presentation only; each failure stays visible.

### Evidence required by control family

- `SEC-*`: scoped threat/trust-boundary review and executable negative tests or a relevant
  scanner result; `SEC-SECRET-01` also requires secret-leak inspection.
- `DAT-*`: declared contract and grain plus result-backed schema, key, business-rule, freshness,
  or reconciliation checks as applicable. Profiling cannot establish an owned business rule.
- `AIR-*`: authority/data-flow review plus retained evaluation cases and observed outcomes.
  Policy prose and provider assertions are insufficient.
- `A11Y-*`: an automated accessibility result plus retained manual keyboard and semantic checks
  for the changed interaction. Automation alone cannot satisfy all three controls.
- `ARC-*`: inspected architecture or decision artifact plus implementation/interface consistency
  review. Diagram existence alone is insufficient.
- `OPS-*`: observed test, simulation, or telemetry result and inspected runbook or rollback
  evidence as applicable. An unexercised procedure is `not_assessed`.
- `SUP-*`: dependency/build metadata, integrity/provenance result, and vulnerability/license
  review scoped to changed inputs. Package-manager success alone is insufficient.
- `HRC-*`: approved change record, multidisciplinary hazard review where applicable, and retained
  precondition, verification, and rollback evidence. Tool-generated risk numbers are
  insufficient.

## Source, license, and refresh provenance

`Baseline` identifies material used to derive original Anchor wording, not a target status. URLs are
provenance and refresh locations; selection performs no runtime network access. Every source was
captured for this authored-reference version on 2026-08-20. No paid source snapshot or restricted
source body is distributed.

| Source ID | Baseline and provenance URL | License and use boundary | Capture date | Refresh trigger |
| --- | --- | --- | --- | --- |
| `nist.ssdf` | NIST SP 800-218, SSDF v1.1, February 2022 — https://csrc.nist.gov/pubs/sp/800/218/final | US Government work; cite NIST and use concepts only. | 2026-08-20 | NIST publishes a revision, errata, or successor. |
| `owasp.asvs` | OWASP ASVS 5.0.0, May 2025 — https://owasp.org/www-project-application-security-verification-standard/ | Upstream CC BY-SA 4.0; preserve attribution while keeping Anchor wording original. | 2026-08-20 | The adopted ASVS major/minor version or license changes. |
| `iso.25012` | ISO/IEC 25012:2008, Data quality model — https://www.iso.org/standard/35736.html | Copyright ISO; retain bibliographic metadata and public concepts only, with no clauses or tables. | 2026-08-20 | ISO publishes an amendment, revision, or withdrawal. |
| `uk.data-quality` | UK Government Data Quality Framework, December 2020 — https://www.gov.uk/government/publications/the-government-data-quality-framework | Open Government Licence v3.0; attribute the source. | 2026-08-20 | GOV.UK marks it updated or publishes a successor. |
| `nist.ai-rmf` | NIST AI 100-1, AI RMF 1.0, January 2023, and NIST AI 600-1 Generative AI Profile, July 2024 — https://www.nist.gov/itl/ai-risk-management-framework | US Government works; cite NIST. | 2026-08-20 | NIST revises the RMF/profile or publishes applicable agent guidance. |
| `owasp.genai` | OWASP Top 10 for LLM Applications 2025 — https://genai.owasp.org/llm-top-10/ | Upstream CC BY-SA 4.0; original Anchor synthesis with attribution. | 2026-08-20 | OWASP publishes a new adopted list/version or changes scope/license. |
| `w3c.wcag` | WCAG 2.2, W3C Recommendation, 5 October 2023 — https://www.w3.org/TR/WCAG22/ | W3C Document License; link and paraphrase without republishing normative text. | 2026-08-20 | The Recommendation, errata, techniques, or supported-platform policy changes. |
| `c4.model` | C4 model website baseline captured 2026-08-20 — https://c4model.com/ | Copyright Simon Brown; site content CC BY 4.0. Attribute and use concepts only. | 2026-08-20 | Site version/license or adopted notation guidance changes. |
| `arc42.template` | arc42 template v8.2 — https://arc42.org/ | CC BY-SA 4.0; attribute without importing the template wholesale. | 2026-08-20 | The adopted arc42 release or license changes. |
| `adr.nygard` | Michael Nygard, “Documenting Architecture Decisions,” 2011 — https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions | Copyright source article; use the ADR concept with original Anchor template wording only. | 2026-08-20 | The canonical source changes or the project adopts another ADR contract. |
| `google.sre` | Google SRE Books online baseline captured 2026-08-20 — https://sre.google/books/ | Book text CC BY-NC-ND 4.0; link and use general concepts without copying or adapting text. | 2026-08-20 | License, canonical guidance, or adopted reliability policy changes. |
| `slsa` | SLSA specification v1.1 — https://slsa.dev/spec/v1.1/ | Community Specification License 1.0; cite and use provenance concepts without external-status claims. | 2026-08-20 | The adopted SLSA release, track model, or license changes. |
| `iec.fmea` | IEC 60812:2018, FMEA/FMECA — https://webstore.iec.ch/en/publication/26359 | Copyright IEC; metadata and general method concepts only, with no tables, scales, or clauses. | 2026-08-20 | IEC publishes an amendment, revision, or withdrawal. |
| `iec.hazop` | IEC 61882:2016, HAZOP studies — https://webstore.iec.ch/en/publication/24321 | Copyright IEC; metadata and general method concepts only, with no guide-word tables or clauses. | 2026-08-20 | IEC publishes an amendment, revision, or withdrawal. |
| `osha.moc` | OSHA 29 CFR 1910.119(l), Management of change — https://www.osha.gov/laws-regs/regulations/standardnumber/1910/1910.119 | US Government regulation; quote only when legally necessary and separately reviewed. This overlay is not legal advice. | 2026-08-20 | The regulation, an official interpretation, or the governing jurisdiction changes. |

A refresh candidate requires source review, a catalog-version decision, applicable tests, and
ordinary release review. Learning does not edit this reference or the catalog automatically.

## Limitations and safe use

- The catalog is a bounded assurance aid, not an audit opinion, accreditation, legal
  determination, or complete framework assessment.
- Overlay selection does not prove that evidence exists, that a scanner or evaluator is complete,
  or that a system is safe. Open evidence stays open.
- The high-risk overlay prompts qualified multidisciplinary review; Anchor does not perform a
  statutory process-safety study or authorize a hazardous change.
- The accessibility overlay requires human checks where automation cannot observe the outcome.
- Security and AI overlays remain catalog/reference concerns; no native skill gains generic
  security or AI ownership from this reference.
- Existing skills own procedures. This reference states applicable outcomes and result evidence
  without replacing data, dependency, documentation, incident, testing, or review workflows.
- New controls, changed predicates, or changed source baselines require a versioned Spec and
  review rather than an ad hoc edit.

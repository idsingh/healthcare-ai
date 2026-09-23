# EOC Benefit Extraction Service — Design

Converts unstructured / semi-structured plan text (`extracted_text.txt`) into
schema-conformant JSON with per-field evidence anchoring.

Deliverables in this repo:

| File | What it is |
|---|---|
| `DESIGN.md` | This document: assumptions, architecture, components, failure modes |
| `schema/extraction.schema.json` | Output contract (JSON Schema 2020-12) |
| `sample_output.json` | Filled output for the provided excerpt |
| `pseudocode.py` | Core extraction logic (deterministic pass + LLM pass + merge) |
| `tools/validate.py` | Runnable validator: groundedness + business rules |

---

## 1. Assumptions

Stated explicitly because most of them are load-bearing.

**About the input**

1. Input is UTF-8 plain text extracted from a PDF. Layout is *lossy*: the source is a
   two-column benefits table (`benefit description` | `what you must pay`) that has been
   flattened into reading order. Column content is therefore **interleaved** — in the
   excerpt, `The plan will pay up to $500 ... each year` appears in the middle of the
   X-ray code list. Any design that assumes linear reading order will mis-attribute fields.
2. Line breaks are arbitrary (mid-phrase, mid-word), whitespace is irregular, bullets are
   `•`, dashes are en-dashes (`–`), and the same logical item can span a page boundary
   (package 1's exclusion list continues onto page 116).
3. Every page repeats a header/footer block (`115 2024 Evidence of Coverage for ...`,
   `HMO-MAPD 1054638MUSENMUB_0102_R ...`). This is boilerplate noise, detectable by
   cross-page repetition rather than by hardcoded regex.
4. The excerpt is a **fragment**: it starts mid-sentence and ends mid-sentence
   (`... oral surgery dental`). The service must produce partial output plus an explicit
   truncation flag, never a guess at the missing tail.
5. Text is the primary input; the PDF is reference only. No OCR step in scope, but the
   ingest port is written so a `PdfTextSource` can be swapped in later.

**About the domain**

6. A document contains 0..N *packages*; each package has at most one premium, at most one
   benefit maximum, and 0..N services / cost-share rules / exclusions.
7. `Dxxxx` tokens are CDT procedure codes. We validate their *shape* only — we do not
   assert that a code exists in the CDT catalog unless a licensed code list is mounted
   (see §6, "code registry"). The excerpt's code→description mapping is taken from the
   document, not from external knowledge.
8. Amounts are USD. Cadence vocabulary is closed (`monthly`, `annual`, `per_visit`, ...).
9. Absence is represented as `status: "not_stated"` with `value: null`. The service never
   infers an unstated value, and never fills from prior plan knowledge.

**About operations**

10. Documents range from a 2-page excerpt to a 300+ page EOC; throughput target is batch
    (thousands of documents), not interactive. p95 per document is a budget, not an SLA.
11. LLM is `gpt-5.6-luna` via OpenRouter, configured as an opaque model id behind an
    `LLMClient` port. I have not independently verified that this router model supports
    structured outputs / seeding, so the client declares a **capability contract** and
    degrades gracefully (§4.5) rather than assuming them.
12. EOC text is a public plan document, not PHI. But the same pipeline will inevitably be
    pointed at member correspondence, so it is built PHI-ready: BAA-covered inference
    endpoints, no-train flags, prompt/response redaction before logging, and a kill switch
    that routes to a self-hosted model. This is a deployment posture, not extra code paths.

---

## 2. Architecture

```
                    ┌──────────────── Control plane ────────────────┐
                    │  idempotency store · run ledger · eval gate   │
                    └───────────────────────────────────────────────┘
                                        │
 ingest        normalize        segment        extract           reconcile      validate     emit
┌───────┐     ┌─────────┐     ┌─────────┐   ┌──────────────┐   ┌──────────┐   ┌────────┐  ┌──────┐
│ blob  │ ──▶ │ NFKC    │ ──▶ │ page    │──▶│ A. determin. │──▶│ merge by │──▶│ schema │─▶│ JSON │
│ +sha  │     │ dash    │     │ deboiler│   │    scanners  │   │ field w/ │   │ rules  │  │ +env │
│       │     │ dehyph  │     │ column  │   │ B. LLM pass  │   │ priority │   │ ground │  │ elope│
│       │     │ offsets │     │ de-int. │   │  (per block) │   │          │   │ -edness│  │      │
└───────┘     └─────────┘     └─────────┘   └──────────────┘   └──────────┘   └────────┘  └──────┘
                    │              │                │                              │
                    └──────────────┴────────────────┴──────────────────────────────┘
                                   emits spans into the *original* char offset space
```

Two properties hold end to end:

- **Offset preservation.** Normalization produces `(normalized_text, offset_map)`. Every
  candidate carries a span in normalized space that maps back to original-file offsets, so
  evidence is verifiable against the file the customer gave us, not a cleaned derivative.
- **Everything is a candidate until validated.** Deterministic scanners and the LLM both
  emit `Candidate(field_path, value, span, source, confidence)`. Nothing writes directly
  into the output object. Merge and validation are the only writers.

### Processing model

- A document becomes a **DAG of block-scoped tasks** (one per package section). Blocks are
  independent → embarrassingly parallel, bounded by a worker pool and a token-bucket rate
  limiter on the LLM provider.
- Document-level fields (plan name, year) come from a single cheap header task.
- Large documents never enter a single prompt. Map (per block) → reduce (assemble
  packages) → document-level cross-checks. Context is bounded by construction, so a 300-page
  EOC costs linearly, not quadratically.
- Each block task is independently cached and retried (§5).

---

## 3. Components and responsibilities

| # | Component | Responsibility | Never does |
|---|---|---|---|
| 1 | `DocumentSource` | Fetch bytes, compute `content_sha256`, decode | Interpret content |
| 2 | `TextNormalizer` | NFKC, dash/ligature folding, de-hyphenation, whitespace collapse, **offset map** | Delete content |
| 3 | `Segmenter` | Page split, boilerplate detection (cross-page repetition), column de-interleaving, section detection, bullet re-joining | Extract values |
| 4 | `CandidateExtractor` (strategy) | Emit typed candidates with spans. Impls: `MoneyScanner`, `CadenceScanner`, `CdtCodeScanner`, `FrequencyScanner`, `CostShareScanner`, `NetworkScanner`, `LlmExtractor` | Decide the winner |
| 5 | `Reconciler` | Merge candidates per field by source priority + agreement; mark `ambiguous` on unresolved conflict | Invent values |
| 6 | `ValidationPipeline` | Composite of `ValidationRule`s: schema, groundedness, business invariants, cross-field | Mutate values (only nulls + flags) |
| 7 | `LLMClient` (port) | Provider-agnostic call: structured output, retries, timeouts, cost accounting. Adapter: `OpenRouterClient(model="gpt-5.6-luna")` | Know about dental |
| 8 | `PromptRegistry` | Versioned, hashed prompt templates; the hash is part of the cache key | Hold business rules |
| 9 | `ExtractionRepository` | Idempotent persistence of run + result + flags | Retry logic |
| 10 | `DomainPack` (registry) | The only dental-specific unit: schema fragment, lexicon, scanners, prompt fragment, validators | Touch the pipeline |
| 11 | `Telemetry` | OTel traces/metrics/logs, redaction | Business decisions |
| 12 | `EvalHarness` | Golden-set scoring, CI gate, drift alarms | Run in the request path |

**Generalizability.** Dental is a `DomainPack`. Vision, hearing, OTC, transportation are new
packs registered at startup. The pipeline, schema envelope, evidence model, validators and
observability are domain-agnostic. Adding a benefit domain = add a pack + goldens, touch no
pipeline code.

---

## 4. Deterministic vs LLM-powered

Rule of thumb: **regex finds tokens, the LLM decides what they mean.** Anything with a
closed vocabulary or a stable surface form is deterministic; anything requiring scope,
attachment or paraphrase is LLM. Numbers are *always* deterministic — the LLM may select a
number's meaning but never re-type the digits.

| Concern | Mechanism | Why |
|---|---|---|
| Hashing, idempotency, caching | Deterministic | Pure functions |
| Unicode/whitespace/de-hyphenation, offset map | Deterministic | Reversible, testable |
| Page split, boilerplate removal | Deterministic (frequency ≥ k across pages) | No semantics needed |
| Package boundary detection | Deterministic anchor `^Optional supplemental package\s+(\d+)` + LLM fallback when 0 anchors hit | Anchored headings are reliable; fallback handles reworded plans |
| Money amounts, percentages | Deterministic (`\$\s?[\d,]+(?:\.\d{2})?`, `\d{1,3}\s?%`) | LLM must never re-type digits |
| CDT codes | Deterministic (`\bD\d{4}\b`) | Fixed shape |
| Code → description pairing | Deterministic (bullet split on dash) + LLM repair for wrapped/split bullets | 95% is pure string work |
| **Column de-interleaving** | Heuristic first, LLM adjudication on low confidence | Genuinely ambiguous once layout is gone |
| **Which package a cost-share belongs to** | LLM, constrained to the block | Attachment is semantic |
| Service + limit normalization ("Two oral exams each year" → `{service, limit:{count:2, period:"year"}}`) | LLM, validated against deterministic numeral scan | Paraphrase with numeric checks |
| Exclusion normalization | LLM | Free prose |
| Network restriction | Deterministic lexicon (`LIBERTY Dental`, `contracted provider`) + LLM for novel phrasings | Hybrid |
| Evidence spans | Deterministic verification (substring + offset) of LLM-quoted text | The anti-hallucination backstop |
| Validation & contradiction checks | Deterministic | Must be auditable |

### 4.5 Prompt strategy and determinism

- **One block, one job.** Each call sees a single package block (plus its column-split
  neighbours), never the whole document. Smaller context = fewer attachment errors.
- **Structured output.** Response is constrained by the JSON Schema fragment for that block.
  If the provider/model can't enforce a schema, fall back to: JSON-only instruction →
  `json.loads` → schema validate → one repair round-trip with the validator error → fail.
  The pipeline tolerates a model without native structured output; it never trusts free text.
- **Quote-only fields.** Every extracted field must carry `evidence_quote` copied
  *verbatim* from the input. Post-hoc we assert the quote is a substring of the normalized
  block. Ungrounded → the field is dropped to `null` with `status:"unverified"`. This single
  rule converts most hallucinations into recorded nulls.
- **Explicit abstention.** The prompt enumerates target fields and instructs
  `"not_stated"` for anything absent, with examples of correct abstention. Nulls are a
  success mode, not a failure.
- **Determinism controls.** `temperature=0`, `top_p=1`, fixed `seed` when supported, pinned
  model id (never a floating alias), pinned prompt version. These make results *reproducible
  in practice*; they do not make a sampled model mathematically deterministic, so the cache
  (§5) is what guarantees byte-identical repeat output.
- **Self-consistency, only where it pays.** For fields the reconciler marks ambiguous,
  re-run k=3 at temperature 0.3 and take the majority with the agreeing evidence span;
  no majority → `ambiguous`. Applied to <5% of fields, so cost stays flat.
- **No chain-of-thought in the payload.** Reasoning, if requested, goes in a separate
  discarded field so it can't leak into values.

---

## 5. Reliability: idempotency, retries, concurrency

**Idempotency key** (per block):

```
key = sha256(content_sha256 | block_id | domain_pack_v | schema_v | prompt_v | model_id | params)
```

Any input change busts the cache; no input change replays the cached JSON byte-for-byte.
Document-level key is the same tuple minus `block_id`, so re-submitting a document is a
no-op returning the prior `run_id`. Result emission uses a transactional **outbox**, so a
crash between "persisted" and "published" cannot double-publish.

**Retries** — layered, each with a distinct trigger:

| Layer | Trigger | Action | Budget |
|---|---|---|---|
| Transport | 429/5xx/timeout | Exponential backoff + full jitter | 5 attempts |
| Parse | Non-JSON / schema-invalid | One repair prompt carrying the validator error | 1 |
| Semantic | Groundedness failure on a field | Re-ask for that field alone with a narrowed window | 1 |
| Provider | Circuit open / model unavailable | Fail over to secondary model id, tag `degraded:true` | 1 |
| Document | Any block permanently failed | Emit partial result + `blocked_blocks[]`, route to review queue | — |

Retries are safe because every attempt is a pure function of (block, prompt, params) and
writes go through the idempotency key.

**Concurrency** — bounded worker pool over block tasks; token-bucket rate limiter and a
concurrent-token budget in front of the provider; per-document semaphore so one huge
document can't starve the queue; circuit breaker per provider; backpressure to the queue
rather than unbounded in-memory fan-out. Cost ceiling per document — exceeding it stops
the LLM pass and emits deterministic-only output flagged `partial_reason:"cost_cap"`.

---

## 6. Failure modes and mitigations

**Input / layout**

| Failure | Detection | Mitigation |
|---|---|---|
| Column interleaving mis-attributes a value (e.g. `$500` lands in package 2) | Cross-field check: benefit max must appear within its package's span | De-interleave before extraction; block-scoped prompts; conflict → `ambiguous` + review |
| Item split across a page boundary | Boilerplate strip leaves a bullet continuing after a page break | Join across page boundaries when the next line starts lowercase or the bullet has no terminator |
| Truncated document (this excerpt) | Last block has no terminator / trailing sentence incomplete | `document.truncated=true`, affected fields `status:"truncated"`, never extrapolate |
| Garbage/empty/wrong-language text | Heuristics: printable ratio, dictionary hit rate, anchor count = 0 | Reject before spending tokens; `rejected_reason` |
| Reworded headings in another plan | Zero anchors but plausible text | LLM segmentation fallback, lower confidence, sample into review |

**Model**

| Failure | Detection | Mitigation |
|---|---|---|
| Hallucinated value (a `$1,200` max that isn't in the text) | Groundedness: evidence quote not a substring | Drop to null + `unverified`; count into `hallucination_rate` metric |
| Plausible-but-wrong attachment (cost share on the wrong package) | Span must fall inside the owning block | Reject candidate; re-ask narrowed |
| Number drift (`$13.00` → `$13`) | Deterministic money scan disagrees | Deterministic value wins; log disagreement |
| Invalid/absent JSON | Schema validate | Repair round-trip, then fail the block |
| Provider outage, rate limit, latency spike | Error rate / p95 monitors, circuit breaker | Backoff, failover model, degrade to deterministic-only |
| Silent model change behind an alias | Pinned id + prompt hash + nightly golden-set run | Version gate: eval must pass before a new model id is promoted |
| Prompt injection from document text ("ignore previous instructions") | Input is data, never instructions; injection-probe goldens | Delimited input block, output constrained by schema, quotes verified against source |

**System**

| Failure | Detection | Mitigation |
|---|---|---|
| Duplicate processing / double emit | Idempotency key + outbox | Replay cached result |
| Cost blowout on a huge document | Per-document token/cost budget | Cap → partial output, flagged |
| Schema evolution breaks consumers | `schema_version` in every envelope | Additive-only changes; versioned contract tests |
| Silent quality regression | Golden set in CI + production null-rate/groundedness drift alarms | Block release; alert on drift |

---

## 7. Validation checks (Part C — at least 3; here are the ones that earn their place)

Implemented in `tools/validate.py`, run against `sample_output.json`.

1. **Evidence groundedness (anti-hallucination).** Every non-null field's `evidence.quote`
   must be an exact substring of the normalized source, and its `[start,end)` offsets must
   resolve to that quote. Fails → field nulled, `status:"unverified"`. *This is the single
   highest-value check; it makes fabrication structurally detectable.*
2. **Numeric fidelity.** Every `amount_usd` / `percent` in the output must appear as a
   literal in its own evidence quote (after normalization). Catches digit drift and
   unit/cadence swaps.
3. **Completeness & contradiction invariants.** Per package: exactly ≤1 premium and ≤1
   benefit maximum; a missing premium on a package whose heading says "additional premium"
   is an error, not a null; two different premium values inside one package span is a
   contradiction; coinsurance percentages must be 0–100; a `copay: $0` co-existing with a
   coinsurance statement for the *same* service set is a contradiction (different service
   sets, as in package 2, is legal).
4. **Referential integrity.** Every `code` matches `^D\d{4}$`; every code referenced by a
   service exists in that package's `codes` list; when a licensed CDT registry is mounted,
   unknown codes are flagged `code_not_in_registry` (warn, never auto-correct).
5. **Ambiguity surfacing.** Any field with `status` in `{ambiguous, unverified, truncated}`
   or `confidence < 0.7` sets `needs_review=true` on the document. Downstream analytics
   filters on this; humans review only the flagged minority.

Severity model: `error` → field nulled and document flagged; `warn` → document flagged;
`info` → metric only. Validation output is part of the envelope, not a side log.

---

## 8. Observability and evaluation

**Traces.** One span per stage per block: `normalize → segment → extract.deterministic →
extract.llm → reconcile → validate`. Span attributes: `doc_id`, `run_id`, `block_id`,
`model_id`, `prompt_v`, `tokens_in/out`, `cost_usd`, `cache_hit`, `attempt`.

**Metrics** (the five that actually drive action):

1. `groundedness_failure_rate` — proxy for hallucination. Alarm on any sustained rise.
2. `field_null_rate` by field path — a spike means upstream format drift, not model decay.
3. `needs_review_rate` — the human cost of the system.
4. `llm_cost_per_document` + `tokens_per_document` — budget guardrail.
5. `block_failure_rate` / retry counts by cause — provider health.

**Logs.** Structured, correlated by `run_id`; prompts and completions stored in a separate
short-retention, access-controlled store with redaction, so a bad extraction can be
replayed exactly.

**Evaluation.**

- *Golden set*: ~50 hand-labelled excerpts across plans/carriers/benefit domains, including
  the nasty ones — interleaved columns, page-split items, truncation, missing premium,
  injection probes.
- *Metrics*: field-level precision / recall / exact-match, evidence-span IoU, hallucination
  rate (values with no support), abstention accuracy (did it correctly say "not stated"?).
  Abstention is scored explicitly — a model that never says null looks great on recall and
  is useless in production.
- *Gate*: CI fails on any regression beyond tolerance; model or prompt version bumps are
  promoted only through the gate.
- *Production*: shadow-run the candidate version on 1% of live traffic, diff against
  incumbent, review disagreements. Sample 2% of unflagged outputs for human audit to
  estimate the false-confidence rate.

---

## 9. SOLID / DRY in this design

- **SRP** — each component in §3 has one reason to change: `TextNormalizer` changes when
  encoding quirks change, `DomainPack` when benefits change, `LLMClient` when providers change.
- **OCP** — new benefit domains and new scanners are registered, not wired in.
  `CandidateExtractor` and `ValidationRule` are the two extension points; the pipeline is closed.
- **LSP** — every extractor, deterministic or LLM, returns `list[Candidate]` with the same
  span contract, so the reconciler cannot tell them apart except by `source` priority.
- **ISP** — narrow ports (`DocumentSource`, `LLMClient`, `ExtractionRepository`); nothing
  depends on a fat "service" interface.
- **DIP** — the pipeline depends on ports; OpenRouter/S3/Postgres are adapters chosen at
  composition root. Swapping `gpt-5.6-luna` for a self-hosted model is a config change.
- **DRY** — one normalizer (evidence verification reuses the exact same function as
  ingestion, so "grounded" means the same thing everywhere), one `Field<T>` envelope used by
  every field, one validation pipeline shared by runtime and the eval harness. The golden-set
  scorer calls the same validators production does.

Deliberate non-DRY: scanners stay small and duplicated rather than merged into one clever
mega-regex. Regex generality is where extraction quality quietly dies.

---

## 10. Running what's here

```bash
python3 tools/validate.py                 # validate sample_output.json against the source
python3 tools/validate.py --fix-offsets   # re-resolve every evidence span, then validate
pip install jsonschema && python3 -c "import json,jsonschema; \
  jsonschema.Draft202012Validator(json.load(open('schema/extraction.schema.json'))) \
  .validate(json.load(open('sample_output.json')))"
```

Actual output on the provided excerpt:

```
fields checked : 50  grounded: 49  null: 1
groundedness   : 100.00%
errors         : 0
warnings       : 4
  [warn] package.exclusions_not_stated: no exclusions section observed for pkg_2 ...
  [warn] review.low_certainty: status=truncated confidence=0.3 (/packages/1/services/1)
  [warn] review.low_certainty: status=truncated confidence=0.6 (/packages/1/cost_share/2)
  [warn] document.truncated: source ends mid-sentence (/document/truncated)
RESULT: PASS | needs_review: True
```

All 49 evidence quotes resolve to verified character offsets in the normalized source; the
one null (`document.issuer`) is a correct abstention — the carrier is not named separately
from the plan name.

**Negative test** — inject a fabricated `$1,200` benefit maximum with a matching fake quote,
and a `D9999` code that isn't in the document:

```
[error] groundedness.not_in_source: evidence not found in source: 'The plan will pay up to $1,200 ...'
[error] service.unknown_code: D9999 referenced by pkg_1.svc_oral_exams is not in the package code list
[error] review.flag_missing: pkg_1 has low-certainty fields but needs_review is false
RESULT: FAIL   (exit code 1)
```

The fabricated value is nulled, the field is marked `unverified`, and the document is blocked
from the clean path. That is the whole anti-hallucination thesis in one test: *a value without
verifiable evidence is not a value.*

`pseudocode.py` is illustrative — helpers like `offset()`, `group_by()` and the `Deps`
container are declared, not implemented; the segmenter bodies are elided to `...`. It is the
logic and the seams, not a runnable service.

**What I'd build first if this were real** (2-week slice): normalizer + segmenter with the
column de-interleaver, deterministic scanners, single-block LLM pass with structured output,
the five validators, and a 20-document golden set wired into CI. Everything else in this
document — self-consistency, model failover, shadow evaluation — is the second increment.

---

## 11. The implemented service

`README.md` covers running it. The mapping from this document to code:

| Design section | Code |
|---|---|
| §2 pipeline | `app/application/pipeline.py` |
| §3 components | `app/domain/ports.py` + `app/application/*` + `app/adapters/*` |
| §4 deterministic vs LLM | `application/scanners.py` vs `application/llm_extractor.py`, reconciled in `merge.py` |
| §4.5 prompt strategy | `llm_extractor.SYSTEM_PROMPT`, `domain/contracts.py` (structured output) |
| §5 idempotency / retries / concurrency | `application/service.py`, `application/retry.py`, `pipeline._process_blocks` |
| §6 failure modes | `tests/test_llm_reliability.py` |
| §7 validation checks | `application/validation.py` (five rules) |
| §8 observability | `app/logging_setup.py`, run counters in `RunInfo` |

Built as specified; §8's metrics exporter and §5's outbox are the two pieces left as design
only, because this service keeps jobs in process.


---

## 12. Extension: Dental Guide PDFs to CSV

The second brief keeps the same architecture and adds one input adapter, one use case and one
projection. Nothing in the existing text path changed.

**Assumptions.** Guides have a text layer (no OCR in scope — a scanned file is rejected with a
clear reason, not silently empty). Benefit tables run across many pages and repeat only part
of their header. Codes follow the CDT shape. Column names, column order and column *set* vary
between carriers; the output column set does not.

**Components added**

| Component | Responsibility |
|---|---|
| `adapters/pdf/pdfplumber_source.py` | The only module that knows pdfplumber: pages as words with geometry, ruled tables, rectangles, text |
| `application/tables/ruled.py` | Read the grid the PDF draws |
| `application/tables/geometric.py` | Rebuild a table with no grid: code column anchors rows, whitespace defines columns, proximity binds wrapped lines, tall rectangles mark merged cells |
| `application/tables/cascade.py` | Run the readers, score them, keep the best; LLM reader last |
| `application/tables/mapping.py` | Header label → canonical field by vocabulary, with an LLM fallback for unknown labels |
| `application/dental_guide.py` | The use case: pages → rows → benefit groups → validation |
| `application/csv_export.py` | The customer's six columns, `-` for anything not stated |

**Deterministic vs LLM, unchanged in principle.** Structure, codes, descriptions, frequencies
and coverage are read from the table. The model names benefit groups when the guide has no
category column — the one genuinely semantic step — and its reply is schema-constrained and
checked against the codes it was given.

**Why this survives an unseen guide.** Three independent readers with a quality score instead
of one reader that must always win; column mapping by vocabulary rather than position; a
carried header so continuation pages keep working; per-document validation flags that say
*which* column was missing rather than emitting a silently empty CSV; and a test that fails the
build if any document-specific identifier appears in `app/`.

---

## 13. What happens when a guide has a layout we have never seen

The guarantee is not "every layout parses". It is: **either the rows come out right, or the
output says which column it could not find — never a confident wrong CSV.**

### Three ways to identify a column, cheapest first

1. **Header vocabulary.** Labels are matched by longest phrase, so a label carrying two
   vocabularies resolves to the more specific one: `Code Description` → description,
   `Benefit Limitations` → frequency, `Non-Participating Provider` → out-of-network.
2. **Cell contents.** For anything the header did not give us — or a header that contradicts
   its column — the column is identified by what it holds: CDT-shaped cells are the code
   column, money/percent cells are coverage (first = in-network, second = out-of-network),
   "per year / every 12 months" text is frequency, the longest free text is the description.
   Content beats wording: a column headed `Procedure` that holds D-codes is the code column.
3. **Carried mapping.** A continuation page that repeats none of its header inherits the
   mapping from the page that had one.

### Measured on layouts the code had never seen

Each row is a PDF generated in `tests/test_unseen_layouts.py`, not one of the supplied guides.

| Layout hazard | Result |
|---|---|
| Columns reordered, headers never seen (`Nomenclature`, `CDT Code`, `Participating Provider`) | All rows, all six columns |
| Dollar copays instead of percentages (`You Pay (In-Network)`) | All rows, all six columns |
| No header row at all | All rows, all six columns, identified from content |
| Landscape page, three columns we do not need | All rows; extra columns reported as unmapped |
| Unruled table, invented wording (`Procedure`, `What it covers`, `How often`, `In-plan`) | All rows, all six columns |
| Two tables with different shapes on one page | Each table mapped separately; no cross-contamination |
| Scanned page, no text layer | Refused: *"no extractable text layer (scanned image?); OCR is required"* |
| A booklet with no benefit table at all | Zero rows, `dental_guide.no_rows` error, job marked `partial` |

### Where it still fails, honestly

- **Scanned or image-only PDFs.** No OCR in this service. It refuses with a reason instead of
  emitting an empty CSV. Adding OCR is an adapter, not a redesign (see §14).
- **A layout no reader can segment** — a benefit "table" laid out as prose paragraphs, or
  rotated text. The LLM reader is the seam for this; it is wired into the cascade and fires
  only when every deterministic reader scores zero.
- **Semantics we cannot see.** If coverage lives in a footnote ("all preventive services are
  covered in full") rather than a column, the CSV says `-` and a flag says the column was not
  present. That is a deliberate choice: `-` is recoverable, a wrong 100% is not.

---

## 14. Why this rather than a document-AI service

Reasonable question, and the answer is "both, in different places". Three options, and what
each is actually good at:

| | This service (pdfplumber + geometry + narrow LLM) | Mistral Document AI | Azure AI Document Intelligence |
|---|---|---|---|
| What it does best | Digital-text benefit tables, mapped straight onto the customer's columns | Reading messy, scanned, handwritten documents into markdown/JSON | Layout + table extraction with cell geometry and confidence, plus custom models trained on labelled samples |
| Scanned pages | Refuses (no OCR) | Strong — this is the point of it | Strong |
| Determinism | Byte-identical for the same input | Model-dependent; re-runs can differ | Versioned models; stable in practice |
| Provenance | Page, strategy and column for every row | Document-level | Bounding boxes and confidences per cell |
| Cost at 100k pages/month | Compute only (~56 ms/page here: 75 pages, 782 rows in 4.2 s) | Per page, roughly $1 per 1k pages (verify current pricing) | Per page, roughly an order of magnitude more for layout, more again for custom models |
| PHI / data residency | Nothing leaves the process | Data leaves your boundary unless self-hosted | BAA and private networking available on Azure |
| Schema mapping | Built in — it knows what "Benefit Group" means | You still write it | You still write it (or train and maintain a custom model) |
| Failure mode | Flags the column it could not find | Can hallucinate plausible values | Low confidence scores you must act on |
| Effort | Weeks of engineering, ours to maintain | Hours to integrate | Days, plus labelling for custom models |

**Where I would actually spend money.** Neither service removes the work that matters here:
both hand back *a* table, and you still have to decide that `Periodicity` is the frequency
column, that a merged cell applies to five rows, and that a missing coverage column means `-`
and not `0%`. That mapping and its validation is the product; the PDF reader underneath is
replaceable.

So the design keeps the reader behind a port and treats a service as one more strategy in the
cascade:

- **Deterministic readers first** for the digital-text majority — free, ~56 ms/page,
  reproducible, auditable, no PHI leaving the process.
- **A fallback reader** for what they cannot read: scanned pages, rotated text, exotic
  layouts. That slot is filled by Docling running in-process (§15). A hosted service would
  implement the same `extract(page, carried)` contract and slot in beside it, scored the same
  way — the choice is per environment, not per codebase.
- **Our validation over whichever reader won**, because a confidence score from a vendor is
  not the same as "this code appears on this page and this column was the one labelled
  out-of-network".

The one case for going service-first is a corpus that is mostly scans, where OCR is the whole
problem and our geometry has nothing to work with. That is a business input, not a design
preference: it is measured by what share of incoming guides have a text layer.

---

## 15. The fallback reader: Docling

### Decision

**Use Docling, running in-process, as the fallback table reader. Do not call a hosted
document-AI service.**

Context: the deterministic readers handle digital-text benefit tables, which is every guide
supplied so far. Two things defeat them — a page with no text layer (a scan), and a layout the
geometry cannot segment. Something has to read those pages, or the CSV silently loses them.

The candidates were a hosted service (Mistral Document AI, Azure AI Document Intelligence) and
a local model stack (Docling). Both read scans; the difference is everything around that.

| | Docling (chosen) | Hosted document AI |
|---|---|---|
| Where the page goes | Stays in the process | Leaves the boundary — a member-facing plan document sent to a third party |
| Cost model | CPU time already paid for | Per page, forever, and it grows with volume |
| Reproducibility | Same container, same output | Model changes under you between runs |
| Compliance | No BAA needed, no data-residency question | BAA, vendor review, egress controls |
| Offline / air-gapped | Works with pre-downloaded weights | Impossible |
| Licence | MIT | Commercial terms |
| Cost of entry | Large dependency, model weights, seconds per page | An API key |
| Failure mode | Slow, or the install is missing | Rate limits, outages, bills |

Decision: **Docling**. In a healthcare context the deciding factor is not accuracy, it is that
nothing leaves the process and there is no per-page meter on a pipeline meant to run over
thousands of documents. The price is a heavyweight optional dependency and seconds-per-page
latency, which is acceptable precisely because the fallback is rare: on the three supplied
guides it is called zero times.

Consequences, and how they are contained:

- Docling is **not** in `requirements.txt`. It lives in `requirements-docling.txt` and is
  imported lazily; a missing install logs a warning and degrades to deterministic-only.
- It is **off by default** (`EXTRACT_DOCUMENT_AI_PROVIDER=none`). Turning it on is a
  deployment decision, not a code change.
- Model weights download on first run; `docling_artifacts_path` points at pre-downloaded
  weights for offline or air-gapped deployment.
- If a hosted service is ever wanted, it is a second adapter implementing the same
  `extract(page, carried)` contract — this decision is reversible per environment.

### Measured, not assumed

Docling 2.130.0, run through this adapter against page 2 of `17_DG.pdf` — a real page from a
supplied guide, chosen because our geometric strategy already reads it, so the two can be
compared directly:

| | Geometric strategy | Docling |
|---|---|---|
| Rows | 17 | 17 |
| Codes agreed | — | **17 / 17**, no extras, none missed |
| Column labels | `ADA code`, `Description of benefits`, `Frequency/limitations`, `In-network coverage`, `Out-of-network coverage` | identical |
| Time for the page | ~0.06 s | **12.8 s** warm, 142.8 s on the first run (model download and load) |

Two things follow. Docling is **good** — it independently reproduced the reading of an unruled
table, including the merged frequency cell, which is a genuine cross-check on our geometry. And
Docling is **~230× slower** on a page the deterministic reader already handles, which is
exactly why it is the fallback and not the default: on the three supplied guides (75 pages) it
would turn a 4-second run into roughly 16 minutes, for the same rows.

### Where it sits

`DoclingTableStrategy` is registered as a **fallback** in `TableCascade`. It is asked for a
page only when every deterministic strategy scored zero on it, **and** the page either clearly
holds benefit codes or has no text layer at all.

| Concern | How it is handled |
|---|---|
| Cost | The document is converted **once** and cached per page — conversion is the expensive part, so per-page conversion would repeat it for every page; `document_ai_max_pages` bounds how many pages one document may claim |
| Latency | Conversion runs in a worker thread (`asyncio.to_thread`), so the API event loop keeps serving |
| Trust | Every returned row is re-verified locally: the code must match the CDT shape, and on a page that *does* have text it must appear on that page |
| Scans | Rows from a page with no text layer cannot be cross-checked, so the result carries `dental_guide.rows_not_locally_verifiable` |
| Version drift | Docling's table export API has changed across versions, so the adapter accepts the dataframe export, the cell grid, or the markdown export, in that order |
| Failure | A conversion error raises `DoclingUnavailable`, the cascade logs it, that page yields zero rows, and the document still completes |

Enable it with:

```bash
pip install -r requirements-docling.txt
export EXTRACT_DOCUMENT_AI_PROVIDER=docling
export EXTRACT_DOCLING_OCR=true            # needed for scans, slower
export EXTRACT_DOCUMENT_AI_MAX_PAGES=25
```

Without it, a scanned PDF is refused with *"OCR is required"*. With it, the same PDF is read
and flagged. That is the whole behavioural difference.

### Why pdfplumber remains the base reader

pdfplumber (pdfminer.six underneath) is the primary reader because this problem is **geometry,
not OCR**: per-word bounding boxes, ruling lines, and the filled rectangles that reveal merged
cells — the three signals the geometric strategy runs on. It is MIT, pure Python, fast
(~56 ms/page here) and deterministic.

| Library | What it adds | Why not the base reader |
|---|---|---|
| **PyMuPDF (fitz)** | 5–10× faster, excellent geometry | AGPL-3.0 unless licensed — a real constraint for a commercial product |
| **Docling** | Layout and table-structure models, OCR, reading order | Seconds per page and model weights; ideal as the fallback, wrong as the default |
| **Camelot / Tabula** | Focused table extraction | Camelot's stream mode is roughly our geometric strategy with less merged-cell control; Tabula needs a JVM |
| **Unstructured** | Many formats, RAG chunking | Optimised for text chunks, not cell-accurate tables |
| **Marker / Surya** | Strong PDF→markdown and OCR | Licence restrictions for commercial use; GPU-oriented |

Under any reader, what does not change: column mapping, merged-cell handling, benefit-group
naming, the validators and the CSV contract. That is where the domain lives, and no extraction
library provides it.

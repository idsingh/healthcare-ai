# Reviewer guide

Everything for both exercises is in this repository: the inputs I was given, the code, the
outputs it produced, and the reasoning behind each decision. This page is the map.

## 60 seconds

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest                                 # 132 tests, ~20s
.venv/bin/python -m tools.extract_dg data/dental_guides    # PDFs  -> output/dental_guides/*.csv
.venv/bin/python -m tools.extract_cli extracted_text.txt   # text  -> output/service_output.json
```

No API key needed: the LLM defaults to an offline stub, and the optional Docling fallback is off.

If you only read three things: [`output/dental_guides/README.md`](output/dental_guides/README.md)
(what the CSVs contain and how each column was produced), [`DESIGN.md`](DESIGN.md) §13 and §15–16
(unseen layouts, the Docling decision, when to buy a service instead), and
[`app/application/dental_guide.py`](app/application/dental_guide.py) (the use case, ~330 lines).

## What was asked, and where it is

### Exercise 1 — EOC text → structured JSON

| Requirement | Where |
|---|---|
| Output schema for dental supplemental benefits | [`schema/extraction.schema.json`](schema/extraction.schema.json) · domain model in [`app/domain/models.py`](app/domain/models.py) |
| Extraction design: components + flow | [`DESIGN.md`](DESIGN.md) §2–3 |
| Pseudo-code for the core logic | [`pseudocode.py`](pseudocode.py) |
| Sample output filled from the provided text | [`sample_output.json`](sample_output.json) (hand-written) and [`output/service_output.json`](output/service_output.json) (produced by the service) |
| Part A — packages, premium, benefit maximum, network restriction | `output/service_output.json` → `packages[].premium`, `.benefit_maximum`, `.network_restriction` |
| Part B — services, limits, CDT codes, cost shares | same file → `packages[].services`, `.codes`, `.cost_share` |
| Part C — exclusions, evidence anchoring, validation checks | `packages[].exclusions`; every field carries `evidence.quote/start/end/page`; five validators in [`app/application/validation.py`](app/application/validation.py) |
| LLM prompt strategy, determinism, safety | [`DESIGN.md`](DESIGN.md) §4.5 · contract in [`app/domain/contracts.py`](app/domain/contracts.py) · prompts in [`app/application/llm_extractor.py`](app/application/llm_extractor.py) |
| Don't invent what isn't stated | `status: "not_stated"` with `value: null`; `document.issuer` is the worked example |
| Service: POST /extract, GET /extract/{job_id} | [`app/api/routes.py`](app/api/routes.py) |
| Hexagonal architecture, DI, mockable LLM, input validation, structured logging, retries, useful error states | `app/domain/ports.py`, `app/api/deps.py`, `app/adapters/llm/*`, `app/api/schemas.py`, `app/logging_setup.py`, `app/application/retry.py`, `app/domain/errors.py` |

### Exercise 2 — Dental Guide PDFs → CSV

| Requirement | Where |
|---|---|
| Extract all relevant information from the guides | [`output/dental_guides/`](output/dental_guides/) — 782 rows: 287 + 153 + 342 |
| CSV in the Sample Output format | Exactly the six columns, per guide plus `all_guides.csv` |
| Generic, not tailored to these guides | Column mapping by vocabulary then by content ([`app/application/tables/mapping.py`](app/application/tables/mapping.py)); [`tests/test_generalization.py`](tests/test_generalization.py) fails the build if a carrier, plan or file name appears anywhere in `app/` |
| Missing values as `-` | [`app/application/csv_export.py`](app/application/csv_export.py); `--missing` makes it configurable |
| Handle variations in content and formatting | Three readers behind a scored cascade ([`app/application/tables/cascade.py`](app/application/tables/cascade.py)); eight unseen layouts in [`tests/test_unseen_layouts.py`](tests/test_unseen_layouts.py) |
| Working code repository | This repo; 132 tests |

## Inputs and outputs in the repo

| | Path |
|---|---|
| Exercise 1 input | [`extracted_text.txt`](extracted_text.txt), [`REQUIRMENTS.md`](REQUIRMENTS.md) |
| Exercise 2 inputs | [`data/dental_guides/`](data/dental_guides/) — the three guide PDFs, `Sample_output.csv`, and the P2 brief |
| Exercise 1 outputs | [`output/service_output.json`](output/service_output.json), [`sample_output.json`](sample_output.json) |
| Exercise 2 outputs | [`output/dental_guides/`](output/dental_guides/) — four CSVs plus `extraction_report.json` (columns detected, how each was mapped, which strategy read each page, validation flags) |

## Verify it without trusting me

```bash
# 1. Every evidence quote in the EOC output really is in the source text
.venv/bin/python - <<'PY'
import json, re, unicodedata
collapse = lambda t: re.sub(r"\s+", " ", unicodedata.normalize("NFKC", t)
                            .translate(str.maketrans("–—−‐‑", "-----"))).strip()
raw = collapse(open("extracted_text.txt").read())
doc = json.load(open("output/service_output.json"))
nodes = [*doc["document"].values()] + [n for p in doc["packages"]
         for n in [p["name"], p["premium"], p["benefit_maximum"], p["network_restriction"],
                   *p["services"], *p["codes"], *p["cost_share"], *p["exclusions"]]]
ev = [n for n in nodes if isinstance(n, dict) and n.get("evidence")]
bad = [n for n in ev if collapse(n["evidence"]["quote"]) not in raw]
print(f"{len(ev)-len(bad)}/{len(ev)} quotes verbatim in the source; {len(bad)} not")
PY

# 2. The CSVs against the supplied sample (28 rows of 17_DG)
.venv/bin/python -m pytest tests/test_dental_guide.py -k sample -v

# 3. Layouts none of the guides have — synthesised at test time
.venv/bin/python -m pytest tests/test_unseen_layouts.py -v

# 4. The service over real HTTP
.venv/bin/uvicorn app.api.main:app &
curl -F file=@data/dental_guides/17_DG.pdf localhost:8000/extract/upload
curl "localhost:8000/extract/<job_id>?format=csv" | head -3
```

## Technical decisions, and where they are argued

| Decision | Section |
|---|---|
| Deterministic vs LLM split — regex owns the digits, the model owns the meaning | [`DESIGN.md`](DESIGN.md) §4 |
| Prompt strategy, structured output, determinism, abstention | §4.5 |
| Idempotency, retries, concurrency, degradation | §5 |
| Failure modes and mitigations | §6 |
| The five validators | §7 |
| Observability and evaluation | §8 |
| SOLID/DRY as applied here | §9 |
| PDF path: strategy cascade, column mapping, merged cells | §12 |
| Unseen layouts: three ways to identify a column, and where it still fails | §13 |
| Hosted document AI compared (Mistral, Azure) | §14 |
| Why Docling, measured against pdfplumber + geometry | §15 |
| When to stop maintaining this and buy a service, with thresholds | §16 |
| How the repo was produced, prompt by prompt | [`PROMPTS.md`](PROMPTS.md) |

## Known deviations and limits, stated up front

1. **Frequency column, 12 of 28 sample rows.** The PDF draws one merged frequency cell over
   D0120–D0180 and another over D0210–D0367, both reading *"Unlimited up to annual maximum"*;
   we attribute a merged cell to every row it covers. The sample instead writes *"As stated in
   plan"* for some of those rows while keeping *"Unlimited…"* for others inside the same merged
   cell, and that phrase appears nowhere in the PDF. Detail in
   [`output/dental_guides/README.md`](output/dental_guides/README.md).
2. **Missing values are `-`**, per the brief's explicit note, even where the sample used a
   default phrase. `--missing` makes it configurable.
3. **The committed outputs were produced with the offline stub LLM** (`run.model_id` says so).
   Every number, code and quote is read deterministically and is identical with a real model;
   what improves is benefit-group naming and service wording. See
   [`output/README.md`](output/README.md).
4. **No OCR by default.** A scanned PDF is refused with a reason. Installing the optional
   Docling fallback (`requirements-docling.txt`) makes those pages readable, flagged
   `rows_not_locally_verifiable` because there is no text layer to check them against.
5. **Benefit-group wording** for guides without a category column is a judgement call. The
   deterministic fallback is the document's own section heading; the model refines it.

## If you want to push on something

- Delete a strategy from the cascade in `app/application/tables/cascade.py` and run the suite —
  the unseen-layout tests should fail, not the supplied-guide tests.
- Add a new header wording to `tests/test_generalization.py::test_unseen_header_wording_still_maps`.
- Point `tools/extract_dg.py` at any other dental guide; the run report will say which columns it
  found, which it mapped, and which it could not.

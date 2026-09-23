# Dental Benefit Extraction Service

Plan documents in, structured data out. Two extraction paths share one service:

| Input | Output | Entry point |
|---|---|---|
| Unstructured EOC **text** | Packages JSON, every field anchored to a quote in the source | `POST /extract` |
| Dental Guide **PDF** | Benefit rows → CSV in the customer's column format | `POST /extract/upload`, `GET /extract/{id}?format=csv` |

FastAPI + Pydantic, hexagonal layering, no value in the output without evidence behind it.

`DESIGN.md` is the design deliverable (assumptions, architecture, failure modes). This file
is how to run and read the implementation. [`output/README.md`](output/README.md) explains the
committed output; [`PROMPTS.md`](PROMPTS.md) records how the repo was produced.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.api.main:app --reload            # http://127.0.0.1:8000/docs
.venv/bin/python -m pytest                             # 98 tests, ~25s
.venv/bin/python -m tools.extract_cli extracted_text.txt      # EOC text -> output/service_output.json
.venv/bin/python -m tools.extract_dg data/dental_guides       # PDFs     -> output/dental_guides/*.csv
```

**Start here if you are reviewing this:**
[`output/dental_guides/README.md`](output/dental_guides/README.md) for the Dental Guide CSVs
(782 rows from three differently-laid-out PDFs, how each column was produced, and where our
reading differs from the supplied sample), or [`output/README.md`](output/README.md) for the
EOC text extraction.

No API key is needed: the default LLM adapter is an offline stub that implements the same
port. For real extraction:

```bash
export EXTRACT_LLM_PROVIDER=openrouter
export EXTRACT_OPENROUTER_API_KEY=sk-or-...
export EXTRACT_MODEL_ID=openai/gpt-5.6-luna            # see .env.example
```

## The two endpoints

```bash
curl -X POST localhost:8000/extract \
  -H 'content-type: application/json' \
  -d '{"text": "...document text...", "document_id": "eoc-excerpt"}'
# 202 {"job_id":"job_ee417fc392e742f2","status":"queued","links":{"self":"/extract/job_..."}}

curl localhost:8000/extract/job_ee417fc392e742f2
# 200 {"status":"succeeded","needs_review":true,"result":{...}}
```

| Situation | Response |
|---|---|
| Accepted | `202` + job id |
| Same text, same config, already submitted | `200` + the original job, `idempotent_replay: true` |
| Body fails validation (blank, <40 chars, >2MB, unknown field) | `422` `invalid_request` |
| Unknown job id | `404` `job_not_found` |
| Input rejected during preprocessing (binary, unusable) | job `failed`, `error.code = input_rejected` |
| LLM pass failed for some block | job `partial`, deterministic content kept, `run.degraded = true` |
| Validation error survived into the result | job `partial`, `validation.passed = false` |

Job states: `queued → running → succeeded | partial | failed`. `needs_review` is separate
from status: a perfectly healthy run on a truncated document still asks for a human.

## Layout

```
app/
  domain/        models.py contracts.py ports.py errors.py    # no framework imports
  application/   text path : preprocess -> segmentation -> scanners -> llm_extractor
                             -> merge -> validation
                 pdf path  : tables/{ruled,geometric,cascade,mapping} -> dental_guide.py
                             -> csv_export.py
                 pipeline.py · dental_guide.py (use cases)   service.py (jobs, idempotency)
  adapters/      llm/{openrouter,stub,scripted}.py   pdf/pdfplumber_source.py
                 repository/memory.py
  api/           main.py routes.py schemas.py deps.py         # thin: no business logic
tests/           preprocess · tables · llm reliability · validation · api · dental guide
                 · generalization guard · provider adapter
```

Dependencies point inward. `app/api/deps.py` is the only place adapters are chosen, which is
what makes the LLM swappable by config and replaceable in tests
(`app.dependency_overrides[get_service]`, `ScriptedLLMClient`).

## Flow (EOC text)

```
text ─▶ preprocess ─▶ segment ─▶ ┌ deterministic scanners ┐ ─▶ merge ─▶ validate ─▶ result
        (normalize,   (package    └ LLM pass (structured   ┘   (numbers   (5 rules,
         de-boilerplate, blocks,     output + repair loop)      from        can null
         offset map)    columns)                                scanners)   values)
```

Package blocks are independent, so they run concurrently under a semaphore. A block whose
LLM pass fails degrades to deterministic-only output; the document still completes.

## Flow (Dental Guide PDF)

```
pdf ─▶ strategy cascade ─▶ column mapping ─▶ row assembly ─▶ benefit group ─▶ CSV
       ruled | geometric   labels matched     wrapped lines   category column
       | LLM fallback      by vocabulary      merged cells    > heading > model
       (scored, best wins) (never position)   furniture out
```

No strategy is trusted to be the one that works: each page is read by every deterministic
reader, scored on how many rows carry a code and real text, and the best reading is kept. A
layout that defeats them all falls back to the LLM reader rather than yielding nothing.
Columns are identified by header vocabulary, then by cell contents when the header is missing
or misleading, then by the mapping carried from the last page that had one — so a guide with
reordered columns, invented header wording, or no header at all still extracts (measured in
`tests/test_unseen_layouts.py`; see `DESIGN.md` §13). The extractor keys off nothing
document-specific — `tests/test_generalization.py` fails the build if a carrier, plan or file
name appears anywhere in `app/`.

PDF reading is `pdfplumber` (pdfminer.six underneath), behind one adapter. There is no OCR: a
scanned PDF is refused with a reason rather than silently yielding an empty CSV. `DESIGN.md`
§14 compares this with Mistral Document AI and Azure AI Document Intelligence and says where
each belongs.

## Never trusting the model

1. **Contract, not prose.** The model answers a narrow Pydantic contract
   (`domain/contracts.py`) through structured output. Its reply is parsed and schema-checked
   before it leaves `llm_extractor.py`; a bad reply is repaired once with the validator error,
   then the block degrades.
2. **Quotes, not claims.** Every item must carry `evidence_quote`. `merge.py` resolves each
   quote inside its own package span; anything unresolvable is withheld
   (`status: "unverified"`), never emitted as a value.
3. **Scanners own the digits.** Money, percentages and CDT codes come from regex scanners and
   are injected into the prompt as pre-scanned literals. If the model contradicts a scanner on
   the same sentence, the scanner wins and the discarded claim is recorded in `notes`.
4. **Five validators** (`application/validation.py`): groundedness, numeric fidelity,
   invariants, referential integrity, review routing. An `error` nulls the value and fails the
   document; a `warn` routes it to review.
5. **Document text is data.** Injection attempts land inside `<document>` tags and cannot
   change the deterministic values — covered by `test_document_text_cannot_issue_instructions`.

## Tests worth reading first

| Test | What it pins down |
|---|---|
| `test_llm_reliability.py` | transient retry, permanent fail-fast, JSON repair, degraded block, hallucinated evidence, fabricated digits, prompt injection |
| `test_preprocess.py` | header/footer stripping that does **not** eat repeated content, page-break spanning, window-scoped quote resolution |
| `test_extraction_e2e.py` | every fact in the real excerpt, offsets re-verified independently, block concurrency |
| `test_openrouter_adapter.py` | which HTTP failures are retryable |
| `test_api.py`, `test_api_pdf.py` | status codes, idempotent replay, PDF upload, CSV projection, error bodies |
| `test_tables.py` | the layout hazards, on synthetic pages: unruled tables, wrapped cells, merged cells spanning rows, page furniture, partial headers on continuation pages |
| `test_dental_guide.py` | all three real guides, the customer sample comparison, guides with no coverage columns |
| `test_generalization.py` | no document-specific identifiers in `app/`; unseen header wording still maps |

## Deliberately not built

In-process job store and background tasks instead of Postgres/Redis/Celery; no auth, no rate
limiter, no metrics exporter. Jobs are small, short-lived and single-process here, and every
one of those sits behind a port (`JobRepository`) or a middleware seam when it is actually
needed. The `notes` in `DESIGN.md` §5 describe what changes when it is.

Offsets in the output are relative to the service's own normalization (page boilerplate
removed). `tools/validate.py` belongs to the design deliverable and validates
`sample_output.json` against its own normalization of the raw file, so the two offset spaces
are not interchangeable.

# How this repo was produced

Built with [Claude Code](https://claude.com/claude-code) (Opus 5) in a single session, on
2026-09-21. Every prompt I gave is reproduced below **verbatim**, in order, with what each
one produced. Commits carry `Co-Authored-By: Claude Opus 5`.

The intent of this file is reviewability: you can see exactly what was asked for, what was
specified by me versus decided by the model, and where the work was corrected.

---

## 1. Design

> You are a Principal AI Engineer. Design a production grade txt to json extraction service.
> Requirements - 1. Input is a unstructued/Semi Structured Txt 2. Output must confirm to a
> predefined schema available in @REQUIRMENTS.md. 3. Use an LLM wherever semantic parsing
> (Default to GPT 5.6 Luna via Openrouter) 4. Handle malformed input, missed fields,,
> ambiguity and LLM failures. 4. Support large documents and concurrent processing.
> 4. Ensure idempotency, retries, observability and evaluation. Assumptions - 1. State
> assumptions 2. Propose architecture 3. Define components and responsbilities. 4. Explain
> what can be determinstic vs llm powered 5. Identify failure modes and mitigations 5. Use
> SOLID/DRY throughout.

Produced: `DESIGN.md`, `schema/extraction.schema.json`, `sample_output.json`,
`pseudocode.py`, `tools/validate.py`.

The design deliverable is hand-written JSON plus a runnable validator, not code: the
validator proves the sample is grounded in `extracted_text.txt` and fails on a planted
hallucination.

## 2. Implementation

> Implement the extraction service. Use - FastAPI, Pydantic, JSON Schema/Structured Output.
> Create 2 endpoints - 1. POST /extract 2. GET /extract/{job_id} Flow -> Txt -> preprocess ->
> llm extraction -> schema validation -> retry/repair -> result. Requirements - 1. Clean
> Modular Architecture (Use Hexagonal/DDD) 2. Dependency injection for the LLM 3. Mockable
> LLM CLient 4. Input validation 5. Structured logging 6. Transient Retries and Failures
> 7. Never trust raw LLM Output. 8. Return useful error states. One key thing to remember -
> Do not overengineer infrastructure that is not required.

Produced: `app/` (domain, application, adapters, api), `tests/` (66 tests), `README.md`.

The last sentence is why there is no Postgres, Redis, Celery, auth or metrics exporter: jobs
are in-process behind a `JobRepository` port.

## 3–5. Publishing

> Create a new repo at my personal account in github - idsingh and commit it. Use
> github-personal as the ssh origin

> Change it to idsingh43@gmail.com in the commit history

> Change this to public repo and test it with with actual txt file available
> @extracted_text.txt

Produced: this repository; the service was then run against `extracted_text.txt` over real
HTTP (`POST /extract` → `202`, `GET /extract/{job_id}` → `succeeded`), including the error
paths (`404` unknown job, `422` blank input, `failed/input_rejected` for binary input).

## 6. Committing the result

> Remember to commit the output.json in the repo so that interviewer can check

Produced: `output/service_output.json`, committed rather than gitignored.

## 7. The question that mattered

> Did it use the llm?

**No** — and that is worth being explicit about. Every run in this session used the offline
stub adapter (`run.model_id: "stub/deterministic-offline"`), because `EXTRACT_LLM_PROVIDER`
defaults to `stub` and no API key was set. The full pipeline ran (prompt construction,
structured-output contract, schema validation, merge, groundedness, validators) — against a
deterministic stand-in rather than a network model.

What this does and does not demonstrate is spelled out in
[`output/README.md`](output/README.md#with-an-external-model): every number in the output
comes from regex scanners either way, so premiums, maximums, percentages and CDT codes are
identical with any model; service naming, cost-share attachment and exclusion normalization
are the model-dependent parts.

## 8–9. Explaining the output

> Document this in the output folder round what would change in case of a external model

> and make it easier for interviewer to understand the output and how it was generated

Produced: [`output/README.md`](output/README.md) — how the file was generated, how to read
one field, where each value came from (32 deterministic / 11 merged / 5 model / 1 correct
null), a paste-able snippet that re-verifies all 48 quotes against the raw text, and the
external-model section.

## 10. This file

> Also document the prompts i used in this conversation to generate the code

## 11. Second brief: Dental Guides to CSV

> Check the new requirements and pdf documents are under /Users/inder/Downloads/Assignment_extended
> and enhance the solution

> For pdf extraction, Remember to use a scalable solution. it must not break over new pdfs

Produced: the PDF path — `adapters/pdf/`, `application/tables/` (ruled, geometric, cascade,
mapping), `application/dental_guide.py`, `application/csv_export.py`, `tools/extract_dg.py`,
`POST /extract/upload`, `GET /extract/{id}?format=csv`, and
[`output/dental_guides/`](output/dental_guides/README.md) — 782 rows from three guides with
three different layouts.

The second instruction is why the design is a scored cascade of independent readers with an
LLM fallback, rather than whichever single extraction call happened to work on these three
files, and why `tests/test_generalization.py` fails the build if a carrier or file name ever
appears in `app/`.

---

## 12. Hardening the PDF path

> What if the pdf has a new kind of layout?

> Also explain me how this logic would compare us tools like Mistral AI & Azure Document AI and
> the tradeoffs

> Also what are you using for extracting the pdf

> Are there any better alternatives for extracting PDFs open source than pdfplumber E.g Docling?

> Add Mistral AI/Document AI as an adapter as fallback for handling cases that can't be parsed
> using determinstic mechanism

> Replace with docling and document the decision and test everything end to end

The first question was answered by building eight PDFs with layouts the code had never seen
(`tests/test_unseen_layouts.py`) rather than by assertion; three of them failed, and the fixes —
content-based column inference, per-table schemas — are in `DESIGN.md` §13.

The fallback was first built against Mistral Document AI, then replaced with Docling running
in-process once the trade-off was examined properly: in a healthcare pipeline, not sending a
member-facing plan document to a third party and not carrying a per-page bill outweigh the
convenience of an API key. `DESIGN.md` §15 records that decision, including what it costs.

## 13. Invocation policy, escalation path and the reviewer guide

> We can have a mix of digital text + docling so ideally we should call it only if as a fallback
> or as per the requirement and if we still face issues then document that mistral AI or document
> AI can serve better than maintaining a solution with edge cases.

> In the end i want you to include the outputs and inputs to the new requirements in the repo and
> document similar to how it was done in previous exercise along with the guide for reviewer.
> Make sure everything is documented in the repo around technical decisions and documentation

Produced: `EXTRACT_DOCUMENT_AI_MODE=always` / `--reader docling` so the fallback can be invoked
deliberately rather than only on failure; `DESIGN.md` §16, which names the measurable point at
which a hosted service beats maintaining layout edge cases; and
[`REVIEWER_GUIDE.md`](REVIEWER_GUIDE.md), mapping every requirement from both exercises to the
file, test and output that satisfies it.

## Corrections made along the way

Worth reading, because they are the parts a demo usually hides. Each was caught by a test or
by a verification step, and each now has a regression test:

| Defect | Symptom | Where the fix lives |
|---|---|---|
| Frequency-based boilerplate stripping deleted repeated **content** | Package 2 came back empty — its CDT bullets and its `$0 copay` sentence duplicate package 1's | `preprocess.py`, zone-restricted detection |
| Running header whose first copy sits mid-page survived | The header text leaked into an exclusion's evidence quote across the page break | `preprocess.py`, digit-masked keys + first-copy tracking |
| `re.finditer` is non-overlapping | `$13.00 monthly premium Two cleanings per year` consumed the real limit phrase | `scanners.py`, lookbehind for decimal tails |
| CDT description ran past its own bullet | `Prophylaxis - adult Two fluoride treatments per year` | `scanners.py`, per-logical-line scanning after re-joining wrapped lines |
| "Truncated" meant "last", not "cut off" | Package 1's final exclusion was wrongly marked truncated | `merge.py`, requires a missing sentence terminator |
| Model/scanner disagreements were resolved silently | A fabricated percentage vanished with no trace | `merge.py`, contradiction recorded in `notes` |

## What I specified vs what the model decided

- **Mine:** the problem, the stack (FastAPI, Pydantic, structured output), hexagonal layering,
  DI for the LLM, the two endpoints, the flow, the reliability requirements, the
  "never trust raw LLM output" rule, and the instruction not to overengineer.
- **The model's:** the evidence-anchoring design (`Field<T>` with quote + offsets), the
  deterministic-vs-LLM split, the column de-interleaving approach, the five validators, the
  stub/scripted test doubles, and the test suite.
- **Reviewed by me:** the output against the source document, and the decision to keep the
  run honest about having used the offline stub.

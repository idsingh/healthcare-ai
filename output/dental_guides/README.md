# Dental Guide extraction — output

CSV extracted from the three Dental Guide PDFs in [`../../data/dental_guides`](../../data/dental_guides),
in the column format of the customer's `Sample_output.csv`.

| File | Rows | Layout it exercises |
|---|---:|---|
| `17_DG.csv` | 287 | Table drawn **without ruling lines**; frequency stated once for a block of codes (merged cell); in- and out-of-network columns |
| `DBD_Copper_Y0020_WCM_4952801E_C_R.csv` | 153 | Ruled table, three columns, **no coverage columns at all** |
| `H0544-056-000_dental_filtered__type1_pages.csv` | 342 | Ruled table with an explicit **Service Category** column and a column we do not need |
| `all_guides.csv` | 782 | The three concatenated |
| `extraction_report.json` | — | Per document: columns detected, how each was mapped, which strategy read each page, validation flags |

Columns are exactly the sample's six. Anything a guide does not state is written as `-`
(configurable with `--missing`).

## Reproduce

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m tools.extract_dg data/dental_guides        # -> output/dental_guides/
```

Or over HTTP:

```bash
.venv/bin/uvicorn app.api.main:app
curl -F file=@data/dental_guides/17_DG.pdf localhost:8000/extract/upload   # -> {"job_id": ...}
curl "localhost:8000/extract/<job_id>?format=csv" -o guide.csv
```

## How a row is produced

```
PDF page ─▶ strategy cascade ─▶ column mapping ─▶ row assembly ─▶ benefit group ─▶ CSV
            ruled | geometric   header labels ->   merge wrapped   category column
            | LLM fallback      canonical fields   lines & spans   > heading > model
```

1. **Cascade.** Each page is read by every deterministic strategy and scored by the share of
   rows that carry a code plus real text; the best reading wins. `17_DG.pdf` has no ruling
   lines, so the ruled strategy scores 0 and the geometric one is used; the other two guides
   are read by the ruled strategy. A page that defeats both falls back to the LLM reader.
2. **Column mapping.** Header labels are matched to canonical fields by vocabulary, never by
   position: `ADA code`, `Code`, `Codes`, `CDT Code` → code; `Code Description`,
   `Nomenclature`, `Description of benefits` → description; `Periodicity`,
   `Frequency Limitation` → frequency; `Participating`/`Non-Participating` and
   `In-network`/`Out-of-network` → the two coverage columns.
3. **Row assembly.** Wrapped cells are rejoined (a description split across three lines is one
   value), a cell drawn as one tall rectangle over several rows applies to all of them, and
   running headers and footers are dropped so they cannot become a benefit group.
4. **Benefit group.** An explicit category column wins; otherwise the section heading above
   the row; otherwise the model names it from the code and description.

## What the model does and does not do

Everything factual is read from the table: codes, descriptions, frequency text, coverage
values. The model is asked for one thing — **naming the benefit group** where the guide has no
category column — and its reply is constrained to a schema, checked against the codes it was
given, and ignored when it answers about a code it was not asked about.

This output was produced with the **offline stub** (`run.model_id` in the report says so), so
benefit groups come from a small generic dental vocabulary. With `EXTRACT_LLM_PROVIDER=openrouter`
the group names get better and nothing else in the CSV moves — the other five columns never
pass through a model. See [`../README.md`](../README.md) for that comparison in detail.

## Two honest notes on the sample

1. **Frequency.** For `17_DG.pdf` our `Frequency/Limitations` matches the sample on 16 of 28
   rows. The differences are not misreads: the PDF draws one merged frequency cell over
   D0120–D0180 and another over D0210–D0367, both reading *"Unlimited up to annual maximum"*,
   and we attribute that to every row the cell covers. The sample instead writes
   *"As stated in plan"* for D0180 and D0210–D0274 while keeping *"Unlimited up to annual
   maximum"* for D0330–D0367 — which sits inside the same merged cell. The phrase
   "As stated in plan" appears nowhere in the PDF.
2. **Missing values.** The brief says to write `-`; the sample writes a default phrase in some
   of those cells. We follow the brief, and `--missing` makes the placeholder configurable.

Everything else matches: all 28 codes, 27 of 28 descriptions (the last is a wording split, see
below), and both coverage columns on all 28 rows.

The one description difference is D1110, where the PDF reads
*"Prophylaxis adult (Removal of plaque, calculus and stains …)"*. We split the leading phrase
into the benefit group and keep the parenthetical as the description, which is what the sample
does too — the remaining gap is only in where the trailing text is cut.

## Validation the extractor runs on itself

Recorded per document in `extraction_report.json`:

| Check | What it catches |
|---|---|
| `dental_guide.no_rows` (error) | A guide that produced nothing — a layout no strategy could read |
| `dental_guide.unmapped_columns` (warn) | A column we did not map, e.g. `Prior Authorization Required?` |
| `dental_guide.column_not_present` (warn) | The guide states no coverage or no frequency; the column is `-` rather than guessed |
| `dental_guide.coverage_unqualified` (warn) | A coverage column that never says which network it is |
| `dental_guide.descriptions_missing` (warn) | More than 10% of rows without a description — usually a column mapped wrongly |
| `dental_guide.repeated_codes` (info) | The same code appearing with different details, which is legal in these guides |

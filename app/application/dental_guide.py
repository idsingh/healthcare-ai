"""Dental Guide use case: PDF benefit tables -> CSV-ready rows.

    pdf -> pages -> table cascade -> canonical column mapping -> rows
        -> benefit-group naming (LLM, with a deterministic fallback) -> validate

Everything factual — codes, descriptions, frequencies, coverage — is read from
the table by the deterministic strategies. The model is used for the one part
that is genuinely semantic: naming the benefit group when the guide does not
carry an explicit category column.
"""
from __future__ import annotations

import re
import time
import uuid
from pathlib import Path

from pydantic import ValidationError

from app.adapters.pdf.pdfplumber_source import PdfPlumberSource
from app.application.tables.cascade import TableCascade
from app.application.tables.mapping import Field as Col
from app.application.tables.mapping import infer_roles, map_labels, unmapped, unqualified_coverage
from app.application.tables.models import RawRow, TableSchema
from app.config import PIPELINE_VERSION, PROMPT_VERSION, Settings
from app.domain.contracts import LlmBenefitGrouping, benefit_grouping_schema
from app.domain.errors import InputRejected
from app.domain.models import (
    BenefitRow, DentalGuideDocument, DentalGuideResult, Flag, GroupSource, RunInfo, Severity,
    ValidationMetrics, ValidationReport, utcnow,
)
from app.domain.ports import LLMClient
from app.logging_setup import get_logger

log = get_logger("extract.dental_guide")

GROUP_BATCH = 40
MAX_GROUP_LEN = 60
# 'Prophylaxis adult (Removal of plaque, calculus ...)' — the leading phrase is the
# benefit group and the parenthesis is the real description. Generic PDF authoring
# habit, not a property of any one guide.
PREFIXED_DESCRIPTION = re.compile(r"^(?P<prefix>[^()]{3,45}?)\s*\((?P<body>[^()].{25,})\)\s*$")
LONG_GROUP = 40


def tidy_group(value: str | None) -> str | None:
    """A section heading can carry a parenthetical caveat — 'Dentures rebase (not
    covered if within six months of initial placement)'. Keep the name, drop the
    caveat, which belongs to the frequency column anyway."""
    if not value:
        return value
    text = re.sub(r"\s+", " ", value).strip(" .;")
    if len(text) > LONG_GROUP and "(" in text:
        text = text.split("(", 1)[0].strip(" -–,")
    return text or None

GROUPING_SYSTEM = """You name benefit groups for dental procedure codes.

Rules:
1. Reply only with JSON matching the schema: one entry per code you are given.
2. Copy each code exactly as given. Never invent codes, never drop codes.
3. A benefit group is a short clinical grouping of 1-4 words, in the wording a
   benefits document would use: 'Exams', 'Bitewing X-rays', 'Amalgam',
   'Resin-based composite', 'Crowns', 'Extractions'.
4. Base the group only on the code and the description you are given. If a
   section heading is supplied, prefer wording consistent with it.
5. If you cannot tell, repeat the supplied section heading, or return '-'."""


class DentalGuidePipeline:
    def __init__(self, settings: Settings, llm: LLMClient | None = None,
                 cascade: TableCascade | None = None):
        self._s = settings
        self._llm = llm
        self._cascade = cascade or TableCascade()
        self._column_cache: dict[tuple, dict[Col, int]] = {}

    # -- public -------------------------------------------------------------
    async def run(self, path: str | Path, *, run_id: str | None = None) -> DentalGuideResult:
        path = Path(path)
        if not path.exists():
            raise InputRejected(f"file not found: {path}", stage="pdf_ingest")
        started, t0 = utcnow(), time.perf_counter()

        self._column_cache.clear()
        rows, document = await self._read_pages(path)
        rows = self._dedupe(rows)
        flags = await self._assign_groups(rows)
        result = DentalGuideResult(
            run=RunInfo(
                run_id=run_id or f"dg_{uuid.uuid4().hex[:12]}",
                content_sha256=self._sha(path), idempotency_key=self._sha(path),
                started_at=started, model_id=getattr(self._llm, "model_id", None),
                model_params={"temperature": self._s.temperature, "seed": self._s.seed},
                prompt_version=PROMPT_VERSION, pipeline_version=PIPELINE_VERSION,
                blocks_total=document.pages_with_rows),
            document=document, rows=rows)
        result.validation = self._validate(result, extra_flags=flags)
        result.run.completed_at = utcnow()
        log.info("dental guide extracted", extra={
            "file": path.name, "pages": document.pages, "rows": len(rows),
            "strategies": document.strategies, "unmapped_columns": document.unmapped_columns,
            "duration_ms": round((time.perf_counter() - t0) * 1000)})
        return result

    # -- read ---------------------------------------------------------------
    async def _read_pages(self, path: Path) -> tuple[list[BenefitRow], DentalGuideDocument]:
        schema: TableSchema | None = None
        best_mapping: dict[Col, int] = {}
        best_labels: list[str] = []
        rows: list[BenefitRow] = []
        strategies: dict[str, int] = {}
        pages = pages_with_rows = unverifiable = 0

        self._cascade.start_document()
        for page in PdfPlumberSource(path, require_text=not self._cascade.has_fallbacks).pages():
            pages += 1
            table = await self._cascade.extract_page(page, schema)
            schema = table.schema or schema
            # A guide with no header row anywhere still yields rows: the columns
            # are then identified from their contents.
            if not table.rows:
                continue
            pages_with_rows += 1
            strategies[table.strategy] = strategies.get(table.strategy, 0) + 1

            page_text = page.text
            for segment in table.segments:
                seg_schema = segment.schema or schema
                labels = seg_schema.labels if seg_schema else []
                mapping = self._columns_for(labels, segment.rows, best_mapping, best_labels)
                if len(mapping) >= len(best_mapping):
                    best_mapping, best_labels = mapping, list(labels)
                for raw in segment.rows:
                    row = self._to_row(raw, mapping, table.strategy, path.name)
                    if not row:
                        continue
                    # The code must be on the page. A page with no text layer (a
                    # scan read by the document-AI fallback) cannot be checked
                    # this way; those rows are flagged instead.
                    if page_text and row.dental_code not in page_text:
                        continue
                    if not page_text:
                        unverifiable += 1
                    rows.append(row)

        document = DentalGuideDocument(
            file_name=path.name, pages=pages, pages_with_rows=pages_with_rows,
            rows_without_text_layer=unverifiable,
            column_labels=best_labels or (schema.labels if schema else []),
            mapped_fields=[f.value for f in best_mapping],
            unmapped_columns=unmapped(best_labels), strategies=strategies)
        return rows, document

    def _columns_for(self, labels: list[str], rows: list[RawRow],
                     best_mapping: dict[Col, int], best_labels: list[str]) -> dict[Col, int]:
        """Three ways to identify a column, cheapest first:

        1. the header label, matched by vocabulary;
        2. the cell contents, when the header is missing or worded oddly;
        3. the carried mapping, for a continuation page that lost its header.
        """
        key = tuple(labels)
        if key in self._column_cache:
            return self._column_cache[key]

        mapping = map_labels(list(labels))
        if len(mapping) < 5:
            mapping = infer_roles([r.cells for r in rows], mapping)
        if Col.code not in mapping and len(best_labels) == len(labels):
            mapping = best_mapping                     # continuation page lost its header
        self._column_cache[key] = mapping
        log.debug("columns identified", extra={"labels": list(labels),
                                               "mapping": {k.value: v for k, v in mapping.items()}})
        return mapping

    def _to_row(self, raw: RawRow, mapping: dict[Col, int], strategy: str,
                file_name: str) -> BenefitRow | None:
        code = raw.code
        if not code:
            return None

        def cell(field: Col) -> str | None:
            index = mapping.get(field)
            if index is None or index >= len(raw.cells):
                return None
            value = re.sub(r"\s+", " ", raw.cells[index]).strip()
            return value or None

        description = cell(Col.description)
        if description:
            description = description.replace(code, "").strip(" -–—:")
        group = cell(Col.group) or raw.group
        source = GroupSource.column if cell(Col.group) else (
            GroupSource.heading if raw.group else GroupSource.missing)

        if description and (m := PREFIXED_DESCRIPTION.match(description)):
            group = group if source is GroupSource.column else m.group("prefix").strip()
            description = m.group("body").strip()
            source = source if source is GroupSource.column else GroupSource.heading

        return BenefitRow(
            benefit_group=tidy_group(group), dental_code=code, description=description,
            frequency=cell(Col.frequency), in_network=cell(Col.in_network),
            out_network=cell(Col.out_network), page=raw.page, source_file=file_name,
            strategy=strategy, group_source=source)

    @staticmethod
    def _dedupe(rows: list[BenefitRow]) -> list[BenefitRow]:
        seen: set[tuple] = set()
        out: list[BenefitRow] = []
        for row in rows:
            key = (row.dental_code, row.description, row.frequency, row.in_network, row.out_network)
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
        return out

    # -- benefit groups -----------------------------------------------------
    async def _assign_groups(self, rows: list[BenefitRow]) -> list[Flag]:
        """Ask the model to name a group for rows the document does not group
        itself. Deterministic content is never sent back for re-writing."""
        pending = [r for r in rows if r.group_source is not GroupSource.column]
        if not pending or self._llm is None or not self._s.dg_llm_grouping:
            return []

        flags: list[Flag] = []
        by_code: dict[str, list[BenefitRow]] = {}
        for row in pending:
            by_code.setdefault(row.dental_code, []).append(row)
        items = [(code, group[0]) for code, group in by_code.items()]

        for start in range(0, len(items), GROUP_BATCH):
            batch = items[start:start + GROUP_BATCH]
            try:
                named = await self._name_batch(batch)
            except Exception as exc:
                log.warning("benefit grouping degraded", extra={"error": str(exc)[:200]})
                flags.append(Flag(rule="dental_guide.grouping_degraded", severity=Severity.warn,
                                  message="benefit groups fell back to the document's own headings",
                                  path="/rows"))
                break
            for code, name in named.items():
                for row in by_code.get(code, []):
                    row.benefit_group = name
                    row.group_source = GroupSource.llm
        return flags

    async def _name_batch(self, batch: list[tuple[str, BenefitRow]]) -> dict[str, str]:
        lines = "\n".join(
            f"{code} | {row.description or '-'} | section heading: {row.benefit_group or '-'}"
            for code, row in batch)
        payload = await self._llm.complete_json(
            system=GROUPING_SYSTEM,
            user=f"Name the benefit group for each row.\n\n{lines}",
            json_schema=benefit_grouping_schema(), schema_name="LlmBenefitGrouping",
            seed=self._s.seed, temperature=self._s.temperature)
        try:
            parsed = LlmBenefitGrouping.model_validate(payload)
        except ValidationError as exc:
            raise ValueError(f"grouping reply did not satisfy the contract: {exc.errors()[:2]}")

        known = {code for code, _ in batch}
        out: dict[str, str] = {}
        for item in parsed.groups:
            name = re.sub(r"\s+", " ", item.benefit_group or "").strip()
            if item.code in known and 0 < len(name) <= MAX_GROUP_LEN and not name.isdigit():
                out[item.code] = name                       # unknown codes are ignored
        return out

    # -- validation ---------------------------------------------------------
    def _validate(self, result: DentalGuideResult, extra_flags: list[Flag]) -> ValidationReport:
        flags = list(extra_flags)
        doc, rows = result.document, result.rows

        if not rows:
            flags.append(Flag(rule="dental_guide.no_rows", severity=Severity.error,
                              message="no benefit rows were found in this document", path="/rows"))
        if doc.unmapped_columns:
            flags.append(Flag(rule="dental_guide.unmapped_columns", severity=Severity.warn,
                              message=f"columns not mapped to an output field: {doc.unmapped_columns}",
                              path="/document/column_labels"))
        for field, label in ((Col.in_network, "In-Network Coverage"),
                             (Col.out_network, "Out-of-Network Coverage"),
                             (Col.frequency, "Frequency/Limitations")):
            if field.value not in doc.mapped_fields:
                flags.append(Flag(rule="dental_guide.column_not_present", severity=Severity.warn,
                                  message=f"this guide states no {label}; the column is filled with '-'",
                                  path="/rows"))
        if unqualified_coverage(doc.column_labels):
            flags.append(Flag(rule="dental_guide.coverage_unqualified", severity=Severity.warn,
                              message="coverage column does not say which network it applies to; "
                                      "mapped to in-network", path="/rows"))
        missing_description = sum(1 for r in rows if not r.description)
        if rows and missing_description > len(rows) * 0.1:
            flags.append(Flag(rule="dental_guide.descriptions_missing", severity=Severity.warn,
                              message=f"{missing_description} of {len(rows)} rows have no description",
                              path="/rows"))
        if doc.rows_without_text_layer:
            flags.append(Flag(rule="dental_guide.rows_not_locally_verifiable", severity=Severity.warn,
                              message=f"{doc.rows_without_text_layer} rows come from pages with no "
                                      "text layer, so their codes could not be checked against the "
                                      "page itself", path="/rows"))
        duplicates = len(rows) - len({r.dental_code for r in rows})
        if duplicates:
            flags.append(Flag(rule="dental_guide.repeated_codes", severity=Severity.info,
                              message=f"{duplicates} rows repeat a code with different details",
                              path="/rows"))

        filled = sum(1 for r in rows for v in (r.benefit_group, r.description, r.frequency,
                                               r.in_network, r.out_network) if v)
        total = max(len(rows) * 5, 1)
        return ValidationReport(
            passed=not any(f.severity == Severity.error for f in flags),
            needs_review=any(f.severity in (Severity.error, Severity.warn) for f in flags),
            flags=flags,
            metrics=ValidationMetrics(fields_total=len(rows) * 5, fields_grounded=filled,
                                      fields_null=total - filled,
                                      groundedness_rate=round(filled / total, 4)))

    @staticmethod
    def _sha(path: Path) -> str:
        import hashlib
        return hashlib.sha256(path.read_bytes()).hexdigest()

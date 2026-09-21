Context
Your task is to design and partially implement an extraction approach that converts a small excerpt from an Evidence of Coverage (EOC) document into structured output suitable for downstream analytics.
Inputs Provided
PDF excerpt (for visual reference only).
extracted_text.txt (plain text extracted from the same pages; use this as your primary input).
What We Care About
Correctness: do not invent information that is not present in the text.
Robustness: handle formatting quirks (bullets, line breaks, items split across pages).
Design quality: clear schema, traceability back to evidence text, and validation checks.
Pragmatism: solutions should be implementable and maintainable.
Your Deliverables (during the interview)
A proposed output schema (JSON) for dental supplemental benefits and packages.
A high-level extraction design (components + flow).
Light pseudo-code for the core extraction logic (string parsing + LLM step if used).
A sample output JSON filled for the provided text (does not need to be perfect; focus on structure + key fields).
Exercise Parts (Easy → Medium → Hard)
Work in order. If you finish early, improve validation and edge-case coverage.
Part A - Easy (structure + key facts)
Detect each Optional Supplemental Package and extract: package name, premium (amount + cadence), and benefit maximum (if stated).
Capture provider network restriction (e.g., 'LIBERTY Dental providers only') when present.
Part B - Medium (benefit details)
Extract included services and limits (e.g., 'Two oral exams each year', 'Two cleanings per year').
Extract associated dental codes (D0120, D0140, ...) and map them to their descriptions.
Extract cost-share statements (copay/coinsurance) and attach them to the right package.
Part C - Hard (exclusions + evidence + reliability)
Extract exclusions/limitations as a normalized list.
Add evidence anchoring: for each extracted field, store the supporting phrase (or a short excerpt) from extracted_text.txt.
Propose (at least) 3 validation checks that reduce hallucinations and catch contradictions (e.g., missing premium, ambiguous copay).
If you use an LLM step: describe your prompt strategy and how you ensure determinism and safe extraction.
Constraints
Assume you cannot rely on perfect PDF layout; text may be messy and line breaks may be arbitrary.
Do not use external internet resources.
If something is not explicitly stated, represent it as null/unknown rather than guessing.
Keep your design generalizable to other supplemental benefit sections (not only dental).
Expected Output Shape (Example Skeleton)
{
  "document": {"plan_name": "Anthem Select (HMO)", "year": 2024},
  "packages": [
    {
      "package_id": "pkg_1",
      "name": "Optional supplemental package 1 - Preventive dental package",
      "premium": {"amount_usd": 13.00, "cadence": "monthly", "evidence": "Premium: $13.00 monthly premium"},
      "benefit_maximum": {"amount_usd": 500, "period": "per year", "evidence": "plan will pay up to $500 ... each year"},
      "network_restriction": {"value": "LIBERTY Dental providers only", "evidence": "Coverage is available ..."},
      "services": [ /* normalized services + limits + codes */ ],
      "cost_share": [ /* copay/coinsurance rules */ ],
      "exclusions": [ /* normalized exclusions */ ]
    }
  ]
}
Submission
You will share your schema and approach verbally (or on a shared editor/whiteboard). If you write pseudo-code, keep it short and focused on core extraction logic.

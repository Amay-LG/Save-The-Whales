---
name: owner-check
description: Check one owner's (Person 1-4) work against their tests, ruff, and their "Completion checks" in Tasks.md. Use when asked how a Person's work stands or whether it's ready to hand off or merge. Takes the Person number as an argument, e.g. /owner-check 3.
---

Report how Person `$ARGUMENTS`'s deliverables stand. If no number was given, use the Person named in `CLAUDE.local.md`; if there's none, ask. Don't change any files except creating `.venv` if it's missing.

1. **Environment.** If `.venv/` is missing, run `uv venv .venv` and then `uv pip install --python .venv/bin/python -r requirements.txt`.
2. **Ownership.** Re-read "## N. Person N" in `Tasks.md` for the owned paths. Don't rely on memory. The usual test mapping:
   - Person 1: `tests/test_geo.py tests/test_sources.py`
   - Person 2: `tests/test_simulation.py`
   - Person 3: `tests/test_advisor.py`
   - Person 4: `tests/test_ui_contract.py tests/test_end_to_end.py`
3. **Tests.** From the repo root run `.venv/bin/python -m pytest <files> -q`, never bare `pytest`. Record the pass/fail counts and the names of failing tests. For Person 4, also run `.venv/bin/python tests/fixtures/ui/trace_integration.py`. For Person 2, also run `.venv/bin/python tests/fixtures/simulation/trace_p1.py`.
4. **Lint.** Run `.venv/bin/ruff check <owned package dir and owned tests>`. Report the findings; don't fix them.
5. **Checklist.** For each item under "### Completion checks" in that Person's section, find evidence: the test function(s) that exercise it and whether they passed, or the data artifact that satisfies it.
   - Fixture data (`fixture_only=true`, `SYNTHETIC_FIXTURE`) never satisfies a real-data item.
   - For Person 1, check that `data/land/provenance.json` has a source, a hash and `fixture_only=false`, and that any saved route bundle has `source.kind=AISSTREAM_CAPTURE` with at least one route where `validation.ok=true`.
6. **Report** one table, `check | status (PASS / FAIL / NOT BUILT) | evidence`, then the test and lint summary and at most 3 bullets on the most valuable next fixes. Keep it terse.

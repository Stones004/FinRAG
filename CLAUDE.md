# FinRAG — instructions for Claude Code

Read `docs/MASTER_BUILD.md` before doing anything. It is the source of truth.
Each module has its own spec in `docs/modules/`. Progress is tracked in `docs/PROGRESS.md`.

## Rules (summary — full text in MASTER_BUILD.md §3)
1. Build one module at a time, in order M00 → M11. Never create files for a later module.
2. Before coding a module: read its spec, restate the plan in 5–10 lines, wait for approval.
3. Done = spec files exist, module tests pass, all earlier tests pass, `ruff check` passes,
   acceptance criteria demonstrated, `docs/PROGRESS.md` updated. Then stop and wait.
4. If a spec is wrong or incomplete, propose an edit to the spec first.
5. All tunables come from `config/settings.yaml` via `finrag.core.config`. No literals for
   paths, thresholds, URLs, model names, tolerances or intervals.
6. Domain lists live in registry YAMLs (`taxonomy`, `metrics`, `validation_rules`, `sections`, `intents`).
7. Never invent XBRL concept names, URLs or file formats. Use real files or leave a marked TODO and tell the user.
8. Money is `decimal.Decimal`, stored as TEXT. Never float.
9. The LLM never computes numbers. It only explains values given to it with IDs.
10. No network calls in tests. Mock adapters and the LLM client.

## Current module
See the last entry in `docs/PROGRESS.md`.

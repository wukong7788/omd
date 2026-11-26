# Plan: Upgrade `edgartools` from `5.56.0` to `6.4.0`

Status: `COMPLETED`

## 1. Objective & Scope

Upgrade the optional `sec-financials` dependency in `ohmydata` (OMD) from
`edgartools==5.56.0` to `edgartools==6.4.0` to unlock 6.x `lxml`-only performance,
improved `20-F`/`40-F` XBRL coverage, standardized concept mapping, and explicit
`edgar.exceptions` error handling for downstream consumers (`stock_notify`,
`funmoney_backtest`, and the new `funmoney_stock` repository).

Scope strictly covers:
- Dependency pin & lockfile (`pyproject.toml`, `uv.lock`);
- SEC parser-version provenance strings and runtime pin guards;
- API compatibility adaptations across `src/ohmydata/providers/sec/`;
- Offline deterministic unit/integration tests under `tests/providers/sec/`;
- Active SEC documentation (`README.md`, `docs/financial-period-integrity.md`,
  `CHANGELOG.md` `## Unreleased`).

Out of scope:
- Changing OMD's published package version (`0.4.11`) or dispatching PyPI publication;
- Modifying historical `CHANGELOG.md` entries (`<= 0.4.11`) or historical
  archived plans (`docs/plans/financial-period-integrity-and-edgartools-compatibility.md`);
- Weakening PIT `filing_date` alignment, `SecPitPolicy`, or raw XBRL unit corroboration.

## 2. Known & Audited Breaking Boundaries

1. **Statement dimension API (`Financials.*(include_dimensions=...)`)**:
   - `edgartools 5.56.0` emitted `DeprecationWarning: include_dimensions is deprecated and will be removed in v6.0. Use view='standard', 'detailed', or 'summary' instead.`
   - `src/ohmydata/providers/sec/sgml_financials.py` (`_financial_rows`) must migrate from `include_dimensions=request.include_dimensions` to the verified `6.4.0` `view` parameter (`"detailed"` when `request.include_dimensions` else `"standard"`), while verifying `_native_statement_rows` preserves exact dimensional vs non-dimensional filtering.
2. **Parser-version provenance isolation**:
   - All `edgartools-5.56.0` parser-version strings across 10 SEC modules (`unit_evidence.py`, `edgartools_adapter.py`, `financials.py`, `cli.py`, `_statement_parser.py`, `sgml_financials.py`, `observed_xbrl_financials.py`, `_observed_financial_output.py`, `xbrl_package_financials.py`, `_document_financial_contract.py`) and runtime pin checks in `sgml_financials.py` (`installed != "5.56.0"`) update to `6.4.0`.
   - Updating parser-version strings intentionally produces distinct `configuration_identity` and `evidence_identity` hashes so `5.56.0` and `6.4.0` projections never collide silently.
3. **Internal XBRL/SGML/Statement & exception boundaries**:
   - Verify `XBRLAttachments`, `XBRL`, `XBRLProcessingError`, `FilingSGML.from_text`, `Statement`, `xbrl.parser.facts`, `xbrl.parser.contexts`, `xbrl.contexts`, `xbrl.context_period_map`, `xbrl.units`, `xbrl.find_statement`, `xbrl.presentation_trees`, `statement.to_dataframe(...)` columns, and `edgar.exceptions` against `edgartools==6.4.0` before editing adapter code.

## 3. Execution Checklist

- [x] Bump `edgartools==6.4.0` in `pyproject.toml`, run `uv lock` and `uv sync --extra sec-financials`.
- [x] Introspect live `edgartools==6.4.0` APIs and confirm zero missing internal symbols.
- [x] Update parser-version identities, pin guards, and `view` / exception handling in `src/ohmydata/providers/sec/`.
- [x] Update SEC tests for `edgartools-6.4.0` and verify zero deprecation warnings.
- [x] Update `README.md`, `docs/financial-period-integrity.md`, and `CHANGELOG.md` (`## Unreleased`).
- [x] Pass all 6 canonical OMD gates (`pytest`, `ruff check`, `ruff format --check`, `ty check`, `uv build`, `git diff --check`).

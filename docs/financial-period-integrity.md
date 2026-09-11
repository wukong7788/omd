# Financial period integrity and migration

This unreleased change repairs OMD's adapters against `edgartools==5.56.0` and
keeps `yfinance==1.7.0`. It does not require an upstream upgrade. The core SDK
still has no runtime dependencies; Edgar remains in `sec-financials`.

## Yahoo financial periods

`YFinanceSymbolFundamentals.report_date` is the latest actual column date across
the supplied quarterly income, cash flow and balance sheets. It is not a filing
date or a point-in-time availability timestamp. `provider_report_date` preserves
`info.mostRecentQuarter` separately. Metadata alone cannot date financial values.

Every latest metric selects that exact report column, including null cells.
An older table without that column yields `None`, with `coverage_flags` and
`financials.metric_periods` explaining missing rows, periods or values. Each
`YFinanceMetricPeriod` identifies the source statement and row, current and prior
column dates, and native cell strings. Flattened records retain this metadata.
Consumers must check coverage before combining amounts or EPS.

Prior-year selection uses the calendar anniversary of the selected date, with
a seven-day tolerance for 52/53-week fiscal calendars and February 29 mapped to
February 28. Exactly one column must match; multiple matches raise `ValueError`.
This explicit date policy is not proof of a provider-supplied fiscal-quarter
identifier. A missing quarter or null never shifts another column into the
prior-year slot. Column order does not matter. Conflicting duplicate dates and
duplicate metric rows raise errors; undated integer-indexed series are rejected.

`operating_income_*` selects only `Operating Income`. EBIT and adjusted or
normalized earnings are not aliases for GAAP operating income. Existing separate
normalized-EPS fields remain distinguishable.

## Yahoo valuation provenance

FY1 calibration uses `info.currentPrice`, then `info.regularMarketPrice`, and
requires matching explicitly supplied quote and financial currencies. It retains
the existing EPS ratio guard as an additional fallback check, not proof of ADR
share-unit or accounting-basis compatibility. `quote_price`, `quote_source` and
`quote_time` identify the supplied quote; `regularMarketTime` is used only for
`regularMarketPrice`. A `currentPrice` timestamp remains unknown.
No freshness claim is made from a missing timestamp.

`forward_eps_period="FY1"` and `forward_eps_source="earnings_estimate.0y.avg"`
describe successful current-year calibration. FY1 is not NTM. Missing price or
unconfirmed/mismatched currencies leave `RAW_FALLBACK` and the raw provider EPS/PE;
the fallback forecast period remains unknown. `raw_forward_pe` and
`raw_forward_eps` are always preserved. The legacy `derive_implied_price` helper
remains available for compatibility, but the parser no longer uses it.

Yahoo's two EPS sources do not establish GAAP versus non-GAAP comparability.
`eps_accounting_basis` and `accounting_basis_comparability` therefore remain
`"unknown"`. Legacy `gaap_diff_pct` and `has_gaap_distortion` retain their numeric
divergence calculation and threshold for compatibility; the flag is not evidence
of a one-off gain or a known accounting basis.

## SEC schema migration

The financial dataset and vintage identity move to `sec-company-financials-v3`.
The native adapter uses presentation-tree membership and XBRL instance facts,
bypassing display-level revenue deduplication and complementary-concept merging.
Period starts, ends, instant/duration identity and dimensions belong to each fact;
quarter and YTD facts with the same end date must remain separate. Unknown unit
or duration start remains unknown. Availability continues to use filing
`accepted_at`, never the financial period end.

Typed `SecStatementRow.value` stays `Decimal`. Parquet v2 introduced, and v3
retains, `value` as a canonical decimal **string**, so arbitrary source precision
survives storage; convert with `Decimal(value)` when reading. `value_native`
separately preserves the exact source text. The old v1 fixed `decimal128(28,4)`
column is not reused. `unit_ref` retains
the native XBRL unit identifier; `unit` resolves its measure (for example
`iso4217:USD`) or the compound unit definition. An absent definition remains
unknown. `decimals_native` preserves `INF`; the integer `decimals` is then null.

`period_key`, `context_ref`, `period_source`, and the canonical dimension mapping
retain provenance. The client includes dimensions by default. Explicit
`SecFinancialsRequest(include_dimensions=False)` excludes them and adds
`DIMENSIONS_EXCLUDED_BY_REQUEST`; the standalone parser retains its legacy
default of excluding dimensions. Display-only compatibility input retains the
full column label as its key and marks its duration start unknown.

`limit` counts eligible filings per symbol after form, amendment and filing-year
filters, ordered by filing date and accession descending. A base form includes
its amendments only when `include_amendments=True`; an amendment-only form request
never substitutes the original. Each amendment remains an independent accession
and vintage. Missing financials are explicit coverage, not proof of a governance
amendment or a financial restatement; linkage to a particular original accession
and amendment purpose remain unknown unless separately established.

`quality_flags` distinguishes absent statements (`*_MISSING`), empty supplied
statements (`*_EMPTY`), parsing failures (`*_PARSE_FAILED`), no financials object,
and missing acceptance time. `SecStatementParseError` is the stable standalone
parser error. The client keeps failure flags with the selected filing rather than
silently dropping it or claiming complete coverage.

With the unreleased FactsView fix, **zero rows plus any parsing failure raises
`SecFinancialsParseError`**. Inspect `error.vintage` for the selected accession
and coverage flags, and `error.__cause__` for the original failure chain. A
nonempty vintage with `*_PARSE_FAILED` is partial coverage, not complete success.
Missing or empty financials without parsing failure still return explicit flags.
Consumers that previously accepted a nonempty vintage list must handle this
exception and check flags; do not treat parsing failures as valid N/A values.

For edgartools 5.56.0 the native access boundary is `XBRL.parser.facts`, a mapping
of original `Fact` models. `XBRL.facts` is a `FactsView`; its public `get_facts()`
creates enriched query dictionaries and can rewrite concept identifiers. OMD
therefore checks the native mapping shape and scans it once per statement, with
no enriched-view or display fallback. This lower-level dependency is intentional
and covered using the installed upstream XBRL, FactsView and Statement classes
with synthetic data; the edgartools version remains pinned.

Repeated native facts sharing a concept, context period, dimensions and unit are
validated before selection. Equal finite `decimals` values must have exactly equal
`Decimal` values; different precisions must have one common closed rounding
interval. `INF` is a singleton interval. Once the whole group is consistent, OMD
keeps the highest precision original fact independently for each context (with a
stable lexical tie break), preserving its native value, precision, unit and
context. Missing precision is accepted only when all repeated values are exactly
equal; mixing known and unknown precision is rejected because consistency cannot
be established. Unit conflicts, malformed precision, non-finite values,
arithmetic inputs beyond the bounded 10,000-digit guard, and non-intersecting
intervals remain parse errors. Affected filings must be rerun from the same
source after this repair; no automatic migration of existing data is performed.

Both tables and manifest are staged before one directory rename. An existing v2
partition is rejected by the v3 writer; changed contents require a new root. CLI
inspection and validation check actual table
schemas and required files as well as manifest versions and hashes.
The client returns uppercase symbols. Manually constructed vintages must match
the uppercase partition symbol exactly; invalid casing is rejected before writing.

SEC statement rows also expose nullable `currency`, derived only from a complete
bare uppercase three-letter unit (for example `USD`) or the exact `iso4217:`
prefix form. The original `unit` and `value_native` remain unchanged; this field
does not validate a registry, infer from concepts, or perform conversion. For
example, `unit="iso4217:USD"` yields `currency="USD"` while the Decimal amount
and native unit text remain unchanged. Existing v1 and v2 partitions must be
rebuilt into a new output root for the v3 schema.

Rebuild into a new output root. Do not relabel v1 or v2 metadata as v3 or overwrite
old evidence. Consumers must rerun same-source, same-period comparisons against
an immutable candidate build before replacing local safeguards. No consumer
migration, live validation or publication is implied by the offline repair.

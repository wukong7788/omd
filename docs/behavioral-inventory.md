# Tushare behavioral inventory (Phase 0)

This is an evidence inventory, not an SDK API. It records the calls in the
initial consumer paths and deliberately leaves unresolved differences for the
Phase 1/2 contracts. Dates shown are provider observation/request dates; this
document makes no point-in-time availability claim.

## Call inventory

| Consumer path | Endpoint and request | Current behavior and semantics | Ownership / offline evidence |
|---|---|---|---|
| `funmoney_backtest/data_provider/tushare.py` | `trade_cal(exchange="SSE", is_open="1", start_date, end_date, fields="cal_date")` | Optional batch-calendar optimization; ascending open dates, latest-window trimming, pending latest date may stop batch and fall back per symbol. Empty/missing `cal_date` disables batch. | SDK endpoint-specific calendar contract; consumer owns canonical sessions and cutoff. Offline fake-client evidence: `funmoney_backtest/tests/test_tushare_provider.py` trade-calendar/batch-cutoff/fallback cases. |
| same | `etf_basic(market="E", list_status="L", fields="ts_code,name,market,list_status,delist_date")` in explicit `all_symbols=True` mode | Required active-universe query; local filters retain `list_status == L`, empty/null `delist_date`, `market == E`, and valid `NNNNNN.SH`/`NNNNNN.SZ` symbols; absent endpoint raises `RuntimeError`; empty response or no valid active symbols also raises `RuntimeError` (no fallback). | Consumer owns explicit all-symbols selection; offline fake-client evidence: `funmoney_backtest/tests/test_tushare_provider.py::etf_basic`. |
| same | `fund_basic(market="E", status="L"), status="D", status="I"` | Queries all three statuses; absent endpoint, empty response, or missing requested symbol receives default metadata rather than failing. | Consumer owns metadata; offline fake-client evidence: `funmoney_backtest/tests/test_tushare_provider.py`. |
| same | `fund_daily(ts_code, start_date, end_date)` or per-`trade_date` batch | Raw OHLC/volume/amount retained; empty may be allowed only for delisted/out-of-window symbols, otherwise failure. Broad legacy retry. | SDK fetch semantics; consumer owns backtest schema and point-in-time transforms. Offline fake-client evidence: `funmoney_backtest/tests/test_tushare_provider.py` empty/retry/batch/per-symbol cases. |
| same | `fund_adj` with paged windows | Joined by date; funmoney deduplicates pagination keep-first; missing factors tolerated up to a consumer threshold (≤10 rows). | SDK strict policy deferred; offline fake-client evidence: `funmoney_backtest/tests/test_tushare_provider.py` paging/partial-coverage cases. |
| same | optional `fund_nav(ts_code, start_date, end_date)` | Empty/failed NAV is optional and does not fail bars; duplicate NAV dates are deduplicated. | Consumer feature enrichment; offline fake-client evidence: `funmoney_backtest/tests/test_tushare_provider.py` NAV dedup case. |
| `funmoney_backtest/data_pipeline/tushare_ext/client.py` | Injected generic endpoint caller via `RequestSpec`; `SnapshotStore` records each page | Evidence-only legacy client: unconditional generic `limit`/`offset` pagination and date-windowing; `max_retries + 1` total attempts over all exceptions with exponential backoff; `allow_empty` controls empty result; every page is snapshotted. Response/manifest writes are immutable-ish but non-atomic. | Must not be copied as the SDK API. Offline fake-client evidence: `funmoney_backtest/tests/data_pipeline/test_tushare_ext.py`. |
| `stock_notify/data/build_aetf_prices.py` | `fund_basic(market="E", status="L", fields=...)` | Fixed YAML pool; response must be non-empty and cover every requested symbol; metadata fields copied. | Consumer owns YAML universe/metadata. Evidence gap. |
| same | `fund_daily(ts_code, start_date, end_date)` | Retries exceptions and empty frames using two configured delays; required OHLC plus volume/amount columns checked; dates sorted ascending. | SDK should classify retries; consumer owns publication. Characterization fixture covers shared OHLC semantics. |
| same | `fund_adj(ts_code, start_date, end_date)` | Retries exceptions/empty; requires `adj_factor`; duplicate dates currently `drop_duplicates(keep="last")`; left join and strict rejection of any missing factor; computes `adj_close = close * adj_factor`. | Offline fake-client evidence: `stock_notify/tests/test_build_aetf_prices.py`; characterization is frozen fail-closed baseline pending Phase 3, not shared current semantics. |
| same | `trade_cal(exchange="SSE", is_open="1", start_date, end_date, fields="cal_date")` | Used to select completed dates for publication; operational cutoff is local Asia/Shanghai scheduling. | Consumer owns cutoff/publication. Evidence gap. |
| `stock_notify/data/build_etf_dividend_yield.py` | `fund_basic(market="E", status="L")` | Discovers dividend-themed ETFs by name matching; empty behavior is effectively no candidates. | Consumer owns discovery and feature semantics. Evidence gap. |
| same | `fund_nav(ts_code, start_date, end_date)` | Optional per-symbol; empty/exception logged and returns `None`; keeps `unit_nav`, `adj_nav`, dates. | Consumer owns optional feature handling. Evidence gap. |
| same | `fund_div(ts_code)` | Optional; empty/exception returns `None`; duplicate `(ex_date, div_cash)` rows dropped; ex-date used for frequency. | Consumer owns dividend feature and deduplication. Evidence gap. |
| same | `fund_share(ts_code, start_date, end_date)` | Used in Pass 1 AUM calculation; missing/empty response or missing latest `fd_share` becomes `fd_share=0`, then `aum_map=0`, potentially silently excluding funds when `min_aum_yi>0` (confirmed missing-value bug). | Consumer owns AUM filtering/publication; SDK must preserve unknown, never impute zero. Evidence gap for this branch. |
| `stock_notify/data/build_underlying_yield.py` | `fund_portfolio(ts_code)` | Latest `end_date` holdings selected; empty/exception returns `None`. | Consumer owns holdings selection. Evidence gap. |
| same | `daily_basic(trade_date, fields="ts_code,trade_date,dv_ttm")` | Empty/exception returns `None`; `dv_ttm` and `mkv` coerced; unmatched/missing `dv_ttm` filled with zero (confirmed bug), coverage is always total weight. | SDK must preserve missing values; consumer bug remains unfixed. Evidence gap. |
| `stock_notify/data/build_underlying_yield_ts.py` | `index_weight(index_code, start_date=YYYYMM01, end_date=trade_date)` | Latest returned snapshot selected; weights coerced from percent to decimal; empty/exception returns `None`. | Consumer owns monthly window and index semantics. Evidence gap. |
| same | `daily_basic(trade_date, fields="ts_code,trade_date,dv_ttm")` | Missing/unmatched `dv_ttm` filled with zero (confirmed bug); weighted sum returned without finite-value coverage check. | SDK must not impute; consumer fix is separate. Evidence gap. |
| `stock_notify/data/build_index_proxy_prices.py` | Reuses adjusted-price retrieval (`fund_daily` + `fund_adj`, and calendar path) | Pool selection and output paths/publication remain consumer-owned; inherits adjusted-bar retry/dedup behavior. | SDK recipe eventually; no additional endpoint contract. Evidence gap. |

## Cross-cutting semantics

- Request fields, endpoint windows, ordering and units remain provider/endpoint
  concerns. Consumers own final schemas, storage, universes, and operational
  cutoffs. Observation dates alone do not establish when data was available.
- Raw OHLC and raw `adj_factor` must remain distinguishable from the derived
  `adj_close`; no missing numeric value may become zero in SDK code.
- Official Tushare documentation reviewed on 2026-07-31 defines
  `fund_daily.vol` as hands and `fund_daily.amount` as thousand yuan.
  funmoney's multiplication of `amount` by 1000 is therefore an explicit
  consumer conversion to yuan, while stock_notify retains the provider value.
  It also defines `daily_basic.dv_ttm` and `index_weight.weight` as percent,
  `fund_portfolio.mkv` as yuan (not 万元), and `fund_share.fd_share` as
  ten-thousand shares. The current stock_notify comment that treats
  `fund_portfolio.mkv` as 万元 is a confirmed unit-label bug; its relative
  weight calculation is scale-invariant, but the label must not be copied.
- Empty responses are endpoint/request policy decisions. Optional enrichment
  calls may return no feature, while required symbol history or factor coverage
  fails closed.
- Legacy consumers use Pandas/Polars, but the Phase 0 package has no dataframe
  runtime dependency and performs no provider calls.

## Conflict and evidence-gap table

| Conflict / gap | Evidence | Phase 0 disposition |
|---|---|---|
| Broad exception + empty-response retry versus classified transient-only retry | Both backtest and AETF helpers retry broad exceptions and empties | Record conflict; do not implement retry in Phase 0. |
| Generic `limit`/`offset` pagination versus endpoint-specific windows | `funmoney_backtest/data_pipeline/tushare_ext/` generic pagination; AETF uses date windows | Record conflict; endpoint contracts deferred to Phase 2. |
| Adjustment-factor strictness disagreement | AETF rejects any missing factor; backtest tolerates up to 10 rows | Characterization is frozen fail-closed baseline pending Phase 3 resolution; no public recipe yet. |
| Duplicate adjustment dates | funmoney keeps first; stock_notify keeps last | Characterization rejects duplicates; Phase 3 must resolve recipe policy. |
| Operational cutoff disagreement | Backtest trims latest batch date; stock_notify uses local scheduled end date | Consumer-owned; no SDK cutoff selected. |
| `dv_ttm -> 0` and false coverage | Both underlying-yield scripts fill missing values with zero and sum all weights | Confirmed consumer bugs; inventory documents them, does not repair them. |
| AUM missing-value imputation | ETF dividend Pass 1 maps missing/empty `fund_share` or missing latest `fd_share` to zero AUM | Confirmed consumer bug; must remain unknown in SDK and be fixed in a later consumer task. |
| Parameter-dropping retry | funmoney `_fetch_frame` catches `TypeError` then retries minimal kwargs, dropping dates/fields | Consumer date/field semantic drift; migration blocker, not inherited by Phase 2. |
| NaN adjustment coverage | funmoney `_coerce_float` may retain float NaN while strict coverage checks only `is not None` | Confirmed missing-value classification bug; SDK must use finite/null semantics. |
| Non-atomic legacy snapshot writes | `tushare_ext` snapshot implementation writes response/manifest non-atomically | Phase 1 snapshot contract must replace it; no snapshot code here. |
| Credential discovery | Consumers load token/config from `.env`/environment | Migration evidence only; SDK accepts injected token/client and never discovers credentials. |
| Offline behavioral evidence | `funmoney_backtest/tests/test_tushare_provider.py` and `stock_notify/tests/test_build_aetf_prices.py` use offline fake clients and cover substantial behavior | Inventory cites matching tests; only uncovered endpoint/unit/cutoff details remain evidence gaps. |

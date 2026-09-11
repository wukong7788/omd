# US pilot instrument identity qualification

Status: partial source research; no complete universe qualification or provider
alias history accepted. Checked 2026-09-12 Asia/Shanghai. The
[production plan](pit-data-production-and-event-refresh.md) owns the frozen
candidate universe and expansion limits. This document contains public source
conclusions only, not the private candidate list or downloaded payloads.

## Special symbols

| Candidate | Evidence | Required treatment |
| --- | --- | --- |
| VIX | Cboe describes VIX as its volatility benchmark index; listed futures/options are separate products. | Index, not a corporate equity or ETF. Corporate financial metrics are not applicable. A vendor alias and its time coverage still need explicit validation. |
| SKHY | Nasdaq identifies SK hynix American Depositary Shares; the issuer announces Nasdaq ADR trading started July 10, 2026. | US depositary security distinct from Korean ordinary shares and Luxembourg depositary shares. Do not backfill its US history before listing or infer an ADR ratio. Final depositary terms, provider mapping and SEC form coverage remain required. |
| SPCX | Nasdaq identifies Space Exploration Technologies Class A common stock. A separate SEC supplement says The SPAC and New Issue ETF changed from SPCX to SPCK effective April 7, 2026. | Ticker reuse across unrelated instruments. Never concatenate the old ETF history with the current common stock. Keep effective-dated security identities and validate vendor history before accepting a panel. |

Primary references:

- [Cboe volatility products](https://www.cboe.com/tradable-products/volatility-trading/)
- [Nasdaq SKHY security page](https://www.nasdaq.com/market-activity/stocks/skhy)
- [SK hynix July 10 listing announcement](https://news.skhynix.com/en/skhynix-lists-adrs-on-nasdaq/)
- [Nasdaq SPCX security page](https://www.nasdaq.com/market-activity/stocks/spcx)
- [SEC April 2 ETF ticker-change supplement](https://www.sec.gov/Archives/edgar/data/1719812/000199937126007611/cist-497_040226.htm)

These current web observations are research references, not retained SDK source
receipts or proof that a mapping was known at a past cutoff. A listing headline
does not establish provider price coverage, a complete effective interval,
currency, depositary ratio, or availability of supported financial forms.
Unresolved fields remain unqualified. No candidate is silently substituted,
no foreign ordinary-share price is used as a US ADS price, and no financial
PASS or historical PIT eligibility is assigned by this research.

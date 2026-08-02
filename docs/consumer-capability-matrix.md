# OMD / Stock Notify / Funmoney Backtest Capability Matrix

Status: `CURRENT-STATE EVIDENCE — 2026-08-01`

本文回答两个问题：

1. OMD 现在已经实现什么，`stock_notify` 和 `funmoney_backtest` 实际各自使用什么；
2. 长期哪些能力值得进入 OMD，共用边界应放在哪里，哪些看起来相同的功能其实调用了不同 API 或拥有不同语义。

本文是代码盘点和路线输入，不替代 `PLAN.md`、provider contract 或消费者自己的
schema/PIT/运行契约。状态以当前工作区代码为准；“候选共用能力”不等于已授权开发。

## 1. 一句话现状

| 项目 | 当前 dataframe 主边界 | 当前 Tushare 接入 | OMD 使用状态 |
|---|---|---|---|
| OMD | Tushare provider-native Pandas；core 与 dataframe 解耦；可选显式 Polars adapter | typed request/result、retry、rate limit、error、provenance、recipe | SDK 本体 |
| Stock Notify | Pandas | 已把当前生产数据脚本迁到 `ohmydata[tushare]==0.0.4`；凭据和 client 构造仍由消费者负责 | 已使用 OMD 11 个 typed endpoint 中的 10 个和 3 个 recipe 路径；未使用 `etf_basic`、OMD snapshot/Polars adapter |
| Funmoney Backtest | Polars | 主线 `data_provider/tushare.py` 直接包装官方 client；研究线另有 `data_pipeline/tushare_ext/` | 尚未依赖 OMD；现有能力是迁移对照基线 |

目标边界不是把两个消费者的数据管线都搬进 OMD，而是：

```text
provider-native 请求、分页、重试、错误、原始单位、provenance、通用纯 recipe
                              -> OMD

universe、PIT/as-of、消费者 schema、存储、调度、特征、回测、信号、UI
                              -> 各消费者
```

## 2. OMD 当前已经实现的能力

### 2.1 Core

| 能力 | 当前公开 API | 已实现的关键语义 | 当前消费者使用情况 |
|---|---|---|---|
| 请求身份 | `RequestSpec` | canonical parameters/fields、SHA-256 request identity、拒绝 secret-bearing key | Stock Notify 通过 typed request 间接使用；funmoney 有自己的 `tushare_ext.RequestSpec`，不是同一类型 |
| 错误分类 | `AuthenticationError`、`PermissionDeniedError`、`RateLimitError`、`TransientProviderError`、`EmptyResponseError`、`SchemaMismatchError`、`PaginationError`、`CoverageError` 等 | permanent/transient 分离，确定性错误不重试 | Stock Notify 已采用；funmoney 主线多为 `RuntimeError`/`ValueError` 与 broad retry |
| Retry | `RetryPolicy`、`execute_with_retry` | `max_attempts` 表示总尝试次数；sleep/jitter 可注入 | Stock Notify 已采用 3 次总尝试；funmoney 主线是自有固定次数，`tushare_ext` 是 `max_retries + 1` |
| Rate limit | `RateLimitPolicy`、`RateLimiter` | instance-scoped、无隐藏全局凭据 | Stock Notify 已采用；funmoney 使用显式 sleep/自有 request delay |
| Provenance | `FetchProvenance`、`EmptyDisposition` | effective request、attempt、rows/columns、retrieval time、warnings、snapshot IDs | Stock Notify 获取 result 时存在，但业务代码主要消费 `.frame`；funmoney 使用自有 snapshot/metadata |
| Snapshot | `SnapshotStore`、`SnapshotMode.APPEND/FROZEN` | atomic、immutable、hash/replay/integrity validation | 两个消费者当前均未通过 OMD typed client 使用；funmoney 研究线有另一套 `tushare_ext.SnapshotStore` |

### 2.2 Tushare typed endpoint

OMD 当前公开 11 个 typed endpoint。返回值都是防御性复制的 provider-native Pandas
frame；日期字符串、字段和单位不会被隐式归一化。

| Tushare endpoint | OMD request / client method | Stock Notify | Funmoney Backtest 当前路径 |
|---|---|---|---|
| `trade_cal` | `TradeCalendarRequest` / `fetch_trade_calendar` | 已直接使用，解析最新可用 A 股 session | 直接 `client.trade_cal(...)`；用于批量日线和 21:00 截止逻辑 |
| `fund_basic` | `FundBasicRequest` / `fetch_fund_basic` | 已直接使用：固定池 metadata、红利 ETF 名称发现、benchmark 映射 | 直接 `client.fund_basic(...)` 查询 `L/D/I`；另用于 universe/clone research |
| `etf_basic` | `EtfBasicRequest` / `fetch_etf_basic` | 未使用 | 待迁移：`all_symbols` active ETF metadata；universe filtering remains consumer-owned |
| `fund_daily` | `FundDailyRequest` / `fetch_fund_daily` | 通过 `fetch_adjusted_etf_bars` 间接使用 | 直接 `client.fund_daily(...)`；主线支持按 symbol 或按 `trade_date` 批量 |
| `fund_adj` | `FundAdjustmentRequest` / `fetch_fund_adjustment` | 通过 adjusted-bars recipe 间接使用 | 直接 `client.fund_adj(...)`，自有滚动窗口和 keep-first 去重 |
| `fund_nav` | `FundNavRequest` / `fetch_fund_nav` | 已直接使用，服务基金分红率/NAV 数据 | 直接调用；主 provider 作为可选 enrichment，研究线还用于 ETF premium |
| `fund_share` | `FundShareRequest` / `fetch_fund_share` | 已直接使用，服务 AUM；缺失保持 unknown | 研究数据合同直接调用，用于 share-flow/PIT panel |
| `fund_div` | `FundDividendRequest` / `fetch_fund_dividend` | 已直接使用，服务 ETF 分红事件和频率 | 当前主要研究事件用的是股票 `dividend`，不是 `fund_div` |
| `fund_portfolio` | `FundPortfolioRequest` / `fetch_fund_portfolio` | 已直接使用，按有界年度选择 ETF 持仓报告 | 当前代码未使用该 endpoint |
| `daily_basic` | `DailyBasicRequest` / `fetch_daily_basic` | 已直接使用，获取成分股 `dv_ttm` | 研究线通过自有 generic client 获取并转为 PIT Polars schema |
| `index_weight` | `IndexWeightRequest` / `fetch_index_weight` | 已直接使用，构造月度指数底层收益率 | 研究线通过自有 generic/direct client 获取并转为 PIT Polars schema |

### 2.3 Provider-semantic recipes 与 dataframe adapter

| OMD 能力 | API | Stock Notify | Funmoney Backtest |
|---|---|---|---|
| ETF 复权日线 | `build_adjusted_etf_bars`、`fetch_adjusted_etf_bars` | 已使用 `STRICT`；输出再映射到 Stock Notify Pandas schema | 尚未使用；自有 Polars join、单位转换和缺失因子阈值 |
| ETF 持仓加权股息率 | `build_portfolio_dividend_yield` | 已使用 `NORMALIZE_SUPPORTED`，并由消费者执行 `coverage > 99%` gate | 当前没有 `fund_portfolio` 路径 |
| 指数成分加权股息率 | `build_index_dividend_yield` | 已使用 `NORMALIZE_SUPPORTED`，由消费者选择最新 snapshot 和百分比输出边界 | 尚未使用；自有 PIT Polars aggregation，包含 session/staleness/coverage 规则 |
| Pandas → Polars | `pandas_to_polars` | 当前不需要，Stock Notify 仍以 Pandas 为主 | 已在 OMD 实现但 funmoney 尚未接入；这是后续 shadow migration 的 representation boundary |
| Polars → Pandas | `polars_to_pandas` | 当前未使用 | 当前未使用；主要用于显式 round-trip/parity，不应成为正常 ingestion 的往返路径 |

## 3. 两个消费者各自保留的功能

### 3.1 Stock Notify 自己拥有

| 功能 | 当前实现/API | 为什么不属于 OMD |
|---|---|---|
| A 股 ETF universe | 固定 YAML pool；另有 `fund_basic` 名称关键词发现红利 ETF | universe、名称规则和产品筛选是应用定义 |
| 最新数据日 | OMD `trade_cal` + Stock Notify 15:30 Asia/Shanghai 数据就绪时间 + 保守 fallback | cutoff、调度和 freshness 是消费者运行策略 |
| 价格发布 | Pandas schema、逐 symbol Parquet、DuckDB 检查、staging、锁和 atomic directory swap | 存储路径和发布事务属于应用 |
| 基金现金分红率 | `fund_div + fund_nav`，滚动 365 日现金分红 / `unit_nav` | 这是 Stock Notify 的产品指标，不等于 OMD 的成分股 `dv_ttm` 加权 recipe |
| AUM | `fund_share.fd_share × unit_nav / 10000`，缺失保持 unknown | 输出单位、日期对齐和筛选 gate 属于应用 |
| ETF 持仓报告选择 | bounded `fund_portfolio` 查询后按 `ann_date/end_date <= as_of` 选择最新报告 | OMD 只保证 provider request/原始单位；PIT/as-of 选择属于消费者 |
| 指数 snapshot 选择 | 当月窗口中选择最新 `index_weight.trade_date` | 选择窗口、as-of 和发布日期属于消费者 |
| 净收益与评分 | gross yield 减 expense、clip、coverage、stability、cashflow score | 产品特征/UI 逻辑，不是 ingestion infrastructure |
| 数据刷新和 UI | cache、Parquet、DuckDB、Electron/frontend 调度与展示 | 应用运行和发布责任 |

### 3.2 Funmoney Backtest 自己拥有

| 功能 | 当前实现/API | 为什么不属于 OMD，或为什么尚待迁移 |
|---|---|---|
| Polars provider contract | `DataProviderRequest`、`DataProviderResult`、`RAW_BAR_SCHEMA`、`QUALITY_SCHEMA` | 回测 normalized schema 和质量报告属于消费者；后续只替换 provider fetch boundary |
| Universe | explicit symbols、YAML、`all_symbols=True`；`etf_basic` active ETF 过滤；研究线还有 clone map | universe、survivorship policy 和研究池定义属于消费者；但 `etf_basic` endpoint contract 可进入 OMD |
| 日线批量策略 | `trade_cal` 后按 `trade_date` 拉全市场，再按 symbol fallback；21:00 cutoff | batch/cutoff 是 funmoney 操作策略，不能用 Stock Notify 的 15:30 规则替换 |
| 原始 bar 归一化 | `trade_date→date`、`vol→volume`、`amount × 1000` 转元、NAV join、quality rows | 最终字段和单位是回测 schema；Stock Notify 保留 provider-native `amount` 千元，不能共用同一输出 schema |
| 复权因子容忍 | `strict_adj_factor=True` 时仍允许最多 10 行缺失，超过才失败 | 与 OMD `STRICT` 不同；shadow parity 期间必须显式保留，之后才能单独决定是否改行为 |
| Incremental/Parquet | 7 日 refresh lookback、keep-last incremental merge、bars/quality/summary artifacts | 数据集发布和增量策略属于消费者 |
| Research generic client | `TushareExtClient.fetch(RequestSpec)`，generic limit/offset、date windows、snapshot-per-page | 是待替换的 legacy research infrastructure；不能直接当 OMD 公共 API |
| PIT index weights | `index_weight` → `weight_decimal` → first usable canonical session | PIT 可用性、session 对齐和 staleness 属于消费者 |
| PIT constituent yield | `daily_basic.dv_ttm` → decimal，绑定 snapshot/retrieval，检查 provider revision semantics | OMD recipe明确不声称 PIT；这些规则必须留在 funmoney |
| PIT weighted aggregate | snapshot selection、最大 stale sessions、minimum coverage、最终 feature date | 虽然数学上也叫 weighted dividend yield，但比 OMD pure recipe 多一层消费者时间语义 |
| ETF premium | `fund_daily + fund_nav`，含 announcement-aligned/lagged 版本 | feature/PIT 对齐是研究定义；底层 typed endpoint 已存在 |
| PCF/share-flow | `fund_share`、`etf_sh_cons`/`etf_sz_cons` | `fund_share` 已有 OMD endpoint；PCF endpoints 尚未实现，最终 panel/PIT 仍属消费者 |
| Constituent events | 股票 `dividend` endpoint + null-yield/PIT calibration | 不是 Stock Notify 使用的基金 `fund_div`；OMD 当前也未实现股票 `dividend` typed API |
| Backtest/signal/live | feature、strategy、accounting、signal export、broker/live | 永远不进入 OMD provider SDK |

## 4. 看起来一样，但 API 或语义并不一样

这是迁移时最容易产生隐式漂移的部分。

| 看起来相同的功能 | Stock Notify | Funmoney Backtest | 不能直接合并的原因 |
|---|---|---|---|
| “ETF 基础信息” | `fund_basic(market="E", status="L")`；metadata、名称关键词、benchmark | 主线 `etf_basic(market="E", list_status="L")` 找 active ETF，同时 `fund_basic` 查 `L/D/I` metadata；clone map 还组合两者 | `fund_basic` 与 `etf_basic` 是不同 endpoint、字段和 coverage 语义；OMD 已实现 `etf_basic typed API`，但 funmoney 尚未迁移 |
| “ETF universe” | 固定 YAML + 红利名称发现 | explicit/YAML/all-symbol + clone/research universe | universe policy 不应由 provider SDK 统一 |
| “复权价格” | OMD strict recipe；Pandas；仅发布 `adj_close`；`amount` 保持千元 | Polars 自有 join；保留 `adj_factor`；`amount × 1000` 转元；最多容忍 10 个 missing factors | raw API 相同，但 coverage、单位和输出 schema 不同 |
| “最新交易日” | OMD `trade_cal`，15:30 数据 ready，60 日窗口，部分失败保守 fallback | direct `trade_cal`，21:00 cutoff，支持全市场按日 batch/fallback | cutoff 和失败后的动作不同，不能抽成一个默认规则 |
| “NAV” | OMD `fund_nav`；用于基金分红率并拒绝重复 `(ts_code, nav_date)` | direct `fund_nav`；主 provider 作为 optional enrichment 并按 date 去重；研究线用于 premium/PIT lag | 同一 endpoint 的 optional/required、去重、ann-date 语义不同 |
| “分红事件” | 基金 `fund_div`：ETF 自身现金分红、频率和 TTM distribution yield | 股票 `dividend`：指数成分股事件，用于 `dv_ttm` null/PIT 校准 | endpoint、主体、字段和经济含义都不同；名称相似不代表可复用同一 request |
| “底层股息率” | `fund_portfolio + daily_basic` 或 `index_weight + daily_basic`，OMD 只算选定 snapshot 的纯加权值 | `index_weight + daily_basic`，自有 first-usable-session、staleness、revision/PIT gate | OMD recipe 可复用数学部分，但 funmoney 时间对齐必须保留 |
| “coverage” | `finite_weight_coverage > 99%` 后才发布 normalized-supported estimate | 最低权重 coverage 通常 95%，还要求 snapshot/staleness/PIT valid | 阈值和是否允许 renormalize 是消费者决策，不能藏进 adapter |
| “snapshot” | 应用 publication 使用 staging/atomic swap；typed fetch 当前不落 OMD snapshot | `tushare_ext` 对每页 raw response 建自有 snapshot，研究 artifact 再绑定 hash | publication snapshot 与 provider raw snapshot 不是同一种资产 |
| “重试次数” | OMD `RetryPolicy(max_attempts=3)`，仅 classified transient | 主 provider 的 `fetch_retry_count=3`，broad exception/selected empty retry；研究线 `max_retries + 1` | 同一个数字 `3` 在三处含义不同 |
| “空数据” | request 逐项选择 `EmptyPolicy.ALLOW/ERROR`，消费者再决定 skip/fallback | optional flag、retry-on-empty、active/delisted symbol 判断、research generic `allow_empty` | empty 是 endpoint + request + consumer context 的组合，不存在一个全局默认值 |

## 5. 长期 OMD 共用能力路线

优先级只表示建议顺序。每项在实现前仍需单独冻结字段、单位、日期、分页、empty、
permission、duplicate 和 coverage contract。

| 优先级 | 候选能力 | 当前证据/调用者 | OMD 应拥有 | 消费者继续拥有 | 判断 |
|---|---|---|---|---|---|
| 已完成 | 显式 Pandas↔Polars adapter | OMD provider 是 Pandas；funmoney contract 是 Polars | representation-only conversion、dtype/null/timezone fail-closed | funmoney schema/单位/特征 | 已实现，待不可变 release 与 funmoney offline shadow |
| 已完成 | `etf_basic typed API` | funmoney `all_symbols` 和 clone map 的迁移需求；Stock Notify 的 `fund_basic` 不能替代 | typed request/result、字段、key、排序、empty、permission、coverage | active universe filter、all-symbol mode、clone grouping | 已实现；必须作为独立 endpoint，不能伪装成 `fund_basic` |
| P0 | funmoney offline shadow provider | 两个消费者最终都应走同一 SDK fetch contract | OMD typed fetch + Polars adapter + provenance | `DataProviderResult`、amount 转元、21:00 cutoff、缺失因子阈值 | 不是新增通用算法，但是真正验证共用性的下一 Gate；A 股实盘切换仍禁止 |
| P1 | typed fetch 与 OMD snapshot 集成 | OMD core snapshot 已存在；funmoney 研究线需要 raw snapshot/replay；Stock Notify 也需要可复现 fetch evidence | response serialization identity、manifest、atomic write/replay、provenance snapshot IDs | snapshot 保存策略、路径、保留期、发布 | 有共用价值；需先冻结 Pandas serialization 和 licensed-data policy |
| P1 | 多 symbol adjusted-bars orchestration | Stock Notify 与 funmoney 都逐 symbol 拉 `fund_daily + fund_adj` | provider-safe request batching、限速、每 symbol 结果/错误汇总 | universe、cutoff、partial-batch publication 和最终 schema | 有两个具体调用者；不能把任一消费者的 cutoff/容错作为默认 |
| P1 | 股票 `dividend typed API` | funmoney constituent event/PIT calibration 的近期迁移需求 | endpoint fields、pagination、revision identity、empty/error | event eligibility、PIT availability、null-yield interpretation | 与 `fund_div` 分开设计；先确认 revision/公告日期 contract |
| P1/P2 | `etf_sh_cons` / `etf_sz_cons` typed API | funmoney PCF/share-flow 数据合同 | exchange-specific endpoint contract、字段/单位/key/pagination | business-date lag、panel schema、share-flow feature | provider 能力可进入 OMD；当前只有一个明确消费者，先按迁移需要窄实现 |
| P2 | adjusted constituent stock bars / `pro_bar` capability | funmoney component diagnostics 使用 `pro_bar(..., adj="hfq")` | Tushare-specific request、adjustment provenance、raw/derived traceability | component universe、coverage artifact、研究用途 | 不应复用 ETF `fund_daily + fund_adj` recipe 名义；需单独 contract |
| P2 | 基金 distribution-yield recipe | Stock Notify 当前实现 `fund_div + fund_nav` TTM yield | 只有当第二个调用者共享 rolling window、NAV、revision、missing semantics 时才提取纯 recipe | discovery、AUM、expense、publication | 现在不要因名称都叫“股息率”就与 funmoney 的 constituent `dv_ttm` 聚合合并 |
| P2 | ETF premium provider-semantic recipe | funmoney 使用 `fund_daily + fund_nav` | 最多提取无 PIT 主张的纯价格/NAV arithmetic | announcement alignment、availability lag、feature schema | 当前缺第二个共享调用者；先保留在 funmoney |
| 不进入 OMD | PIT/canonical-session alignment | funmoney 专用且依赖决策日、session、staleness 和 revision 假设 | 只保留真实 provenance/observation date | first usable session、as-of、no-look-ahead gate | 明确消费者责任 |
| 不进入 OMD | universe、storage、publication、strategy/live | 两个消费者定义均不同 | 无 | 全部 | 永久边界，不以“减少重复”为由上移 |

## 6. 推荐执行顺序

1. 发布包含 `ohmydata[polars]` 的不可变 OMD 版本；不要让 funmoney 依赖 editable
   workspace。
2. 在 funmoney 增加非默认、非实盘的 OMD-backed shadow provider；同一 fake response
   同时运行 legacy Polars path 与 OMD Pandas→Polars path。
3. 分别比较 request、字段、分页、schema、单位、null/NaN、排序、error、quality、
   Parquet、回测与信号；有差异时先分类为 representation、已知语义冲突或缺陷修正。
4. 只有离线和授权只读 shadow 的 drift 报告被接受后，才单独规划 A 股实盘切换。
5. `dividend`、PCF、`pro_bar`、snapshot integration 按实际迁移调用者逐项冻结，禁止
   一次性复制 funmoney 的 generic `fetch(endpoint, params)` 到 OMD。

## 7. 代码证据索引

### OMD

- `src/ohmydata/core/`
- `src/ohmydata/providers/tushare/client.py`
- `src/ohmydata/providers/tushare/endpoints.py`
- `src/ohmydata/providers/tushare/recipes/`
- `src/ohmydata/adapters/polars.py`

### Stock Notify

- `stock_notify/data/build_aetf_prices.py`
- `stock_notify/data/build_etf_dividend_yield.py`
- `stock_notify/data/build_underlying_yield.py`
- `stock_notify/data/build_underlying_yield_ts.py`
- `stock_notify/data/add_div_frequency.py`
- `stock_notify/docs/plans/omd_stock_notify_migration.md`

### Funmoney Backtest

- `funmoney_backtest/data_provider/base.py`
- `funmoney_backtest/data_provider/tushare.py`
- `funmoney_backtest/data_pipeline/parquet_pipeline.py`
- `funmoney_backtest/data_pipeline/tushare_ext/`
- `funmoney_backtest/data_pipeline/etf_iter04_contract.py`
- `funmoney_backtest/data_pipeline/cn_iter05_4/`
- `funmoney_backtest/config/data/cn_research_datasets.yaml`

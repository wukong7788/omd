# Oh My Data (OMD)

[English](README.md) | [中文说明](README_zh.md)

> 离线优先、严格抵御前视偏差（Anti-Lookahead）、零凭据侵入的金融与另类市场数据采集 SDK & CLI。

`ohmydata` 是为量化投研与严肃回测设计的高可靠市场数据摄取基础设施。核心库保持零外部依赖，各数据源适配器严格采用显式注入机制，外部调用者自行持有官方客户端与凭据，库内绝不触碰任何敏感信息或私有环境。

---

## 目录

- [核心设计原则与不变量](#核心设计原则与不变量)
- [环境要求与安装 (Extras 选型)](#环境要求与安装-extras-选型)
- [OMD 命令行工具集 (CLI)](#omd-命令行工具集-cli)
  - [1. SEC N-PORT 基金季度持仓批处理 (`omd sec nport`)](#1-sec-n-port-基金季度持仓批处理-omd-sec-nport)
  - [2. SEC EDGAR 公司财报 Point-in-Time 抽取 (`omd sec financials`)](#2-sec-edgar-公司财报-point-in-time-抽取-omd-sec-financials)
  - [3. yfinance 跨版本零漂移审计 (`omd audit-drift`)](#3-yfinance-跨版本零漂移审计-omd-audit-drift)
- [Provider SDK 使用指南](#provider-sdk-使用指南)
  - [1. yfinance Provider（美股/全球行情与基本面）](#1-yfinance-provider美股全球行情与基本面)
  - [2. SEC EDGAR & N-PORT Provider（美股财报与基金持仓）](#2-sec-edgar--n-port-provider美股财报与基金持仓)
  - [3. Tushare Provider（A 股与中国 ETF 行情与事实）](#3-tushare-providera-股与中国-etf-行情与事实)
- [核心工程机制与不可变快照 (Core Architecture)](#核心工程机制与不可变快照-core-architecture)
- [高性能 Dataframe 适配器 (Polars & Pandas)](#高性能-dataframe-适配器-polars--pandas)
- [本地开发、质量门禁与文档索引](#本地开发质量门禁与文档索引)

---

## 核心设计原则与不变量

在量化研究中，垃圾数据输入与前视偏差（Look-ahead Bias）是导致策略实盘溃败的主要元凶。OMD 在架构层面固化了以下不可违背的不变量：

1. **凭据显式注入，零隐式访问**：核心库与各 Provider 绝不读取 `.env`、系统环境变量或用户配置文件；凭据与授权客户端必须由外部显式传入，保证生产与离线测试边界分明。
2. **严禁静默假定与虚假填充（No Silent Imputation）**：缺失数据绝不静默补 `0.0` 或假定值。未获取或非法的字段必须保留真实空状态（`None` / `NaN`），防止策略将“数据缺失”误判为真实价格或分红。
3. **Point-in-Time (PIT) 强闭环防御**：严格区分“数据生成日期”（如财报报告期、交易日）与“数据对外界公开可用时间”（如 SEC EDGAR `accepted_at`、采集回执时间）。非官方带毫秒级凭证的时间戳一律标记为保守不可信状态（`PIT_UNPROVEN`），杜绝时间旅行。
4. **零漂移治理（Zero-Drift Governance）**：上游第三方库版本升级极其容易引发隐式行为突变（如列名变化、价格修复算法篡改历史值）。OMD 对 `yfinance` 等上游实施版本绝对锁定（`yfinance==1.7.0`），并在升级前强制执行历史全周期位级（Bit-exact）零漂移审计门禁。
5. **不可变快照与全链路血缘追溯**：内置基于内容哈希的不可变快照存储（`SnapshotStore`），原生支持落盘回执记录、网络请求身份指纹生成与百分之百确定性的离线重放。

---

## 环境要求与安装 (Extras 选型)

* **Python 版本要求**：Python 3.11 或 3.12（`>=3.11,<3.13`）
* **核心零依赖**：`ohmydata` 核心机制（限流、重试、快照、血缘凭据）不引入任何第三方依赖。
* **按需安装 Extras**：根据投研场景选择安装对应的扩展包：

| Extra 扩展名 | 核心功能 | 核心依赖 | 适用场景 |
| :--- | :--- | :--- | :--- |
| `ohmydata[yfinance]` | 美股及全球行情日线、估值快照、财务对比、分析师预期、跨版本零漂移审计 | `pandas`, `yfinance==1.7.0` | 美股、全球 ETF、全球宏观标的回测与选股 |
| `ohmydata[sec-cli]` | SEC EDGAR N-PORT 季度基金持仓批量流构建、校验与合格评定 CLI | `pyarrow` | 美股 ETF / 共同基金底层持仓穿透与机构跟踪 |
| `ohmydata[sec-financials]` | SEC EDGAR 10-K / 10-Q 三大表（资产负债、利润、现金流）PIT 抽取与 Parquet 本地湖 | `edgartools`, `pyarrow` | 美股上市公司财务基本面、财报季量化研究 |
| `ohmydata[tushare]` | A 股与国内 ETF 日线行情、复权因子、PCF 申赎清单、分红事实、申万行业 | `pandas`, `tushare` | A 股股票、ETF 资产配置与事件驱动策略 |
| `ohmydata[polars]` | 强校验、高保真的 Pandas ↔ Polars 双向零损转换适配器 | `pandas`, `polars`, `pyarrow` | 现代 Polars 高性能分析流与特征工程 |
| `ohmydata[vintage-plane]` | ETF 基准指数与成份股多维历史切片（Vintage Plane）装配器 | `pandas`, `pyarrow` | 历史指数权重切片重构与基准深度穿透 |

### 快速安装

```bash
# 安装基础美股与财报支持
pip install "ohmydata[yfinance,sec-financials]"

# 或使用 uv 进行精确依赖锁定
uv add "ohmydata[yfinance,sec-cli]"
```

源码检出开发：

```bash
git clone https://github.com/wukong7788/omd.git
cd omd
uv sync --all-extras
uv run python -c "import ohmydata; print(ohmydata.__version__)"
```

---

## OMD 命令行工具集 (CLI)

OMD 提供了完备的命令行工具，开箱即用支持大规模批量数据的提取、离线构建与审计。

### 1. SEC N-PORT 基金季度持仓批处理 (`omd sec nport`)

支持获取全美合法注册的公募基金与 ETF 季度持仓明细（包含 CIK、Series、Class、具体证券持仓与权重）：

```bash
# 1. 查看处理计划 (Plan)
uv run omd sec nport plan --config artifacts/sec-sync.yaml

# 2. 批量同步季度数据 (Sync：流式下载、增量重试并构建本地 Parquet 湖)
uv run omd sec nport sync \
  --quarters full \
  --root artifacts/sec-nport \
  --universe artifacts/sec-equity-etfs.json \
  --user-agent-file /path/to/sec-contact.txt \
  --availability-policy accepted-at-plus-lag \
  --lag-days 0

# 3. 校验本地数据湖的完整性与 SHA-256 校验和 (Validate)
uv run omd sec nport validate --config artifacts/sec-sync.yaml

# 4. 检查具体季度与标的持仓明细 (Inspect)
uv run omd sec nport inspect --root artifacts/sec-nport --quarter 2026q1 --symbol SPY --rows

# 5. 执行结构化独立合格评定 (Qualify：基于源码重放比对数据湖与事实表)
uv run omd sec nport qualify \
  --quarters 2026q1 \
  --root artifacts/sec-nport \
  --universe artifacts/sec-equity-etfs.json \
  --output artifacts/sec-qualified
```

> **提示**：命令行支持 `--config FILE`（`.yaml`, `.json`, `.toml`），避免在终端中反复书写长参数；联系人信息严格从外部文件或标准输入传入，杜绝硬编码。

---

### 2. SEC EDGAR 公司财报 Point-in-Time 抽取 (`omd sec financials`)

直接从 SEC EDGAR 抽取上市公司 10-K（年报）与 10-Q（季报）三大财务报表（资产负债表、利润表、现金流量表）：

```bash
# 1. 根据配置文件批量拉取财报
uv run omd sec financials sync --config artifacts/sec-financials.yaml

# 2. 或直接指定标的与年份范围
uv run omd sec financials sync \
  --symbols "AAPL,MSFT,NVDA" \
  --forms "10-K,10-Q" \
  --root artifacts/sec-financials \
  --user-agent-file /path/to/sec-contact.txt \
  --start-year 2020 \
  --end-year 2026

# 3. 检查本地 Parquet 分区中的最新财报条目
uv run omd sec financials inspect --root artifacts/sec-financials --symbol AAPL --rows

# 4. 校验本地 Parquet 文件的签名与完整性
uv run omd sec financials validate --root artifacts/sec-financials
```

---

### 3. yfinance 跨版本零漂移审计 (`omd audit-drift`)

用于在升级 `yfinance` 版本或更换底层数据源时，对历史 10+ 年数据进行严格的横向位级（Bit-exact）比对，杜绝脏数据侵入：

```bash
# 严格未复权行情位级零差异门禁（OHLCV 必须 0 差异）
uv run omd audit-drift \
  --universe r10a0 \
  --baseline-dir ./data_baseline \
  --target-dir ./data_candidate \
  --raw-only

# 全量审计（包含 adj_close 复权收盘价浮点微小公差对比）
uv run omd audit-drift \
  --universe r10a0 \
  --baseline-dir ./data_baseline \
  --target-dir ./data_candidate \
  --abs-tolerance 0.001 \
  --output-md audit_summary.md \
  --output-json audit_receipt.json
```

* 预置的 **`r10a0` 基准宇宙**：覆盖核心多资产 ETF，包括 `SPY`、`QQQ`、`XLK`、`IWM`、`SMH`、`XLF`、`XLE`、`XLV`、`TLT`、`GLD`、`USMV`、`SHY`、`IEF`（跨越 2015 至今 38,000+ 根日线 Bar）。

---

## Provider SDK 使用指南

### 1. yfinance Provider（美股/全球行情与基本面）

OMD 封装的 `YFinanceClient` 拥有严格的输入清洗、标准小写列转换以及健壮的单标的修复机制（Per-symbol repair）：

#### 获取标准化日线行情 (Daily Bars)

```python
from ohmydata.providers.yfinance import (
    YFinanceAdjustmentMode,
    YFinanceBatchPolicy,
    YFinanceClient,
    YFinanceDailyBarsRequest,
    YFinanceRepairPolicy,
)

# 1. 构建强类型请求（支持美股标的、带点类股 BRK-B、指数 ^VIX、港股 0700.HK、外汇 GBPUSD=X）
request = YFinanceDailyBarsRequest(
    symbols=("SPY", "QQQ", "^VIX"),
    start_date="2024-01-01",
    end_date_exclusive="2024-02-01",
    adjustment_mode=YFinanceAdjustmentMode.RAW_WITH_ADJ_CLOSE,
    batch_policy=YFinanceBatchPolicy.STRICT,
    repair_policy=YFinanceRepairPolicy.PER_SYMBOL,  # 批量抓取遇个别标的缺失时自动启动安全单体补救
)

client = YFinanceClient()
result = client.fetch_daily_bars(request)

# 2. 获得标准格式的 Pandas DataFrame
# 列结构稳定为：symbol, date, open, high, low, close, adj_close, volume
df = result.dataframe
print(df.head())
```

#### 获取基本面估值与分析师预期 (Fundamentals & Estimates)

```python
from ohmydata.providers.yfinance import (
    YFinanceClient,
    YFinanceFundamentalsRequest,
)

req = YFinanceFundamentalsRequest(
    symbols=("AAPL", "NVDA"),
    include_financials=True,
    include_valuation=True,
    include_estimates=True,
)

client = YFinanceClient()
fund_result = client.fetch_fundamentals(req)

# 转为规整记录或 DataFrame
for symbol, record in fund_result.records.items():
    print(
        f"[{symbol}] 市值: {record.valuation.market_cap}, 动态市盈率: {record.valuation.trailing_pe}"
    )
    print(f"       最近季度营收: {record.financials.revenue_latest}")
    print(f"       分析师当季 EPS 预期: {record.estimates.eps_est_current_q}")
```

> **治理准则**：在 OMD 中，默认严格遵循 `auto_adjust=False, repair=False, actions=True, keepna=True` 原则，禁止上游第三方库随意修改原始分红与拆股价格。

---

### 2. SEC EDGAR & N-PORT Provider（美股财报与基金持仓）

#### 抽取上市公司 10-K/10-Q 财报三大表

```python
from ohmydata.providers.sec import (
    SecFinancialsClient,
    SecFinancialsRequest,
    write_financials_partition,
)

# 外部注入 Contact User-Agent 头（符合 SEC 监管规范，绝不通过 .env 偷读）
client = SecFinancialsClient("MyResearchFirm/1.0 (quant@example.com)")

request = SecFinancialsRequest(
    symbols=("AAPL", "MSFT"),
    forms=("10-K", "10-Q"),
    availability_policy="accepted-at-plus-lag",
    lag_days=0,  # 严格基于 EDGAR accepted_at 加上偏移天数作为可用性时间锚点
)

vintages = client.fetch_company_financials(request)

# 写入不可变 Parquet 本地湖（包含标准概念 standard_concept 与原始概念 concept）
for symbol in ("AAPL", "MSFT"):
    sym_vintages = [v for v in vintages if v.symbol == symbol]
    write_financials_partition("artifacts/sec-financials", symbol, sym_vintages)
```

---

### 3. Tushare Provider（A 股与中国 ETF 行情与事实）

Tushare 适配器只接收已经完成初始化的客户端实例，库内部不触碰 Token：

#### 日线行情与复权处理

```python
from ohmydata.providers.tushare import (
    AdjustedEtfBarsRequest,
    AdjustmentCoveragePolicy,
    EmptyPolicy,
    FundDailyRequest,
    StockDailyRequest,
    TushareClient,
    fetch_adjusted_etf_bars,
)

# 传入外部已初始化成功的 tushare pro 客户端
client = TushareClient(external_pro_api)

# 1. 股票日线（原生单位：成交量为手，成交金额为千元，百分比涨跌幅）
stock_req = StockDailyRequest(ts_code="000001.SZ", empty_policy=EmptyPolicy.ERROR)
stock_df = client.fetch_stock_daily(stock_req)

# 2. 复权 ETF 行情（显式指定复权因子覆盖策略，严密抵御缺失因子）
adj_req = AdjustedEtfBarsRequest(
    ts_code="510050.SH",
    empty_policy=EmptyPolicy.ERROR,
    coverage_policy=AdjustmentCoveragePolicy.STRICT,
    start_date="20240101",
    end_date="20240131",
)
adj_bars = fetch_adjusted_etf_bars(client, adj_req)
```

#### ETF 申赎清单 (PCF) 与大窗口二分法检索

```python
from ohmydata.providers.tushare import (
    EmptyPolicy,
    EtfPcfHistoryRequest,
    fetch_etf_pcf_history,
)

# fetch_etf_pcf_history 具备时间窗口递归二分检索能力，自动规避 Tushare 单次 3000 行限制
history = fetch_etf_pcf_history(
    client,
    EtfPcfHistoryRequest(
        ts_code="510050.SH",
        exchange="SH",
        start_date="20240101",
        end_date="20240131",
        empty_policy=EmptyPolicy.ALLOW,
    ),
)
pcf_df = history.frame
```

> **深度穿透与配方文档**：
> 有关行业成份股穿透、加权股息率计算配方（`build_index_dividend_yield`）及多维切片装配，请参考专题迁移文档：
> - [`docs/v0.1.2-lookthrough-migration.md`](docs/v0.1.2-lookthrough-migration.md)
> - [`docs/v0.1.3-vintage-plane-migration.md`](docs/v0.1.3-vintage-plane-migration.md)

---

## 核心工程机制与不可变快照 (Core Architecture)

`ohmydata.core` 构成了整个 SDK 的可靠性底座：

```python
from datetime import UTC, datetime
from pathlib import Path

from ohmydata.core import (
    RateLimiter,
    RateLimitPolicy,
    RequestSpec,
    RetryPolicy,
    SnapshotMode,
    SnapshotStore,
    execute_with_retry,
)

# 1. 请求规范与防敏感信息泄露：序列化参数时自动拒绝包含 token 等可疑键名
req_spec = RequestSpec("demo_source", "bars", {"symbol": "AAPL"})

# 2. 实例级安全限流器与智能重试机制（total-attempt 语义）
limiter = RateLimiter(RateLimitPolicy(rate=5.0))  # 5 QPS
limiter.acquire()
data = execute_with_retry(lambda: b"payload-bytes", RetryPolicy(max_attempts=3))

# 3. 不可变快照写入（APPEND 追加观测记录 / FROZEN 锁死唯一事实版本）
store = SnapshotStore(Path("snapshots"))
store.write(req_spec, data, datetime.now(UTC), "json-v1", SnapshotMode.APPEND)
```

* **`AvailabilityEvidence`**：所有产出的事实数据均绑定独立的可用性证据，杜绝基于“收盘日”臆测“可用时点”。
* **`RawFactEnvelope`**：保留每次数据抓取的原始哈希指纹、版本号与不可变凭证，保障审计完全可追溯。

---

## 高性能 Dataframe 适配器 (Polars & Pandas)

安装 `ohmydata[polars]` 后，可使用严密类型的双向转换器：

```python
from ohmydata.adapters.polars import pandas_to_polars, polars_to_pandas

# 严格类型转换；遇到非法或有损字段立即抛出 SchemaMismatchError
polars_df = pandas_to_polars(pandas_df)

# 对于全空的 Pandas object 列，支持显式降级为 String
polars_df = pandas_to_polars(pandas_df, empty_object_policy="string")

# 反向转为 Pandas DataFrame
pandas_df = polars_to_pandas(polars_df)
```

---

## 本地开发、质量门禁与文档索引

OMD 采用严格的现代 Python 工具链。在提交代码或发布前，必须跑通全部质量门禁：

```bash
# 1. 依赖锁定与环境同步
uv lock
uv sync --all-extras

# 2. 完整离线单元测试与回归测试（无网络依赖）
uv run pytest

# 3. 代码风格与 Lint 检查
uv run ruff check .
uv run ruff format --check .

# 4. 强静态类型检查 (Astral ty)
uv run ty check

# 5. 构建包产物校验与 Git 卫生检查
uv build
git diff --check
```

### 核心文档索引

* [**AGENTS.md**](AGENTS.md)：开发智能体执行准则、最高优先级不变量与发布规范。
* [**PLAN.md**](PLAN.md)：核心架构演进路线图。
* [**docs/consumer-capability-matrix.md**](docs/consumer-capability-matrix.md)：下游量化消费端兼容性矩阵。
* [**docs/behavioral-inventory.md**](docs/behavioral-inventory.md)：核心行为与数据契约特征清单。

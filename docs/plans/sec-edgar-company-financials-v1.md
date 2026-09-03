# SEC EDGAR Company Financials PIT V1 Integration Plan

**Status**: IMPLEMENTED AND ACCEPTED
**Target Scope**: 10-K / 10-Q Three Core Statements (Balance Sheet, Income Statement, Cash Flow) with Point-in-Time (PIT) Provenance
**Upstream Engine**: `edgartools` (isolated as an optional extra)
**Configuration & Credentials**: Unified OMD configuration format (`--config` mapping, explicit User-Agent injection)

---

## 1. 目标与设计原则 (Objectives & Invariants)

在 `ohmydata.providers.sec` 下构建企业财报（Company Financials）摄取适配层，基于 `edgartools` 的 XBRL 解析能力，落地**美股上市公司三大财务报表**，并严格遵守 [`AGENTS.md`](file:///Users/ron/Documents/omd/AGENTS.md) 的核心不变量：

1. **凭证注入（Credential Injection）**：
   - 统一复用 OMD 的现有配置文件标准（如 `artifacts/sec-sync.yaml` 或自定义 YAML/JSON/TOML）；
   - 从配置中的 `user_agent_file` 或显式参数获取 SEC 要求的身份标识；
   - **禁止**隐式读取 `.env`、系统环境变量、钥匙串或未授权文件。
2. **依赖隔离（Dependency Isolation）**：
   - 将 `edgartools` 及其传递依赖完全隔离在可选扩展中：`[project.optional-dependencies] sec-financials = ["edgartools>=5.0.0"]`；
   - OMD 核心库保持 **0 运行时依赖**；未安装该 extra 时调用相关接口将抛出清晰的指引异常，绝不污染基础安装包。
3. **无前瞻偏误（Point-in-Time Lineage）**：
   - 绝不使用财报所属的会计周期截止日（`period_end`）作为时点可用时间；
   - 严格绑定 EDGAR 官方申报时间戳：
     - `filing_date`: 申报公开日（日历日期）；
     - `accepted_at`: SEC 官方接收精确时间戳（UTC）；
     - `availability_anchor`: `accepted_at + lag_days`（支持自定义发布延迟）；
   - 区分原始申报（10-K/10-Q）与更正申报（10-K/A, 10-Q/A），采用只增不改（Append-only）的 Vintage 模型管理财报更正与重述。
4. **仅落地三大核心财务报表**：
   - **资产负债表 (Balance Sheet)**
   - **利润表 / 损益表 (Income Statement)**
   - **现金流量表 (Cash Flow Statement)**
   - 同时保留：
     - 原始字段名（Company-native `concept` & `label`，原汁原味，不篡改）；
     - 标准化字段名（XBRL `standard_concept`，便于跨公司横向对比与量化打分）。
5. **离线优先与确定性测试（Offline by Default）**：
   - 默认单元测试与 CI 不发起任何真实外部网络请求；
   - 提供基于合成 XML/JSON 的离线 Fixtures 与快照回放机制。

---

## 2. 模块架构与文件布局

```text
src/ohmydata/providers/sec/
├── __init__.py
├── artifacts.py               # 现有：SEC 压缩包快照存储
├── batch.py                   # 现有：N-PORT 批处理湖仓构建
├── nport.py                   # 现有：N-PORT TSV 解析
├── edgar.py                   # 现有：EDGAR 官方 Submissions API 访问
├── core_dataset.py            # 现有：Parquet 写入器与 Schema 定义
├── financials.py              # 【新增】：三大报表强类型请求、结果与 Point-in-Time 实体
└── edgartools_adapter.py      # 【新增】：安全封装 edgartools 的隔离客户端与解析器

tests/providers/sec/
├── test_financials.py         # 【新增】：离线单元测试（Schema、PIT 时间戳、更正处理）
└── fixtures/financials/       # 【新增】：合成 10-K / 10-Q 财报测试夹具
```

---

## 3. 配置文件与凭证注入规范

与现有 `omd sec nport` 保持 100% 格式统一与复用：

```yaml
# artifacts/sec-financials.yaml
universe:
  symbols: ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN"]
forms: ["10-K", "10-Q"]
start_year: 2020
end_year: 2026
root: "artifacts/sec-company-financials"
user_agent_file: "artifacts/sec-contact.txt"    # 显式路径，禁止隐式读取 .env
availability_policy: "accepted-at-plus-lag"
lag_days: 0
```

- 在 Python SDK 内部：
  ```python
  from ohmydata.providers.sec.financials import SecFinancialsClient, SecFinancialsRequest

  client = SecFinancialsClient.from_config("artifacts/sec-financials.yaml")
  # 或者显式注入 User-Agent:
  client = SecFinancialsClient(user_agent="SampleApp/1.0 (contact@example.com)")
  ```
- 适配器在调用 `edgartools` 前，显式在受控上下文设置身份，并在调用后清理或隔离状态，不污染宿主全局环境。

---

## 4. 数据表结构设计 (Core Dataset Schema)

为三大报表设计紧凑、强类型、适合量化回测的 Parquet 表结构：

### 4.1 财报申报期次主表 (`company_financial_vintages.parquet`)
| 字段 | 类型 | 说明 |
| :--- | :--- | :--- |
| `symbol` | string | 股票代码（如 AAPL） |
| `cik` | string | SEC 官方 10 位 CIK（如 0000320193） |
| `company_name` | string | 公司官方注册名 |
| `form` | string | 申报表单类型（`10-K`, `10-Q`, `10-K/A`, `10-Q/A`） |
| `accession_number` | string | SEC 唯一申报编号（主键标识） |
| `fiscal_year` | int32 | 财年（如 2023） |
| `fiscal_period` | string | 财报期（`FY`, `Q1`, `Q2`, `Q3`） |
| `period_end` | date | **会计周期截止日**（如 2023-09-30，严禁用于 PIT 时点） |
| `filing_date` | date | SEC 申报日（如 2023-11-03） |
| `accepted_at` | timestamp[us, UTC] | **SEC EDGAR 正式接收时间戳**（精确到秒） |
| `availability_anchor` | timestamp[us, UTC] | **可用性锚点**（`accepted_at + lag_days`，回测时点对齐基准） |
| `is_amendment` | bool | 是否为更正重述版本（如 10-K/A） |
| `vintage_identity` | string | 确定性内容哈希（SHA-256） |

### 4.2 财务报表明细表 (`financial_statements.parquet`)
用于存储三大报表的每一项具体数值（资产负债表、利润表、现金流量表）：
| 字段 | 类型 | 说明 |
| :--- | :--- | :--- |
| `accession_number` | string | 关联合同申报编号 |
| `symbol` | string | 股票代码 |
| `statement_type` | string | 报表类型（`balance_sheet`, `income_statement`, `cash_flow`） |
| `standard_concept` | string | **标准化概念**（用于跨公司统一对比，如 `Revenues`, `NetIncome`, `TotalAssets`） |
| `concept` | string | 原始 US-GAAP / IFRS 标签概念（如 `us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax`） |
| `label` | string | 公司报表披露的原始行文本标签（如 "Total net sales"） |
| `value` | decimal(28, 4) | 精确数值（保留原生精度，不隐式舍入） |
| `value_native` | string | 原生原始字符串数值 |
| `unit` | string | 货币单位（如 USD） |
| `decimals` | int32 | 披露精度标识（如 -6 表示以百万为单位） |
| `period_start` | date | 适用期间起始日（损益表/现金流区间型科目使用） |
| `period_end` | date | 适用期间结束日 |
| `availability_anchor`| timestamp[us, UTC] | 冗余字段，方便免关联直接进行时点过滤 |

---

## 5. 实施里程碑 (Implementation Milestones)

- [x] **M1: 依赖扩展配置与环境验证**
  - 在 `pyproject.toml` 中添加可选依赖 `[project.optional-dependencies] sec-financials = ["edgartools>=5.0.0"]`；
  - 增加动态导入防护：未安装时提示清晰安装命令。
- [x] **M2: 凭证受控注入与包装器实现**
  - 在 `src/ohmydata/providers/sec/financials.py` 中实现配置解析与 User-Agent 显式注入；
  - 封装 `edgartools` 的 `Company.get_filings()` 与财报提取逻辑，拦截全局状态泄露。
- [x] **M3: 三大报表标准化提取器与 PIT 数据模型**
  - 实现从 `TenK` / `TenQ` 对象中提取 `Balance Sheet`、`Income Statement`、`Cash Flow Statement`；
  - 提取 `accepted_at` 与 `filing_date`，构建无前瞻偏误的 `availability_anchor`。
- [x] **M4: 离线测试套件构建**
  - 编写合成 10-K/10-Q 样本数据（覆盖正常申报、季度申报与更正申报 10-K/A）；
  - 验证 Schema 完整性、Decimal 精度、PIT 锚点对齐与离线无网络隔离。
- [x] **M5: 批处理命令行工具扩展 (`omd sec financials`)**
  - 提供 `plan`、`fetch`、`build`、`sync`、`validate` 等标准 CLI 命令；
  - 导出符合 Hive 分区的 Parquet 湖仓文件。
- [x] **M6: 文档与迁移指南更新**
  - 更新 `README.md` 与能力清单文档。

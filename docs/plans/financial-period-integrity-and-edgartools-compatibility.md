# OMD 财务期间与 Edgar 适配修复计划

状态：适配层离线修复已实施；live 对照、消费者迁移及发布未执行。原核查日期：2026-09-08。
本计划由 Stock Notify 财务对照问题触发；原计划阶段仅交付方案，后续实施保留 edgartools 5.56.0 与 yfinance 1.7.0。接口变化及迁移要求见[实施说明](../financial-period-integrity.md)。

## 1. 结论：不是单纯升级 edgartools

- OMD 项目版本为 0.2.2，`uv.lock` 已解析到 edgartools 5.56.0。
- 消费者运行环境也为 ohmydata 0.2.2 + edgartools 5.56.0。
- 核查时 [PyPI](https://pypi.org/project/edgartools/) 的当前版本同为5.56.0，发布日期2026-09-02。
- 已确认的问题包含 OMD 自己的适配/选择逻辑；只升级 edgartools 不能修复 Yahoo 分支。
- 执行时重新核对版本。若有新版本，在隔离环境比较；只有回归用例证明改善才升级，不能以版本号代替验收。

## 2. 开工前阅读与工作区

阅读 [AGENTS](../../AGENTS.md)、[SEC原计划](sec-edgar-company-financials-v1.md)、
[FPE计划](v0.2.2-yfinance-fundamentals-consensus-and-fpe.md) 和
[Yahoo治理](../harness/yfinance-governance.md)。本次检查已有 `AGENTS.md` 修改和
`docs/harness/` 未跟踪内容，保留它们；重新检查 git status，不覆盖用户工作。
旧SEC计划的“已接受”是历史状态，本计划描述后来发现的回归缺口。

OMD是公共仓库：仅提交合成fixture，不复制下载的原始报表、消费者快照、联系人或配置。
SDK继续使用调用者显式注入的身份；不可照搬消费者脚本读取环境变量的方式。

## 3. 已确认的代码缺陷与未决归因

| 优先级 | 问题与代码位置 | 修复要求 |
| --- | --- | --- |
| P0 | `src/ohmydata/providers/sec/edgartools_adapter.py` 的日期正则仅接受裸日期；真实列包含 `(FY)`、`(Q2)`、`(YTD)` | 不只删除后缀：保留期间类型、起止和duration，避免相同结束日的季度与YTD混合 |
| P0 | SEC取 `latest(limit)` 后才排除不允许的修订文件；NET案例中最新为治理型10-K/A | 在limit前应用forms、amendment和年份筛选；定义limit为合格申报数。排除修订时仍应找到原始10-K |
| P0 | Yahoo `_fundamentals_parsers.py` 的日期优先取 `mostRecentQuarter`，数值按第一个非空单元格取 | 每个字段必须绑定实际列日期，不能给旧数值贴新财季 |
| P0 | Yahoo `extract_quarterly_pair` 先dropna再取位置0和4 | 按真实同财季日期找同比；缺季度、乱序和空值不得改变年度对应关系 |
| P1 | SEC模型有 `period_start`，adapter未填；解析错误/无匹配列可能返回空或仅资产负债表 | 保留真实期间起点，区分无申报、未披露、解析失败、部分覆盖，不伪称完整成功 |
| P1 | `latest(n)` 返回值仅按list/tuple或单对象处理 | 用合成和实际API形状验证Filings集合；准确枚举0、1、n份，不把集合当单份Filing |

SNOW对照中曾把上季度数值标为最新季度；代码存在足以造成该错误的日期/数值分离路径。
**尚未保存当时完整Yahoo原始矩阵，因此上游更新延迟与OMD选择错误的贡献须由回放进一步区分。**
NET的营业利润与官方GAAP不同，但不能据差值直接断言是重组费用或Edgar缺陷。
须比较Yahoo原始行名/值、OMD候选科目和SEC concept，明确 `Operating Income`、`EBIT`、
`Normalized/Adjusted` 的语义；不把它们静默互换。CRM/PANW亦作为跨源差异回归对象。

## 4. 范围与契约

主要文件：

- `src/ohmydata/providers/sec/{edgartools_adapter,financials,financials_dataset}.py`
- `src/ohmydata/providers/yfinance/{_fundamentals_parsers,fundamentals}.py`
- 对应 `tests/providers/sec/test_financials.py`、`tests/providers/yfinance/test_fundamentals.py`
- 公共导出、schema文档及CLI仅在接口变更确有需要时调整。

SEC必须保留native concept/label/value、unit、维度身份、statement type、period start/end、
期间类型，以及filing accession/form/accepted_at。优先使用edgartools的结构化XBRL context；
展示列文本只作兼容输入，无法恢复真实起点时显式标未知，不按90天或季度标签猜日期。
时点资产负债数据没有duration起点；季度与YTD允许同结束日但不是同一事实。
单位缺失不得默认为USD；非有限数值拒绝。布尔/空值过滤须覆盖numpy.bool_、NA和分部行。

修订策略：`include_amendments=False` 排除修订；开启时保留独立vintage及关系，
不能为得到非空结果静默用原始申报代替用户请求的修订版本。
单纯治理修订与财务重述需明确区分。accepted_at/PIT规则不变，不用财季截止日代替可用时间。

Yahoo继续区分provider原始值与normalized值。明确统一 `report_date` 的含义并为跨表/字段
期间不齐提供元数据或显式覆盖错误；同一年报/财季的金额和EPS才可组合。
估值保留quote time、EPS预测期间与来源，禁止从PE×EPS反推出的价格冒充最新实际行情。
明确GAAP/预期EPS可比性和原始回退，不把任何前瞻FPE都命名为NTM。
保持现有yfinance精确版本和价格默认参数；本计划不升级yfinance。

OMD不接管行业Rule of 40、评级、目标价、Electron存储发布或公司专用IR网页解析。
这些属于消费者。可提供原始事实与coverage，使消费者无需自行绕过adapter。

## 5. 实施顺序

1. **先复现**：保持5.56.0，添加合成失败用例；记录native返回与OMD输出的边界。
2. **修SEC**：期间模型/解析、筛选先于limit、集合枚举、完整性与错误类型；同步序列化契约。
3. **修Yahoo**：按期间取本期与同比，保留缺值，固定营业利润科目含义；避免扩大为行情版本升级。
4. **依赖决策**：用相同fixtures对照基线与候选edgartools；没有已验证收益就不盲升。
   当前 `edgartools>=5.0.0` 声明过宽，应选择并记录测试支持范围或固定版本策略，更新lock，
   同时检查Python3.11/3.12和optional-extra安装。SDK核心仍保持最小依赖。
5. **消费者验收**：以不可变候选构建对照Stock Notify现有适配补丁；达到一致才迁移，
   不先删除消费者防护，不覆盖旧证据。发布与消费者升级分别需要后续明确授权。

## 6. 必须覆盖的离线回归

- 同结束日 `Q2`/`YTD`、FY、裸日期时点列并存；保留起点和期间身份，round-trip无丢失。
- 合成最新Q2列为空但Q1非空，metadata已到Q2：返回Q2缺失或有明确Q1身份的数据，不得伪造Q2。
- 少一季度、列升降序、同期间重复冲突、53周财年：同比依日期/财季契约而非数组位置。
- 最新治理型10-K/A + 原始10-K + limit=1；含/不含修订、年份过滤和多个forms分别测试。
- 0/1/n份Filing集合；部分报表缺失、未知列格式、解析异常分别产生可区分结果。
- GAAP OperatingIncomeLoss、EBIT和调整后字段并存；不静默替代；USD/非USD/未知unit及非有限数值。
- 不同维度、抽象行、numpy布尔与NA；原始concept/value不丢失。
- schema/Parquet/vintage identity兼容性：新增期间信息可能改变身份，应明确schema版本与迁移，
  不能让旧缓存被错误认定为新契约。PIT时间与申报版本回归不变。

## 7. 受控真实验证与验收

本计划编写不授权新的live请求。实施任务获得授权后，以调用者注入身份运行integration标记用例：
PLTR、CRM、NOW、CRWD、SNOW、NET、PANW、MDB。最多8标的；每家固定一份年度和一份季度申报，
NET另加一份修订用于选取验证；先固定accession再比较，避免latest在两次运行间变化。
串行；每份最多60秒，整批最多20分钟；不自动循环重试；超时终止自有任务并记录未完成项。
缓存命中与首次下载分别计时，过程记录覆盖、失败、来源和实际期间，禁止把失败归入N/A成功数。

对照三层：原始SEC结构化事实 → OMD typed rows → 消费者计算。
Yahoo另做原始带日期矩阵 → normalized事实的对照；不同源不一致先分类，禁止用“取较大值”解决。
私有消费者证据位于邻近Stock Notify的 `data/earnings/evidence/SAAS_IGV_20260908_COMPLETE/`，
参考实现为 `data/update_research_metrics.py`、`data/sec_actuals.py`、`data/valuation_snapshot.py`。
这些只是问题线索/迁移对照，不是可直接搬入公共SDK的标准实现或fixture。

验收要求：同源同期间金额使用Decimal精确对照；消费端比率误差有明确容差；
不同源差异有可解释记录；无单季/YTD混用、财季错位、静默丢报表和虚假PIT。
PANW无当期10-K时明确缺该申报，而非把公告伪装成10-K；这不阻止已发布季度公告在消费者使用。

安装SEC extra后确保测试实际执行，而非全被skip。实现完成运行：

```bash
uv run --extra sec-financials --extra yfinance pytest tests/providers/sec/test_financials.py tests/providers/yfinance/test_fundamentals.py
uv run --extra sec-financials --extra yfinance pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv build
git diff --check
```

若意外需要升级yfinance，另立版本升级任务并执行治理文档的完整zero-drift门槛，不能借本计划绕过。
更新公共接口文档及CHANGELOG；具体新版本号在发布任务决定，不能预先宣称已修复/发布。

## 8. 在OMD任务中开工的提示

“按 `docs/plans/financial-period-integrity-and-edgartools-compatibility.md` 实施修复。
先复现当前5.56.0的失败用例，再修OMD适配层；不要把任务简化成升级依赖。
保留现有工作区修改，离线验证优先，未经授权不做live验证、提交或发布。”

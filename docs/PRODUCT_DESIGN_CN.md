# PriCredit 产品设计文档（中文，基于当前实现）

> 版本：v0.1（实现对齐版）  
> 更新时间：2026-04-21  
> 说明：本文严格基于当前代码与可运行流程，区分“已实现（Now）”与“规划中（Next）”。

## 1. 产品定位

PriCredit 是一个面向 **公开交易 BDC（Business Development Company）** 的 AI-CRM 数据与风控产品。  
核心目标是围绕 SEC EDGAR 公共披露，构建三条能力链路：

1. **Portfolio Management（组合管理）**：从 10-K/10-Q 的 Schedule of Investments 与 XBRL 里抽取组合结构与集中度信号；
2. **Risk Management Engine（风险引擎）**：把可解释指标映射为分项得分、综合风险分与告警；
3. **Investor Reporting（投资人报告）**：将结构化结果合成为可读简报与索引。

当前新增了两条 Beta 能力：
- **8-K 最小事件抽取（ARCC 优先）**
- **Shadow NAV（实验版，ARCC 优先）**

---

## 2. 设计原则

- **EDGAR 单一事实源**：所有上游数据来自 SEC EDGAR（`data.sec.gov` + `www.sec.gov/Archives`）。
- **可审计**：输出尽可能保留来源字段（form、accession、filed date、source path）。
- **可重算**：评分与告警规则配置化（`ingest/risk_weights.yaml`），支持参数调整后全量重算。
- **离线合成**：报告模块只消费已落盘产物，不重复抓取网络数据。
- **渐进式演进**：先 ARCC 单实体打通（vertical slice），再扩展全宇宙与模型精度。

---

## 3. 用户与典型场景

### 3.1 目标用户
- 信贷/二级信贷分析师
- 家办与机构 LP 监控人员
- 私募信贷投研/风控产品经理

### 3.2 场景
- 日常监控：查看哪些 BDC 风险上升、触发何种阈值；
- 事件驱动：8-K 出现后快速定位 Item 与潜在信用/估值影响；
- 对外沟通：生成一致口径的投资人简报；
- 试验性估值：基于事件做 Shadow NAV 先行信号（明确实验属性）。

---

## 4. 系统架构（当前）

高层流程：

1. **BDC 宇宙发现**：`scripts/discover_bdcs.py`
2. **EDGAR 抓取**：`scripts/fetch_filings.py`
3. **XBRL 基础解析**：`scripts/parse_filings.py`
4. **SoI 聚合解析**：`scripts/extract_portfolio.py`
5. **风险评分/告警**：`scripts/compute_risk.py`
6. **投资人简报**：`scripts/build_investor_report.py`
7. **告警分发**：`scripts/send_risk_alerts.py`（可选 WhatsApp）
8. **8-K 事件抽取（最小版）**：`scripts/extract_8k_items.py`
9. **Shadow NAV（实验版）**：`scripts/shadow_nav.py`
10. **总编排**：`scripts/run-daily-pricredit.sh`

---

## 5. 模块设计（Now / Next）

## 5.1 数据采集层（EDGAR Ingestion）

**Now**
- 公共能力位于 `scripts/_edgar_common.py`：限流、重试、UA 校验、缓存。
- 默认抓取表单：`10-K,10-Q,8-K`。
- 可选抓取注册/招股书类表单：`--include-registration`（N-2、424B2/3/5、497）。
- 输出：`filings/<cik>/<accession>/index.json + meta.json + primary_document`。

**Next**
- 注册类文档（N-2/497/424B*）结构化解析（费用条款、投资限制、风险因子）。

## 5.2 组合管理模块（Portfolio Management）

**Now**
- `extract_portfolio.py` 从 10-Q/10-K 的 inline XBRL 抽取：
  - 行业分布、行业 HHI、Top 行业；
  - 关联方占比；
  - 已标注的 non-accrual%（覆盖有限）。
- 输出：`extracted/<cik>/portfolio/<accession>/portfolio.json`。

**Next**
- HTML 表格兜底解析提升 non-accrual 覆盖；
- 更细颗粒（借款级）字段：债务层级、票息/spread、PIK 标识、到期结构。

## 5.3 风险引擎（Risk Management Engine）

**Now**
- 由 `compute_risk.py` + `ingest/risk_weights.yaml` 驱动；
- 分项因子通过分段线性曲线映射到 0-100；
- 综合分按可用因子权重重归一；
- 产出 band：`low/medium/high/critical`；
- 独立告警规则（不依赖综合分）输出 `alert_*.json`。

**Next**
- 因子校准与回测；
- 覆盖缺失因子的数据补全；
- 引入事件层因子（8-K 信号）作为风险补充。

## 5.4 投资人报告模块（Investor Reporting）

**Now**
- `build_investor_report.py` 只合成本地产物，不触网；
- 输出：
  - 单 BDC 简报：`reports/<DATE>/briefs/<TICKER>.md + .json`
  - 全市场索引：`index.md + index.json`

**Next**
- HTML/PDF 渲染；
- 分发自动化（`send_reports.py` 规划中）；
- 客户分层模板（IC 版、LP 版）。

## 5.5 8-K 事件模块（最小版）

**Now**
- `extract_8k_items.py`（ARCC-first）：
  - 抽取 `Item X.XX` 引用与片段；
  - 关键字事件标记（如信用额度变化、realized gain/loss）。
- 输出：
  - `extracted/<cik>/events8k/<accession>/events_8k.json`
  - `reports/<DATE>/events8k_summary_<TICKER>.json`

**Next**
- Item 级语义分类与事件标准化（协议修订、退出/处置、估值相关事件）；
- 文本证据质量评分与冲突消解。

## 5.6 Shadow NAV（实验版）

**Now**
- `shadow_nav.py` 使用：
  - 最新官方 NAV（`summary.json`）作为 baseline；
  - baseline 披露日之后的 8-K 事件做保守启发式调整；
- 输出：`reports/<DATE>/shadow_nav_<TICKER>.json`；
- 默认标注：`experimental` + `low/medium confidence`。

**Next**
- 事件类型到 NAV 影响映射的参数化；
- 与后续 10-Q/10-K 官方 NAV 的偏差回测；
- 置信度模型升级（文本质量、事件强度、时效性）。

---

## 6. 数据与文件契约（关键）

- `bdc/bdc_universe.json`：BDC 基础宇宙（ticker、cik、公开状态）
- `filings/<cik>/<accession>/meta.json`：抓取元数据与文件定位
- `extracted/<cik>/facts/summary.json`：规范化财务快照
- `extracted/<cik>/portfolio/<accession>/portfolio.json`：组合聚合信号
- `extracted/<cik>/events8k/<accession>/events_8k.json`：8-K 事件信号（v0）
- `reports/<DATE>/risk_<ticker>.json`：风险评分卡
- `reports/<DATE>/alert_*.json`：规则告警
- `reports/<DATE>/briefs/*.md|*.json`：投资人简报
- `reports/<DATE>/shadow_nav_<ticker>.json`：Shadow NAV（实验）

---

## 7. 端到端日常运行

推荐入口：`scripts/run-daily-pricredit.sh`

常见运行模式：

```bash
# 标准日跑
scripts/run-daily-pricredit.sh

# ARCC 深入 + 注册类表单 + 8-K + Shadow NAV
INCLUDE_REGISTRATION=1 RUN_8K_ITEMS=1 RUN_SHADOW_NAV=1 \
scripts/run-daily-pricredit.sh --tickers ARCC
```

---

## 8. 非功能设计

- **合规**：强制 EDGAR UA 联系方式（`PRICREDIT_UA_EMAIL`）；
- **稳定性**：重试 + 限流 + 缓存；单标的失败不阻塞全局；
- **可追踪**：运行日志与 run summary 文件；
- **幂等性**：多数步骤支持复跑与 `--force` 控制；
- **安全性**：凭据置于 `~/.pricredit-env`，不入库。

---

## 9. 风险与限制（当前真实状态）

- 非应计覆盖仍不完整（很多 BDC 不直接打 tag）；
- 8-K 解析仍是规则/关键字最小实现，适合“发现线索”，不适合直接自动决策；
- Shadow NAV 是实验信号，不可替代官方 NAV；
- 当前尚未形成完整回测闭环。

---

## 10. 未来 2 个迭代建议

### 迭代 A：8-K 事件标准化（优先）
- 建立事件 taxonomy（融资、退出、损失、治理、指引等）；
- 建立“是否影响 NAV”的规则白名单/黑名单；
- 增加 evidence quality score。

### 迭代 B：Shadow NAV 校准化
- 参数外置（YAML）；
- 形成 “预测 vs 后验官方 NAV” 评估报表；
- 输出误差分布与置信区间。

---

## 11. 对外口径建议（中文）

建议统一表述：

> PriCredit 通过 SEC EDGAR 的公开披露构建 BDC 组合监控、风险评分与投资人报告能力；  
> 8-K 事件与 Shadow NAV 当前为实验增强模块，用于提供早期风险线索，不构成投资建议。


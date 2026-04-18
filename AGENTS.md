# Agents in this workspace (`PriCredit`)

- **PriCredit / AI-CRM**（本工作区）：以 **SEC EDGAR** 为唯一事实源，围绕
  **公开交易的 BDC**（Business Development Company，公募化的私募信贷基金）
  构建 Investor AI-CRM。主体能力分三个模块：Portfolio Management、
  Investor Reporting、Risk Management Engine。详见
  [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) 与 [`SOUL.md`](SOUL.md)。

本工作区**自成一体**：不依赖 `wechat` / `idvault` 等其他仓库；OpenClaw
多代理协作（若启用）通过显式 `OPENCLAW.md` 契约声明。

## 数据流（期望形态）

1. **bdc/** — BDC 宇宙（`bdc_universe.json`）：ticker + CIK + 元数据。
   由 `scripts/discover_bdcs.py` 自动发现，可叠加人工 `bdc_overrides.json`。
2. **EDGAR 拉取** — `scripts/fetch_filings.py` 按 CIK 读取 submissions.json，
   下载目标表单（10-K / 10-Q / 8-K / DEF 14A / N-54A 等）。
3. **parse** — 表单解析（XBRL Company Facts、Schedule of Investments、
   Management Discussion）写入 `extracted/<cik>/<accession>/*.json`（未进仓库）。
4. **risk** — `scripts/compute_risk.py` 消费 `extracted/<cik>/facts/summary.json`，
   按 `ingest/risk_weights.yaml` 的权重 + 分段线性阈值曲线评分，产出
   `reports/YYYY-MM-DD/risk_<ticker>.json`（单家明细）、`risk_summary.json`（汇总）
   与 `alert_RISK-<ticker>-*.json`（触发阈值时一条一个）。评分体系详见
   [`docs/RISK_MODEL.md`](docs/RISK_MODEL.md)。
5. **distribute** — 风险告警由 `scripts/send_risk_alerts.py` 按
   `ingest/notifications.yaml` 的路由 + 严重度过滤发送邮件；幂等标记写入
   `reports/<DATE>/.sent/<alert_id>.json`。投资人报告（规划中的
   `scripts/send_reports.py`）共用同一套配置。凭据在 `~/.pricredit-env`
   （SMTP_HOST/PORT/USER/PASSWORD/FROM，参考 idvault）。

## 本仓库脚本（`scripts/`）

| 脚本 | 作用 | 状态 |
|------|------|------|
| `scripts/_edgar_common.py` | EDGAR HTTP 客户端：合规 UA、10 req/s 限流、429/503 退避、磁盘缓存。 | ✅ v0 |
| `scripts/discover_bdcs.py` | 经 EDGAR 全文搜索 N-54A（BDC 登记表）+ `company_tickers.json` 生成 `bdc/bdc_universe.json`。 | ✅ v0 |
| `scripts/fetch_filings.py` | 按 CIK 读取 `submissions.json`，下载指定表单（10-K/10-Q/8-K）。 | ✅ v0 |
| `scripts/run-daily-pricredit.sh` | **每日编排**：必要时刷新 universe → 拉取最新表单 → 解析 XBRL 事实 → 风险评分 → 可选分发告警邮件（`--send-alerts` / `--digest` / `--alert-dry-run`）。 | ✅ v0 |
| `scripts/parse_filings.py` | 10-K/10-Q XBRL company facts → 规范化 NAV/NII/杠杆/资产覆盖率/PIK 时间序列，支持 10-K/A 重述。详见 [`docs/XBRL_CONCEPT_MAP.md`](docs/XBRL_CONCEPT_MAP.md)。 | ✅ v0 |
| `scripts/_xbrl_concepts.py` | XBRL 标签 → PriCredit 规范化指标的映射表 + 正则兜底 + 衍生指标（杠杆、公允/成本、PIK 占比、分红覆盖率）。 | ✅ v0 |
| `scripts/compute_risk.py` | 风险评分：6 个因子 × 分段线性曲线 → 复合得分 / 分档 + 独立阈值规则 → `alert_*.json`。配置在 `ingest/risk_weights.yaml`，方法论见 [`docs/RISK_MODEL.md`](docs/RISK_MODEL.md)。 | ✅ v0 |
| `ingest/risk_weights.yaml` | 风险引擎的权重、曲线、阈值、告警规则；所有数值都可编辑后重算。 | ✅ v0 |
| `scripts/send_risk_alerts.py` / `send-risk-alerts.sh` | 读 `reports/<DATE>/alert_RISK-*.json`，按 `ingest/notifications.yaml` 的严重度过滤路由成邮件。支持 `--digest` 合并成一封、`--dry-run` 预览、基于 `.sent/` 目录的幂等。 | ✅ v0 |
| `scripts/extract_portfolio.py` | Schedule of Investments → 贷款级结构化数据（会补齐 non-accrual% 与行业 HHI 两个因子）。 | ⏳ 计划 |
| `scripts/build_investor_report.py` | 客户端投资人报告合成。 | ⏳ 计划 |
| `scripts/send_reports.py` | 投资人报告分发（复用 `send_risk_alerts.py` 的 SMTP / 路由层）。 | ⏳ 计划 |
| `scripts/requirements.txt` | Python 依赖（requests、lxml、openpyxl、pandas、pyyaml 等）。 | ✅ v0 |
| `scripts/README.md` | 脚本详细用法与环境变量。 | ✅ v0 |
| `ingest/notifications.yaml` | 报告收件人与过滤（默认收件人：`jianfengren.sd@gmail.com`）。 | ✅ v0（占位） |

## Skills

| Skill | 用途 |
|--------|------|
| `skills/aicrm-bdc-monitor/` | 每日 EDGAR 拉取 → 解析 → 风险 → 分发的顶层契约。|

## 安全与合规

- **EDGAR `User-Agent` 必须是真实联系人**；默认从 `PRICREDIT_UA_EMAIL`
  读取（示例：`jianfengren.sd@gmail.com`）。违规可能被封 IP。
  参考 [`docs/EDGAR_USAGE.md`](docs/EDGAR_USAGE.md)。
- **不要** 把原始 10-K/10-Q、解析产物、风险报告提交到 Git；见根目录
  `.gitignore`。
- 邮件凭据（SMTP_*）只放在 `~/.pricredit-env`（被 `.gitignore` 排除），
  `ingest/notifications.yaml` 只放非敏感的路由 + 过滤规则。
- 风险输出仅为**信息性**参考，不构成投资建议；每份告警/报告都要带免责声明。

# Owner's Office

> **不构成投资建议 / Not investment advice.** 本仓库记录的是一位个人投资者的方法和判断记录，不推荐任何证券，不给出交易信号，也不执行任何交易。This repository documents one private investor's method and track record. It recommends no security, produces no trading signals and executes no trades.

[中文](#中文) · [English](#english)

## 中文

Owner's Office 是一间按伯克希尔方式运转的“所有者办公室”。别的工具只评价企业；这里同时评价主人自己：作为所有者，主人的判断到底可不可靠。

- **论点即代码。** 每家公司的论点按软件项目维护：档案是源代码，“什么会证明我错”写成可执行的 thesis tests，新财报触发测试，合并一个 PR 就是一次有记录的决策。
- **先写下，后验证。** 财报前的预期带概率、判定标准和数据来源，在业绩首次公开之前登记并加时间戳；事后按事先写好的标准结算。
- **双向问责。** 管理层对股东的承诺，和系统、主人自己的预测，记在同一种账本里，用同一套规则结算。
- **反锚定。** 一个看不到论点的“盲推”模型独立作答；它和起草模型的分歧，是复核最该花时间的地方。
- **能力圈实测。** 已结算的预测按领域计算 Brier 分数和校准曲线，能力圈由记录画出来，而不是自我声明。
- **分权自治。** 各角色按公开的投资宪法自行运转，权限随历史准确率升降；只有资金事项和修宪交给主人。

### 仓库地图

| 仓库 | 可见性 | 内容 |
| --- | --- | --- |
| [thesis-ci](https://github.com/kentian742-creator/thesis-ci) | 公开 | 格式规范（JSON Schema）、lint、预注册核验、Brier 与校准工具 |
| owners-office（本仓库） | 公开 | 投资宪法与大佬原则、决策权、agent 定义、各公司的 thesis.yml 与两分钟故事、预注册、预测与覆盖、言行账本、股东信、错误清单 |
| owners-office-private | 私有 | 估值与价格参考、L3 备忘录、升级请求、系列排名、带金额的决策日志、完整报告 PDF、提示词 |

### 本仓库目录

| 路径 | 内容 |
| --- | --- |
| `docs/DESIGN.md` | 项目框架与工作思路 |
| `docs/STATUS.md`、`docs/decisions/` | 进度与决策记录 |
| `constitution/` | 投资宪法、大佬原则如何变成系统规则、三级决策权 |
| `agents/` | 十三个角色（公司经理、行业研究员、总部、抽取员、排版员，以及事实审计、模型审查、反方、合成审查、版面审查、盲推、判定员、结算员七个独立监督角色）各自的模型、可见范围与提示词编号 |
| `companies/<代码>/` | thesis.yml、两分钟故事、来源表、预注册、言行账本、更新记录 |
| `trust/` | 各角色的信任等级（由流水线维护） |
| `industries/<id>/` | 行业模块与可观测的路标 |
| `forecasts/`、`letters/`、`mistakes.md` | 预测与覆盖记录、股东信、错误清单 |
| `pipeline/`、`scripts/` | 模型调用入口（唯一的一处）、阶段验收脚本 |

### 不做什么

- 不产生买卖信号，不做回测，不预测股价。
- 不显示日常股价；价格只作为私有的参考数据（估值用的价格参照、估值模型自检用的历年年末收盘价、跨越事先写好的区间时的一次提醒）。公开文件里唯一会出现的价格，是公司自己披露的历史回购均价。
- 不执行任何交易：涉及资金的事项永远由主人本人决定、亲手执行。

### 许可

方法文档（`docs/`、`constitution/`、`agents/` 等）采用 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)；研究内容（`companies/`、`industries/`、`forecasts/`、`letters/`、`mistakes.md`）保留所有权利；代码（`pipeline/`、`scripts/`、`tests/`、`.github/`）采用 MIT。详见 [LICENSE.md](LICENSE.md)。

## English

Owner's Office is a one-person "owner's office" run the Berkshire way. Other tools evaluate companies; this one also evaluates the owner: it keeps measuring whether the owner's judgment as a business owner is actually reliable.

- **Thesis as code.** Each company's thesis is maintained like a software project: the dossier is the source, "what would prove me wrong" becomes executable thesis tests, new filings trigger the tests, and every merged pull request is a recorded decision.
- **Write it down first.** Pre-earnings expectations carry a probability, a resolution rule and a data source. They are registered and timestamped before results are public, then settled against the rules written in advance.
- **Two-way accountability.** Management's promises to shareholders and the system's and owner's own forecasts go into the same kind of ledger and are settled the same way.
- **Anti-anchoring.** A "blind reader" model that cannot see the thesis answers independently; where it disagrees with the drafting model is where review time goes.
- **Circle of competence, measured.** Settled forecasts are scored per domain (Brier score, calibration curves), so the circle of competence is drawn by the record rather than by self-assessment.
- **Decentralized autonomy.** Roles operate under a public investment constitution, and their autonomy grows or shrinks with their track record. Only money matters and constitutional amendments go to the owner.

### Repository map

| Repository | Visibility | Contents |
| --- | --- | --- |
| [thesis-ci](https://github.com/kentian742-creator/thesis-ci) | public | Format spec (JSON Schema), linter, pre-registration verification, Brier and calibration tools |
| owners-office (this repo) | public | Constitution and principles, decision rights, agent definitions, per-company thesis.yml and two-minute stories, pre-registrations, forecasts and overrides, say-do ledgers, shareholder letters, mistakes list |
| owners-office-private | private | Valuation and price reference data, L3 memos, escalation requests, series ranking, decision log with amounts, full report PDFs, prompts |

### What it does not do

- No trading signals, no backtests, no stock price predictions.
- No daily stock prices; prices exist only as private reference data (the price reference used for valuation, year-end closing prices for checking the valuation model, one alert when a pre-written range is crossed). The only price a public file may show is a company's own disclosed average buyback price.
- No trade execution: money matters are always decided and executed by the owner personally.

### License

Method documents (`docs/`, `constitution/`, `agents/`, etc.) are licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Research content (`companies/`, `industries/`, `forecasts/`, `letters/`, `mistakes.md`) is all rights reserved. Code (`pipeline/`, `scripts/`, `tests/`, `.github/`) is MIT. See [LICENSE.md](LICENSE.md).

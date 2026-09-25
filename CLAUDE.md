# Owner's Office · 常驻规则

这是 Owner's Office 的公开仓库：投资宪法、决策权、agent 定义、各公司的 thesis.yml 与两分钟故事、预注册、预测、言行账本和股东信。系统跟踪企业，也跟踪主人（Ken）作为所有者的判断是否可靠。设计以 `docs/DESIGN.md` 为准；文件格式以兄弟仓库 `../thesis-ci/spec/` 为准。

## 新会话怎么接着做

主人说“继续推进”时：

1. 读 `docs/STATUS.md`，从“下一步”的第一项做起；
2. 需要背景时再读 `docs/DESIGN.md` 的相关章节和 `docs/decisions/`；
3. 每做完一段就更新 `docs/STATUS.md`（进度、待办、卡点），让下一个会话能接上。

## 授权范围（来自 DESIGN.md 的开工指令）

- 当前阶段内的一切工程决策自己定：目录、schema、脚本、测试、CI。
- 设计文档没写到的细节，按 `constitution/` 的原则自行决定，并记进 `docs/decisions/`。
- 只有两类事情停下来问主人：涉及资金的操作，和修改投资宪法。其余不要问。
  - 资金操作包括买入、加仓、减仓、卖出，也包括提高模型预算（见 `docs/decisions/0003`）。
  - 修宪指改动 `constitution/` 里的规则本身；只改格式或补检查映射不算。
- 只有主人本人能做的事写进 STATUS 待办，不代做：把模型 API key 放进 GitHub Secrets（绝不贴进聊天或代码）、审阅后首次创建并推送 GitHub 仓库。
- SEC 访问用的 User-Agent 只放在工作区根目录的 `.env`（`SEC_USER_AGENT`），不进任何仓库、日志或提交（`docs/decisions/0013`）。
- 系统不会、也无法替主人交易；资金事项的默认选项永远是维持现状。

## 硬规则（开工指令原文，由 CI 执行）

| 硬规则 | 执行它的检查 |
| --- | --- |
| 档案中的每个数字必须带来源标签； | `C-SRC-TAG`、`C-SRC-FACT`（`C-SRC-ACCESSION` 报警） |
| 不抓取、不显示日常股价，价格只用于跨越价值区间时的提醒； | `C-NO-PRICE-FEED` |
| 公开内容里不出现买卖建议；不执行任何交易； | `C-PUBLIC-NO-ADVICE`、`C-NO-TRADING` |
| 价值区间和 L3 备忘录只存私有仓库，公开的 thesis.yml 不含 value_ranges； | `C-PUBLIC-NO-VALUATION`、`C-PUBLIC-NO-AMOUNTS` |
| 模型调用只能放在 pipeline/llm.py，并记录模型名、提示词版本和输入哈希。 | `C-LLM-ENTRY`；日志字段由 `tests/test_llm.py` 覆盖 |

价格的细则以 00 §H2 为准：允许出现的价格只有四类，除公司披露的历史回购均价外都只在私有文件里。密钥只放 GitHub Secrets，由 `C-NO-SECRETS` 拦截。检查定义见 `../thesis-ci/spec/checks.yml`。CI 不过的内容不合并；不要为了通过检查去改检查，要改就先写决策记录。

## 写档案的约定

- 来源标签写成 `[src:TAG#LOCATOR]`，命名按 `../thesis-ci/spec/SPEC.md` §3.3：自有报告 `<代码>-RPT<编号>-<日期>`，定期报告 `<代码>-<表格>-<财年期间>`（如 `AXP-10Q-FY2026Q2`），临时报告 `<代码>-<表格>-<EDGAR filing date>`。报告的定位符 `pN` 是 PDF 页码（`docs/decisions/0006`）。标签必须在文件所在目录或仓库根的 `sources.yml` 里登记。
- 不许编造数字：没有出处就留空（`null`）并记进待办。原文引用每处最多一句。
- 公开仓库不写价格、价值区间、由价格推出的回报或倍数、仓位金额；这些放 `../owners-office-private/`（00 §H4，`docs/decisions/0004`）。
- 文档以中文为主；README 中英双语，中文在前。

## 文件地图

| 路径 | 内容 |
| --- | --- |
| `docs/DESIGN.md` | 设计（权威） |
| `docs/STATUS.md` | 进度、待办、怎样接着做 |
| `docs/decisions/` | 决策记录：背景、选项、决定、理由、被否决的方案、日期 |
| `constitution/` | 投资宪法、大佬原则、规则与检查映射、三级决策权 |
| `agents/` | 各角色的模型、可见范围、提示词编号 |
| `companies/<TICKER>/` | thesis.yml、story.md、sources.yml、prereg/、ledger.yml、updates/ |
| `trust/levels.yml` | 各角色的信任等级（流水线维护，`C-TRUST-WRITE` 核对） |
| `industries/<id>/` | 行业模块与路标（不写持仓、不给建议） |
| `forecasts/`、`letters/`、`mistakes.md` | 预测与覆盖、股东信、错误清单 |
| `pipeline/llm.py` | 唯一的模型调用入口（日志在 `logs/`，不入库） |
| `scripts/accept.py` | 阶段验收脚本 |
| `../owners-office-private/` | 估值、L3 备忘录、升级请求、系列排名（`hq/`）、决策日志、PDF 报告、提示词 v3（`prompts/`） |
| `../thesis-ci/` | 格式规范、lint、selftest |

## 常用命令（在工作区根目录 `/Users/asuka/OwnersOffice` 下）

```bash
.venv/bin/thesis-ci lint owners-office
.venv/bin/thesis-ci lint owners-office-private --counterpart owners-office
.venv/bin/thesis-ci checks
.venv/bin/thesis-ci selftest
.venv/bin/python -m pytest -q owners-office/tests
.venv/bin/python owners-office/scripts/accept.py --phase 0 --write-report docs/acceptance/phase-0.md
```

仓库还没有提交时，验收加 `--allow-uncommitted`（结果只算临时通过）。验收通过即进入下一阶段，不需要主人签字；未通过写进股东信。

## 提交约定

- 一次提交只做一件事；标题写 `<范围>: <摘要>`，例如 `AXP: 更新 2026Q3 论点`、`pipeline: 预算守卫`、`decisions: 0008 ...`。
- 每条提交信息的最后一行必须是：

  ```text
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  ```

- 提交的作者邮箱用 GitHub 的 noreply 地址（各仓库本地的 `git config user.email`），不用个人邮箱：公开仓库的提交记录也是公开内容。
- 改变论点的 PR 打 `论点变更` 标签；改主意算成绩，不算污点。
- 私有仓库的文件不得复制进本仓库；推送前先跑 lint 和测试。

## 决策记录

设计文档没写到、需要自己拍板的事，写成 `docs/decisions/NNNN-slug.md`（编号递增），结构固定：背景 / 选项 / 决定 / 理由 / 被否决的方案 / 日期。推翻旧决定时写新记录并在旧记录里注明“已被 NNNN 取代”。

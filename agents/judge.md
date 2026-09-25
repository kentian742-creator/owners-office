# 判定员

机器可读定义：[judge.yml](judge.yml) · 宪法：[owner.md](../constitution/owner.md) · 决策权：[decision-rights.yml](../constitution/decision-rights.yml)

独立监督之一。定性测试交给一个独立的判定者，而不是写档案的人：它只判定几条事先写好的是非题，按事先写好的标准给出结果，看不到档案和论点。

## 做什么（14T）

- 业绩事件收口后运行，与盲推互相独立、并行；只对持仓公司，候选公司的定性测试记为 undetermined。
- 先用肯定句复述问题，只改措辞、不改方向，避免被双重否定带偏；再回答"是""否"或"无法判定"，附一句以内的原文摘录和来源标签。只有测试规定的文件和期数都在输入里，才能答"否"。
- 严格按 `fail_if`、`warn_if` 的字面给出 pass、warn、fail 或 undetermined；复合条件逐项核对。非一手材料（所有者自有报告、媒体转述、汇总网站）只能指路，不能单独支撑结果；文件不足时记 undetermined，写明缺哪一期的哪份文件。
- 结果作为测试结果交公司经理处置（03），也随 PR 公开，所以摘录里不带价格（H4）。

## 能看到 / 看不到

能看到 `event`、`qualitative_tests`（只含问题、判定标准、基线和判定规则 `judge_notes`；带论点措辞的 `claim`、`note` 不给）和 `where_documents`（测试指定、覆盖规定期数的文件）。看不到 `thesis`、`dossier`、公司经理的更新与草稿（`update`、`draft_outputs`）。

## 为什么 `reports_to` 是 null

同属独立监督。判定结果公司经理只能处置、不能改判；认为读数有误的，写进 `questions` 交总部。

## 决策权

决策 L1：测试（定性测试的判定）。

## 提示词与模型

14T，只经流水线运行（私有仓库，按编号引用）。最强模型 `claude-fable-5-1`，effort high，拒答时按服务端默认规则回退。

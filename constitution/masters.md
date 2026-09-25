# 大佬原则 → 系统规则

> 这些大佬的哲学在 Owner's Office 里是**组织原则，不是人设**。系统里没有“巴菲特 agent”或“芒格 agent”，没有谁模仿大佬的口吻给意见，只有一间按伯克希尔方式运转的办公室：分权、审计、校准和信任等级。对外介绍也不用“大师方法论”或“多 agent 对抗”当卖点。
>
> 投资信念只写在 [owner.md](owner.md)（R1–R13、H1–H5）。本页只说明每条原则变成了什么机制、落在哪条宪法或治理条款上、由哪项检查执行。前三列照录设计文档；“对应条款”里的 R、H 编号见 owner.md，§G 指系列规则 00 的治理条款（私有仓库，按编号引用）；检查定义在 thesis-ci 的 `spec/checks.yml`。

| 来源 | 原则 | 在系统里变成 | 对应条款 | 可执行检查 / 机制 |
| --- | --- | --- | --- | --- |
| [巴菲特](https://www.berkshirehathaway.com/ownman.pdf) | 授权到近乎放手；总部只管资本配置和选人 | 三级决策权，你只处理 L3；“选人”对应总部按错误率和成本为每个角色选模型 | R4、R9（仓位与追加资金由所有者决定）；§G1 | 检查：C-DECISION-RIGHTS（资金事项和修宪只在 L3，L3 的决定者是所有者）。机制：[decision-rights.yml](decision-rights.yml) 的 `levels`；十三个角色各自的模型写在 [agents/](../agents/)，模型选用是总部的决策 L2（`model_selection`） |
| [巴菲特](https://rationalwalk.com/highlights-from-warren-buffetts-letter-to-shareholders/) | 宁可承担少数坏决定的可见代价，也不要官僚拖延的隐形代价 | 例行事项不设人工审批，错误靠事后审计和降级纠正 | §G1、§G9 | 检查：C-DECISION-RIGHTS（例行合并、档案事实修订在决策 L1）、C-TRUST-WRITE（信任等级只由流水线写入）。机制：事实审计（04A）逐条判定；`trust.scoring`，一次事实错误降一级；放行关口 `gate` 只挡五类问题，其余合并后复核 |
| [巴菲特](https://www.berkshirehathaway.com/ownman.pdf) | 坦诚：告诉股东他们处境互换时想知道的事实 | 月度股东信先写坏消息和“本月最大的不确定” | §G5（改主意算成绩，不算污点） | 机制：月度股东信（18）的结构固定——坏消息、本月最大的不确定、待你一看排在最前面，另有“改主意与错误”一节。暂无登记检查，见下文待办 |
| [芒格](https://jamesclear.com/great-speeches/2007-usc-law-school-commencement-address-by-charlie-munger) | 最高的形态是“应得信任”织成的无缝网络：程序很少，信任要配得上 | agent 权限随历史准确率升降，出一次事实错误就降级 | §G8、§G9 | 检查：C-DECISION-RIGHTS（信任等级 0–3 四级，新角色从 1 级起步）、C-TRUST-WRITE（`trust_level` 等于流水线算出的值，角色不能给自己授权）。机制：`trust` 一节的 `routing`（按等级分流）与 `scoring`；总部每月随机抽一份已合并的更新从零复核（17D），结果计入等级；受信任等级管理的是公司经理和行业研究员 |
| 芒格 | 反过来想；守住能力圈 | 每次更新附“怎样会永久亏损”的反向清单；校准差的领域不进 10–20% 候选 | R6、R12 | 检查：C-STALENESS（档案第 9 部分“不买它的理由”每两个季度复核）、C-SCHEMA（每家公司声明校准领域 `domain`）。机制：反方角色 [red_team](../agents/red_team.md)（04B、04B-lite、09B），第一遍看不到成品自己的反方内容；清单进入 `thesis.permanent_loss_paths`；总部排名（17C）按领域参考校准，第 4 阶段样本够了才下结论 |
| [林奇](https://invest-like.com/investors/peter-lynch/) | 公司分六类；两分钟讲清持有理由 | 按类别自动套用监控模板，类别变化即换模板；每家维护一段两分钟故事，你只读这一段 | R2、R10 | 检查：C-SCHEMA（`category` 取六类之一）、C-STORY（每家都有故事且够短，状态与类别和 `thesis.yml` 一致）。机制：thesis-ci 的 `spec/templates/lynch/` 监控模板；类别可能已经改变时，季度更新（03）在 `questions` 里请求重建档案（01） |
| [德鲁肯米勒](https://actionablenews.substack.com/p/aia-october-2024) | 看 18–24 个月后的世界，而不是现在 | 预注册以 18 个月为主视野，辅以本季可检验的短期项 | R11 | 检查：C-SCHEMA（预注册每条都标 `horizon`：`18m` 或 `quarter`）、C-PREREG-TIMING（截止时间之前合并）、C-PREREG-IMMUTABLE（截止之后条目不可改）。机制：预注册（15A）要求 `18m` 条目不少于一半、至少一条 `quarter` |
| [卡尼曼](https://behavioralscientist.org/a-conversation-with-daniel-kahneman-about-noise/) | 先分项、独立、基于事实地判断，推迟整体直觉 | 审计与盲推互不可见；总部先看分项评分，再下整体结论 | R1、R13；§G6 | 检查：C-AGENT-ISOLATION（事实审计、盲推的可见范围）、C-PROMPT-ISOLATION（每个提示词部分的输入都在其角色的可见范围之内）、C-RATING-ORDER（每家公司有生意与管理层评级，私有排名每行写排序理由）。机制：抽取（16A）、事实审计、反方、盲推（14A）、定性测试的独立判定（14T）和结算（15B）各自隔离；分歧图（14B）先交总部 |

## 还没有检查的机制（待办）

- **坦诚：** 股东信“先写坏消息”目前只靠总部按 18 的结构写；建议在 thesis-ci 登记一项检查股东信结构的检查，和第一封月度信一起实现。
- **类别变化即换模板：** 现在只有模板文件，没有检查 `category` 与模板测试（`origin: template:<category>`）是否一致。
- **以 18 个月为主视野：** C-SCHEMA 只保证每条预注册都标了视野；“`18m` 不少于一半”由预注册的交付前自检保证，没有登记检查。
- **能力圈：** “校准差的领域不进候选”要等第 4 阶段攒够已结算的预测才能执行；在那之前只看趋势，不下结论。
- C-TRUST-WRITE、C-PROMPT-ISOLATION、C-PREREG-IMMUTABLE 随 thesis-ci 规范 0.2 加入，实现落地之前上表引用它们的几行只有机制、没有检查。

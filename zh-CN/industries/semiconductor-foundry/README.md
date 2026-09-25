> 中文版。英文原文：[industries/semiconductor-foundry/README.md](../../../industries/semiconductor-foundry/README.md)

# 半导体代工 · 行业模块

Semiconductor Foundry · 模块 id：`semiconductor-foundry` · 数据截至 2026-09-16 · 最近复核 2026-09-24

本模块把 Business Library 的行业研究《Semiconductor Foundry》（2026 年 9 月，原文存于私有仓库）整理成可被引用、可被机器检查的形式 [src:IND-FOUNDRY-2026-09#p1]。
它只描述行业本身，不评价任何公司，也不构成投资建议。
机器可读的完整版本见同目录的 `industry.yml`，来源标签见 `sources.yml`，页码 #pN 为 PDF 页码（与印刷页码一致）。
标“研究计算”的数字是研究作者在公开数字上的算术，标“本模块推论”的内容是本模块的推断，不是研究原话。

## 一段话结论

代工是一门学习曲线生意：每代制程的固定成本增长快于市场，能负担新节点的厂商越来越少，按 2020 年的统计，在 180nm 生产的芯片制造商有 94 家，在 5nm 只有 3 家 [src:IND-FOUNDRY-2026-09#p9]。
在最新节点上量产最多的厂商学得最快、赚得最多、再为下一个节点出资，加上不与客户竞争的承诺，TSMC 占前十大代工收入的份额从 2021 年第一季度的 55% 升至 2026 年第二季度的 72.5% [src:IND-FOUNDRY-2026-09#p3]。
代工层的其余厂商回报普通且随周期波动 [src:IND-FOUNDRY-2026-09#p3]，研究的一句话概括是 “Profit follows scarcity, not activity.” [src:IND-FOUNDRY-2026-09#p9]
研究认为，补贴更容易改变晶圆厂建在哪里，而不是谁赢 [src:IND-FOUNDRY-2026-09#p3]。

## 研究的四条核心论点

| 核心论点 | 研究所说的推翻条件 | 本模块的对应路标 |
| --- | --- | --- |
| 一、规模加学习是真实但会移动的护城河：领先制程上量产最多的厂商赢下每个节点，差距还在扩大 | 挑战者在 2nm 级工艺上以有竞争力的良率为旗舰客户量产高量产品 [src:IND-FOUNDRY-2026-09#p3] | FOUNDRY-SP1 |
| 二、代工已成为能赚到设计级利润的关口 | TSMC 毛利率在需求没有崩塌时连续数个季度低于公司自定的 56% 下限 [src:IND-FOUNDRY-2026-09#p3] | FOUNDRY-SP2 |
| 三、没有量产规模，钱买不到领先制程，补贴更容易改变工厂位置而不是赢家 | 受补贴的进入者靠外部客户在领先制程上实现持续盈利 [src:IND-FOUNDRY-2026-09#p3] | FOUNDRY-SP4、FOUNDRY-SP5 |
| 四、成熟制程是另一门周期性生意，价格将越来越多地由中国的扩产决定 | 中国继续扩产的同时，中国以外成熟制程利用率到 2028 年一直高于 90% [src:IND-FOUNDRY-2026-09#p3] | FOUNDRY-SP6 |

FOUNDRY-SP3 对应研究在 10–20 年视野下讨论的价值向封装与系统设计转移 [src:IND-FOUNDRY-2026-09#p16]，以及“新封装架构由现任者以外的公司从一开始主导”这一推翻情形 [src:IND-FOUNDRY-2026-09#p19]。

## 行业一览

| 维度 | 读数 | 出处 |
| --- | --- | --- |
| 规模 | 2025 年前十大代工厂收入约 1,700 亿美元，同比增长 26%（TrendForce 汇总公司数据） | [src:IND-FOUNDRY-2026-09#p4] |
| 宽口径 | TSMC 的 “Foundry 2.0” 口径（加上封装、测试、光罩及整合厂的非存储生产）约 3,050 亿美元，与上一行定义不同，研究只用它比较相对规模 | [src:IND-FOUNDRY-2026-09#p3] [src:IND-FOUNDRY-2026-09#p19] |
| 集中度 | TSMC 占前十大代工收入的 72.5%，Samsung 占 5.9%（2026 年第二季度） | [src:IND-FOUNDRY-2026-09#p4] |
| 利润率 | 2025 财年营业利润率：TSMC 50.8%，UMC 18.5%，GlobalFoundries 11.7% | [src:IND-FOUNDRY-2026-09#p4] |
| 资本强度 | TSMC 2025 财年资本开支占收入的 33%（新台币口径，研究计算），2006–2025 年平均约 37%（研究计算），一座领先制程逻辑晶圆厂的十年拥有成本为 350–430 亿美元（BCG 估计） | [src:IND-FOUNDRY-2026-09#p4] [src:IND-FOUNDRY-2026-09#p11] [src:IND-FOUNDRY-2026-09#p9] |
| 周期性 | 前十大代工厂季度合计收入从 2022 年第三季度的 352 亿美元降到 2023 年第一季度的 273 亿美元，两个季度下降 22%（研究计算） | [src:IND-FOUNDRY-2026-09#p4] [src:IND-FOUNDRY-2026-09#p15] |

## 价值怎样流动

- 代工厂按晶圆、并越来越多地按封装出租制造能力，价格由制程节点和该节点产能的稀缺程度决定，成本大部分固定，利润取决于制程结构、利用率，以及是否拥有客户在别处买不到的产能 [src:IND-FOUNDRY-2026-09#p6]。
- 代工在终端价值中的份额很小：按 Epoch AI 对 Nvidia B200 的成本模型推算（研究计算），TSMC 的逻辑裸片加 CoWoS 封装约 1,850 美元，约占售价的 5–6%，10% 的晶圆涨价只让一颗 3 万美元以上的芯片多出约 85 美元 [src:IND-FOUNDRY-2026-09#p7]。
- 利润按稀缺程度分配：成熟制程代工厂的营业利润率为 12–19%，独立封测厂只有 7–8%，因为最稀缺的封装形式 CoWoS 在代工厂内部完成 [src:IND-FOUNDRY-2026-09#p8]。
- 护城河必须在每个节点重新赢得 [src:IND-FOUNDRY-2026-09#p9]，TSMC 2025 年资本开支 409 亿美元，超过 UMC、GlobalFoundries 和 SMIC 的收入之和 237 亿美元（研究计算）[src:IND-FOUNDRY-2026-09#p10]。
- 国家成为所有者和守门人 [src:IND-FOUNDRY-2026-09#p19]，中国以受保护需求和国家资金支撑 SMIC，其 2025 年毛利率为 21%，同年 TSMC 为 59.9% [src:IND-FOUNDRY-2026-09#p13]。

## 路标

研究第 19 页列出了六个路标，本模块逐一编成 FOUNDRY-SP1 至 FOUNDRY-SP6 [src:IND-FOUNDRY-2026-09#p19]。
门槛是事先写下的判定标准，不需要出处；完整门槛、机器规则、数据来源和触发含义见 `industry.yml`。

| 编号 | 路标 | 触发门槛（摘要） | 检查频率 | 研究给出的读数 |
| --- | --- | --- | --- | --- |
| FOUNDRY-SP1 | 挑战者在 2nm 级工艺上为具名外部客户量产 | 挑战者或客户正式确认某个具名外部客户的产品在 Intel 18A/14A、Samsung 2nm、Rapidus 2nm 等 2nm 级工艺上量产出货，或 TSMC 占前十大代工收入的份额连续 4 个季度低于上年同季 | 每季 | 未触发，Samsung 2nm 的特斯拉项目据报道于 2027 年下半年在得州爬坡 [src:IND-FOUNDRY-2026-09#p16]，TSMC 份额在 2026 年第二季度为 72.5% [src:IND-FOUNDRY-2026-09#p4] |
| FOUNDRY-SP2 | 领先代工厂的毛利率下限与 AI 需求 | TSMC 报告的毛利率连续 3 个季度低于公司自定的 56% 下限 [src:IND-FOUNDRY-2026-09#p14]，或同一年度内下调资本开支指引，或 HPC 收入占比连续 2 个季度不高于上年同季 | 每季 | 毛利率 67.7%，HPC 占收入 66%（2026 年第二季度）[src:IND-FOUNDRY-2026-09#p16] |
| FOUNDRY-SP3 | 先进封装在代工收入中的比重 | TSMC 全年先进封装收入占比达到 15%（事先设定），或当期最新一代数据中心 AI 加速器量产时采用逻辑代工厂以外公司自己的集成封装架构 | 每季（占比按年读取） | 2025 年略高于 10% [src:IND-FOUNDRY-2026-09#p8] |
| FOUNDRY-SP4 | 中国代工体系的先进制程产出与利润率 | 中国大陆代工厂 5nm 级量产，或 SMIC 毛利率连续 4 个季度不低于 45%（跟随者毛利率区间上沿 [src:IND-FOUNDRY-2026-09#p4]），或中国成熟制程产能份额的实际估计超过 39%（2027 年预测区间上限 [src:IND-FOUNDRY-2026-09#p17]） | 每季 | SMIC 毛利率 25.3%，利用率 93.7%（2026 年第二季度）[src:IND-FOUNDRY-2026-09#p16] |
| FOUNDRY-SP5 | 关税、产能外迁与台湾生产 | 美国正式实施超出 2026 年 1 月措施的芯片关税，或 TSMC 海外厂毛利率稀释指引上限超过 4 个百分点，或台湾修改 N-2 规则，或台湾生产因冲突、封锁或制裁中断 | 每月 | 第二阶段关税截至 2026 年 9 月仍在考虑中 [src:IND-FOUNDRY-2026-09#p16] |
| FOUNDRY-SP6 | 中国以外成熟制程的产能利用率 | UMC 利用率连续 4 个季度高于 90% 且中国仍在扩产，或连续 2 个季度低于 80% | 每季 | 研究没有 2026 年读数，最近的是 2023–24 年下行期的 70–80% [src:IND-FOUNDRY-2026-09#p3] |

可能性用语沿用研究的定义：很可能指高于 80%，可能指 60–80%，不确定指 40–60%，不太可能指 20–40%，很不可能指低于 20% [src:IND-FOUNDRY-2026-09#p3]。

## 本模块怎样被使用

- 公司论点在自己一侧的 thesis.yml 中声明引用哪些行业模块（字段定义见 thesis-ci SPEC 第 4 节）；本模块不记录、也不评价任何引用它的公司。
- CI 按各路标的检查频率读取数据；某个路标越过门槛时，CI 为每个引用本模块的公司论点开一个复核 issue，并重跑其中覆盖该路标 `tests_to_rerun` 所列维度的测试。
- 门槛按“先写下，后验证”的原则登记，不需要出处；读数是事实，必须带出处。修改门槛应在下一次读数出来之前单独提交，并写明理由。

## English

This module turns the Business Library study *Semiconductor Foundry* (September 2026) into a citable, machine-checkable industry module [src:IND-FOUNDRY-2026-09#p1].
It describes the industry only; it does not assess any company and is not investment advice.
Foundry is a learning-curve business: fixed costs per node rise faster than the market, so each node supports fewer producers, and the largest-volume producer at the newest node tends to win it [src:IND-FOUNDRY-2026-09#p3].
TSMC's share of top-10 foundry revenue rose from 55% in 1Q21 to 72.5% in 2Q26, and in 2025 it earned more operating profit than the rest of the foundry layer combined (the study's calculation) [src:IND-FOUNDRY-2026-09#p3].
Elsewhere in the layer returns are ordinary and cyclical, and the study judges that subsidies move where fabs sit more easily than who wins [src:IND-FOUNDRY-2026-09#p3].

The six signposts follow the list on page 19 of the study; each has a threshold written before the outcome, a check frequency and a named public data source in `industry.yml` [src:IND-FOUNDRY-2026-09#p19].

1. SP1 (quarterly): a named external customer's product shipping in volume on a challenger's 2nm-class process, or a sustained year-on-year fall in TSMC's share of top-10 foundry revenue.
2. SP2 (quarterly): TSMC's reported gross margin below its own through-cycle floor for three straight quarters, an in-year cut to capex guidance, or a stalling HPC revenue share.
3. SP3 (quarterly; the share is read once a year): the advanced-packaging share of TSMC revenue reaching a pre-set level, or a current-generation data-centre AI accelerator in volume using a packaging architecture developed by a firm other than its logic foundry.
4. SP4 (quarterly): 5nm-class volume output by a mainland Chinese foundry, SMIC's gross margin reaching the followers' historical ceiling for four straight quarters, or China's actual mature-node capacity share passing the top of the study's projected range.
5. SP5 (monthly): a formal second phase of US chip tariffs, wider guided margin dilution from TSMC's overseas fabs, a change to Taiwan's N-2 rule, or an interruption of wafer production in Taiwan by conflict, blockade or sanctions.
6. SP6 (quarterly): UMC's utilisation staying very high for four straight quarters while China keeps adding capacity, or falling back into the 2023–24 downturn range for two straight quarters.

Company theses reference this module from their own side; when a signpost is crossed, CI opens a review issue for each thesis that references it and reruns the tests covering the dimensions listed under `tests_to_rerun`.

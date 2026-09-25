# Semiconductor Foundry · Industry module

A Chinese version is in [zh-CN/industries/semiconductor-foundry/README.md](../../zh-CN/industries/semiconductor-foundry/README.md).

Semiconductor Foundry · module id: `semiconductor-foundry` · data as of 2026-09-16 · last reviewed 2026-09-24

This module turns the Business Library industry study *Semiconductor Foundry* (September 2026; the original is kept in the private repository) into a form that can be cited and checked by machine [src:IND-FOUNDRY-2026-09#p1].
It describes the industry only, does not assess any company and is not investment advice.
The complete machine-readable version is `industry.yml` in the same directory, the source tags are in `sources.yml`, and page locators #pN are PDF page numbers (the same as the printed page numbers).
Numbers marked “the study's calculation” are the study authors' arithmetic on published figures, and content marked “this module's inference” is this module's inference, not the study's words.

## Conclusion in one paragraph

Foundry is a learning-curve business: the fixed cost of each process generation grows faster than the market, so fewer and fewer firms can afford a new node, and by a 2020 count 94 chipmakers produced at 180nm but only 3 at 5nm [src:IND-FOUNDRY-2026-09#p9].
The firm with the most volume on the newest node learns fastest, earns most and funds the next node, and together with its commitment not to compete with its customers, this lifted TSMC's share of top-10 foundry revenue from 55% in Q1 2021 to 72.5% in Q2 2026 [src:IND-FOUNDRY-2026-09#p3].
Returns for the rest of the foundry layer are ordinary and cyclical [src:IND-FOUNDRY-2026-09#p3]; the study sums it up in one sentence: “Profit follows scarcity, not activity.” [src:IND-FOUNDRY-2026-09#p9]
The study holds that subsidies change where fabs are built more easily than who wins [src:IND-FOUNDRY-2026-09#p3].

## The study's four core theses

| Core thesis | What the study says would overturn it | Matching signpost in this module |
| --- | --- | --- |
| First: scale plus learning is a real but movable moat; the firm with the most volume on the leading process wins each node, and the gap is widening | A challenger producing a high-volume product in volume for a flagship customer on a 2nm-class process, with competitive yields [src:IND-FOUNDRY-2026-09#p3] | FOUNDRY-SP1 |
| Second: foundry has become a toll gate that earns design-level margins | TSMC's gross margin below its self-set floor of 56% for several quarters in a row without a collapse in demand [src:IND-FOUNDRY-2026-09#p3] | FOUNDRY-SP2 |
| Third: without volume, money cannot buy the leading process, and subsidies change the location of fabs more easily than the winners | A subsidised entrant earning sustained profits on the leading process with external customers [src:IND-FOUNDRY-2026-09#p3] | FOUNDRY-SP4, FOUNDRY-SP5 |
| Fourth: mature nodes are a separate, cyclical business whose prices will increasingly be set by China's capacity expansion | Mature-node utilisation outside China staying above 90% through 2028 while China keeps expanding [src:IND-FOUNDRY-2026-09#p3] | FOUNDRY-SP6 |

FOUNDRY-SP3 corresponds to the shift of value towards packaging and system design that the study discusses on its 10–20 year horizon [src:IND-FOUNDRY-2026-09#p16], and to the overturning case “a new packaging architecture led from the start by a company other than the incumbents” [src:IND-FOUNDRY-2026-09#p19].

## The industry at a glance

| Dimension | Reading | Source |
| --- | --- | --- |
| Size | Top-10 foundry revenue of about $170 billion in 2025, up 26% year on year (TrendForce compilation of company data) | [src:IND-FOUNDRY-2026-09#p4] |
| Broad definition | TSMC's “Foundry 2.0” definition (adding packaging, testing, photomasks and integrated device makers' non-memory production), about $305 billion, is defined differently from the row above, and the study uses it only to compare relative size | [src:IND-FOUNDRY-2026-09#p3] [src:IND-FOUNDRY-2026-09#p19] |
| Concentration | TSMC 72.5% and Samsung 5.9% of top-10 foundry revenue (Q2 2026) | [src:IND-FOUNDRY-2026-09#p4] |
| Margins | Operating margin in fiscal 2025: TSMC 50.8%, UMC 18.5%, GlobalFoundries 11.7% | [src:IND-FOUNDRY-2026-09#p4] |
| Capital intensity | TSMC's capital expenditure was 33% of revenue in fiscal 2025 (NT dollar basis, the study's calculation) and averaged about 37% over 2006–2025 (the study's calculation), and the ten-year cost of ownership of a leading-edge logic fab is $35–43 billion (BCG estimate) | [src:IND-FOUNDRY-2026-09#p4] [src:IND-FOUNDRY-2026-09#p11] [src:IND-FOUNDRY-2026-09#p9] |
| Cyclicality | Combined quarterly revenue of the top-10 foundries fell from $35.2 billion in Q3 2022 to $27.3 billion in Q1 2023, down 22% in two quarters (the study's calculation) | [src:IND-FOUNDRY-2026-09#p4] [src:IND-FOUNDRY-2026-09#p15] |

## How value flows

- Foundries rent out manufacturing capacity by the wafer and, increasingly, by the package; the price is set by the process node and by how scarce capacity at that node is, costs are largely fixed, and profit depends on the process mix, utilisation, and whether the foundry has capacity that customers cannot buy elsewhere [src:IND-FOUNDRY-2026-09#p6].
- Foundry's share of end value is small: by Epoch AI's cost model of the Nvidia B200 (the study's calculation), TSMC's logic dies plus CoWoS packaging cost about $1,850, about 5–6% of the selling price, and a 10% wafer price rise adds only about $85 to a chip that sells for more than $30,000 [src:IND-FOUNDRY-2026-09#p7].
- Profit is allocated by scarcity: mature-node foundries earn operating margins of 12–19% and independent packaging and test houses only 7–8%, because CoWoS, the scarcest form of packaging, is done inside the foundry [src:IND-FOUNDRY-2026-09#p8].
- The moat has to be won again at every node [src:IND-FOUNDRY-2026-09#p9]; TSMC's capital expenditure of $40.9 billion in 2025 exceeded the combined revenue of UMC, GlobalFoundries and SMIC, $23.7 billion (the study's calculation) [src:IND-FOUNDRY-2026-09#p10].
- States have become owners and gatekeepers [src:IND-FOUNDRY-2026-09#p19]; China supports SMIC with protected demand and state money, and SMIC's gross margin in 2025 was 21%, against 59.9% at TSMC in the same year [src:IND-FOUNDRY-2026-09#p13].

## Signposts

Page 19 of the study lists six signposts, which this module encodes one by one as FOUNDRY-SP1 to FOUNDRY-SP6 [src:IND-FOUNDRY-2026-09#p19].
Thresholds are criteria written down in advance and need no source; the full thresholds, machine rules, data sources and what a trigger means are in `industry.yml`.

| ID | Signpost | Trigger threshold (summary) | Check frequency | Reading given by the study |
| --- | --- | --- | --- | --- |
| FOUNDRY-SP1 | A challenger producing in volume for a named external customer on a 2nm-class process | The challenger or the customer formally confirms that a named external customer's product is shipping in volume on a 2nm-class process such as Intel 18A/14A, Samsung 2nm or Rapidus 2nm, or TSMC's share of top-10 foundry revenue is below the same quarter a year earlier for 4 consecutive quarters | Quarterly | Not triggered: Samsung's 2nm Tesla project is reportedly ramping in Texas in the second half of 2027 [src:IND-FOUNDRY-2026-09#p16], and TSMC's share was 72.5% in Q2 2026 [src:IND-FOUNDRY-2026-09#p4] |
| FOUNDRY-SP2 | The leading foundry's gross margin floor and AI demand | TSMC's reported gross margin below its self-set floor of 56% for 3 consecutive quarters [src:IND-FOUNDRY-2026-09#p14], or a cut to capital expenditure guidance within the same year, or the HPC share of revenue no higher than a year earlier for 2 consecutive quarters | Quarterly | Gross margin 67.7%, HPC 66% of revenue (Q2 2026) [src:IND-FOUNDRY-2026-09#p16] |
| FOUNDRY-SP3 | Advanced packaging's share of foundry revenue | TSMC's full-year advanced packaging share of revenue reaching 15% (set in advance), or the current latest generation of data-centre AI accelerators using, in volume production, an integrated packaging architecture of a company other than the logic foundry | Quarterly (the share is read annually) | Slightly over 10% in 2025 [src:IND-FOUNDRY-2026-09#p8] |
| FOUNDRY-SP4 | Leading-edge output and margins of China's foundry system | Volume production at 5nm class by a mainland Chinese foundry, or SMIC's gross margin at or above 45% for 4 consecutive quarters (the top of the followers' gross margin range [src:IND-FOUNDRY-2026-09#p4]), or an actual estimate of China's mature-node capacity share above 39% (the top of the projected range for 2027 [src:IND-FOUNDRY-2026-09#p17]) | Quarterly | SMIC gross margin 25.3%, utilisation 93.7% (Q2 2026) [src:IND-FOUNDRY-2026-09#p16] |
| FOUNDRY-SP5 | Tariffs, relocation of capacity and production in Taiwan | The US formally imposes chip tariffs beyond the January 2026 measures, or the upper end of TSMC's guidance for margin dilution from overseas fabs exceeds 4 percentage points, or Taiwan amends the N-2 rule, or production in Taiwan is interrupted by conflict, blockade or sanctions | Monthly | The second phase of tariffs was still under consideration as of September 2026 [src:IND-FOUNDRY-2026-09#p16] |
| FOUNDRY-SP6 | Mature-node capacity utilisation outside China | UMC's utilisation above 90% for 4 consecutive quarters while China is still expanding, or below 80% for 2 consecutive quarters | Quarterly | The study has no 2026 reading, and the latest is 70–80% in the 2023–24 downturn [src:IND-FOUNDRY-2026-09#p3] |

Probability terms follow the study's definitions: very likely means above 80%, likely 60–80%, uncertain 40–60%, unlikely 20–40%, and very unlikely below 20% [src:IND-FOUNDRY-2026-09#p3].

## How this module is used

- Company theses declare on their own side, in thesis.yml, which industry modules they reference (the field is defined in section 4 of the thesis-ci SPEC); this module neither records nor assesses any company that references it.
- CI reads the data at each signpost's check frequency; when a signpost crosses its threshold, CI opens a review issue for each company thesis that references this module and re-runs the tests in it that cover the dimensions listed in the signpost's `tests_to_rerun`.
- Thresholds are registered on the principle “write it down first, verify later” and need no source; readings are facts and must carry a source. A threshold change should be committed separately, with the reason stated, before the next reading comes out.

## Short summary

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

# Investment constitution

A Chinese version is in [zh-CN/constitution/owner.md](../zh-CN/constitution/owner.md).

> **This file is the only place in Owner's Office where investment beliefs are written down.** The masters' principles ([masters.md](masters.md)), decision rights ([decision-rights.yml](decision-rights.yml)), role definitions ([agents/](../agents/)), company archives and letters to the owner may only cite the clauses here; they may not set up investment beliefs of their own or rewrite these.

- **Version:** v2, in force from 2026-09-24; it can be changed only through the amendment procedure at the end of this file.
- **Source:** the owner's own investment constitution (English notes, version of 2026-09-16; the original is kept in the private repository, with account amounts and the holdings snapshot removed). The interpretations follow the original rule by rule and add no beliefs.
- **Relation to series rules 00:** 00 is the prompt rulebook that drives all research work. It is kept with the prompts in the private repository; public files cite its clauses (e.g. 00 §V13) and the prompts (e.g. 17C) by id only, see [decisions/0009](../docs/decisions/0009-prompt-set-v3.md). 00 §C is a rule-by-rule summary of this file: the numbers and rule text of R1–R13 are identical to §C, word for word. If the two differ, §C prevails and the discrepancy goes to HQ. Priority: hard rules (H) > investment constitution (R) > the rest of 00 > rules specific to a single prompt.
- **The owner's role:** the system runs itself within this constitution. The owner does only two things: capital allocation (buying, adding, trimming, selling) and amending the constitution.
- **Structure:** a preamble says who the owner is and what the owner wants; the thirteen rules of the investment constitution (R1–R13) are the rules the system governs itself by; the five hard rules (H1–H5) are boundaries that no role at any trust level may cross; the amendment procedure comes last. Each rule has four parts: the rule, its interpretation, how the system enforces it, and the executable checks.
- **Enforcement:** each rule maps to at least one executable check; the checks are defined in thesis-ci's `spec/checks.yml`. The mapping from rules to checks also exists in machine-readable form in [rules.yml](rules.yml), and the rule parameters are in [decision-rights.yml](decision-rights.yml). Many rules are judgments, and a machine can check only the record they leave; the `how_checked` field in `rules.yml` states plainly how far each check reaches.
- **How to read it:** numbers in the rules are rule parameters, not facts, and carry no source tags.

## Preamble: who the owner is and what the owner wants

Not numbered, and not an enforcement parameter; it explains where the thirteen rules below come from.

- **An owner-type investor.** Considers themselves a long-term, business-owner-type investor: buying a stock means buying part of a business, not trading price moves.
- **Borrowing, not copying.** Draws on Buffett's and Munger's thinking, but not mechanically: applies it through an owner's-mindset lens rather than imitation.
- **Long-term objective.** Seeks approximately 8–10%+ annualized compounding over time, while recognizing that actual returns depend on business performance, valuation, capital allocation and market conditions. This is a self-description, not a valuation parameter: the discount rate contains no personal required return (00 §V1), and the hurdle for buying and holding is Berkshire or VOO (R7).
- **Still growing.** Sees judgment, patience and emotional control as abilities that improve with experience; their own investing discipline and temperament are still being forged.
- **Scope.** This constitution governs a concentrated portfolio of a few core businesses, which deliberately holds no broad-market index funds. Outside that portfolio there is a separate plan that buys VOO every month; it is outside the scope of this constitution and of the system.

## I. Investment constitution (R1–R13)

### R1 Order and price

**Rule:** Business quality first, management quality second, valuation third. Prefers "fair price for an excellent business" over "cheap price for an ordinary business". Fair price does not mean any price: still requires a meaningful margin of safety relative to business quality, future return expectations, and uncertainty. Prefers buying excellent businesses at fair prices rather than requiring extreme undervaluation — the cost of missing a great business's long-term compounding while waiting for a perfect price outweighs the value of further price optimization.

**Interpretation:** The analysis runs from the business to management to valuation: a great business beats a cheap price. Price affects future returns, but business quality determines the ability to compound over the long term; the owner won't buy a business the owner doesn't respect just because it is cheap. For a business the owner does respect, they also won't wait indefinitely for a perfect or extreme low price; but a fair price does not mean any price, and there must still be a margin of safety that matches the business's quality, the expected return and the uncertainty.

**How the system enforces it:**
- When a company manager builds an archive (01A, 01B), the business is written first and management second, each with its own letter grade; the Munger matrix placement must be made directly, without hedging (00 §M1). The overall grades in research reports (02, 11) likewise start from the business and management; the grade given to the price appears only in private files.
- HQ's quarterly ranking (17C) is a combined judgment under 00 §V13: return and certainty are weighed together, and the business and management grades are part of certainty. It does not sort mechanically by any single metric, nor by a fixed "business → management → valuation" order.
- "Fair price does not mean any price": the margin of safety is stated separately as a percentage discount (00 §V2) and checked item by item by the model review (04C). "Rather than requiring extreme undervaluation": rules such as "no new money while the price is inside the reasonable range" have been removed; whether to put in new money is decided by the owner, on a memo drafted by HQ ([decisions/0010](../docs/decisions/0010-price-grade-rubric.md)).

**Executable checks:** C-RATING-ORDER

### R2 The quality touchstone

**Rule:** "If the stock market closed for 10 years, would I still want to own this business?" — looks for a durable competitive advantage/moat, pricing power, high capital returns, and growing free cash flow.

**Interpretation:** The question takes price out of the judgment: with no quote to look at for ten years, the return can only come from the business's own operations. The owner looks for a durable competitive advantage (a moat), pricing power, high returns on capital and growing free cash flow. In the owner's view, shareholder returns come from earnings growth, free-cash-flow growth, buybacks, dividend growth and a strengthening moat.

**How the system enforces it:**
- When building the archive, the company manager turns the thesis breakers in the dossier into breaker tests and the monitoring dashboard into watch tests (01B); together they must cover moat, pricing power, returns on capital and free cash flow.
- When new filings come in, the pipeline computes the quantitative tests, the judge (14T) rules independently on the qualitative tests, and the company manager handles the results under 00 §G3 (03).
- Staleness tests put judgments such as the moat on a review schedule; the business half of the Munger matrix is argued with this touchstone (00 §M2).

**Executable checks:** C-TESTS-COVERAGE, C-STALENESS

### R3 Management

**Rule:** Management quality test: whether management allocates capital rationally, thinks like owners, prioritizes long-term shareholder value, and uses buybacks/dividends/reinvestment sensibly.

**Interpretation:** Management is judged mainly by how it uses shareholders' money: whether it allocates capital rationally, whether it thinks like an owner, whether it puts long-term shareholder value first, and whether it uses buybacks, dividends and reinvestment sensibly. This is the owner's test of management quality; in the order of analysis it comes after business quality and before valuation (R1).

**How the system enforces it:**
- Each company has at least one test of capital allocation and one test of management (01B).
- The management half of the Munger matrix is argued against two computable criteria: five-year cumulative losses of non-core or loss-making businesses ÷ after-tax operating profit for the same period; and whether shareholder returns in the same period were offset by new debt or equity financing (00 §V14). Past buybacks are judged against the price and earnings of the time; this calculation is done only in the private repository (H4).
- The say-do ledger records management's numeric targets, capital expenditure plans, buyback authorizations and product timelines. The settler (15B) settles them in four grades — kept, partially kept, not kept, silently dropped — and checks whether promises not kept were acknowledged unprompted.

**Executable checks:** C-TESTS-CAPALLOC

### R4 Concentration and position size

**Rule:** Prefers a concentrated portfolio of a small number of high-conviction businesses, typically around 4–5 core holdings, rather than broad diversification; if a business isn't worth a meaningful position, it isn't worth owning at all. Position-sizing rule: 10–20% is a qualification threshold for buying at all, not a mandatory allocation target. Position size should NOT come from historical volatility, max-drawdown math, or generic risk models — it should come from depth of understanding, business quality, management quality, long-term certainty, and valuation margin of safety. These position-sizing principles are a framework for capital allocation, not rigid rules to be applied mechanically. The owner decides position size.

**Interpretation:** The portfolio holds only a few high-conviction businesses, usually four or five. The owner doesn't want small positions in businesses they can't see through: if a business isn't worth a meaningful position, it isn't worth owning. 10–20% is the bar a business must clear to be bought at all (at the owner's portfolio size, a 5% position isn't meaningful), not a target that must be filled. The actual weight depends on conviction, valuation, the opportunities the portfolio faces and the asymmetry of risk and reward; a business that is better understood, fundamentally stronger and more attractive on risk and reward can get a larger position. Position size is not computed from historical volatility, maximum drawdown or generic risk models. This is a framework for capital allocation, not rules to be applied mechanically.

**How the system enforces it:**
- `decision-rights.yml`: `portfolio.max_holdings: 5`, `portfolio.entry_band: [0.10, 0.20]`.
- HQ's quarterly ranking (17C) puts first the companies best suited to be the largest (10–20%) positions and takes the top three outside the holdings as candidates (decision level L2).
- The owner decides position size: buying, adding, trimming and selling are decision level L3 only. Buy and add memos drafted by HQ state a target weight (17B). Below the 10% entry bar a company does not qualify, and no buy memo is written; above 20% is allowed (a better-understood, higher-conviction business can get a larger position), but the reason must be stated. With more than about 4–5 holdings the check only warns; it does not fail ([decisions/0014](../docs/decisions/0014-r4-r7-checks-follow-owner-text.md)).
- Company-level deliverables state no position percentages or ladders; the only exception is in "Note: on position size" below.

**Executable checks:** C-CONCENTRATION, C-DECISION-RIGHTS

### R5 The place of valuation

**Rule:** Distrusts P/E-based valuation, while still wanting an explicit approximate value, since without one the owner feels lost. DCF is just an approximation/replication of value, not a precise instrument, and shouldn't be pushed to make a business look too over- or under-valued. DCF is a supporting framework, not a decision-making engine — valuation should inform an investment decision but should not override business quality. Details of the discount rate are in §V1.

**Interpretation:** Since reading Buffett and Munger, the owner distrusts valuation based on the price/earnings ratio and wants valuation grounded in the real drivers — the moat and management. But the owner still wants an explicit approximate value; without one there is nothing to judge by. DCF is only an approximation of value, not a precision instrument, and should not be pushed to make a business look too over- or undervalued. It informs the investment decision but does not override business quality: what matters most is always the business itself. The discount rate equals the prevailing long-term risk-free rate plus a risk premium specific to the business: in general, more predictable, higher-quality businesses carry a lower premium, and less predictable or ordinary ones a higher premium. About 6–10% is only a practical reference under current interest rates, not a hard-coded rule (not "MSFT is always 6%" or "an ordinary company is always 10%"); the whole discount-rate framework changes with interest rates, business conditions and uncertainty.

**How the system enforces it:**
- Discount rate = 10-year US Treasury yield (with the date and source of the reading) + a risk premium specific to the company (00 §V1). The premium prices only the volatility and predictability of this company's cash flows through a full cycle, judged from the company's own cash-flow record: no lookup tables, no tiers, no numbers read off grades or Munger matrix cells, and no interpolation between the premiums of other companies in the series (00 §V10). "Higher quality, generally a lower premium" is a tendency, not a hard constraint across companies ([decisions/0012](../docs/decisions/0012-discount-rate-no-cross-company.md)). The discount rate contains no personal required return of the owner.
- Valuations exist only in the private repository (H4). They give ranges only, never a precise entry point; a report uses one discount rate; the margin of safety is stated separately (00 §V2, §V7, §V8). Recalculation happens only in archive builds (01C), valuation refreshes (02) and report revisions (05). The result is first a pending version and takes effect only when the model review (04C) approves it (decision level L2).
- The valuation section of the public archive states only the method and judgments, never numbers derived from the price ([decisions/0004](../docs/decisions/0004-valuation-private-even-for-msft.md)).

**Executable checks:** C-DISCOUNT-RATE

### R6 Holding and selling

**Rule:** Ideal holding period is indefinite — holds as long as a company's fundamentals, moat, and management haven't permanently deteriorated; treats 1–3 years only as a minimum observation window, not the real target. Sells only for permanent deterioration: a permanently damaged moat, a fundamentally changed business model, declining management quality, seriously bad capital allocation, or finding a clearly better long-term opportunity. Will not sell because of price declines, a recession, market panic, or a short-term earnings miss on an otherwise-sound business.

**Interpretation:** The ideal holding period is indefinite: a business is held as long as its fundamentals, moat and management have not permanently deteriorated; 1–3 years is only the minimum observation window, not the real target. The owner sells only for permanent deterioration — a permanently damaged moat, a fundamentally changed business model, declining management quality, seriously bad capital allocation — or on finding a clearly better long-term opportunity. A price decline, a recession, market panic, or a short-term earnings miss at an otherwise healthy business is not a reason to sell. As long as intrinsic value, fundamentals and management are not permanently impaired, the owner is willing to accept deep drawdowns of half or more.

**How the system enforces it:**
- A failed test is not a sell (00 §G3). A failed breaker test has only two dispositions: a false trigger (with primary evidence; the test is corrected, effective only going forward), or a confirmed trigger (that part of the investment case is void, and the disposition says how the whole thesis changes); "thesis maintained" is not an allowed disposition. For a failed watch test or any warning, the company manager writes maintain, revise or pending. All dispositions are completed within 7 days (03).
- After a confirmed trigger, the company manager submits an escalation request to HQ only if the conclusion touches one of four causes inside the business — a permanently damaged moat, a fundamentally changed business model, declining management quality, seriously bad capital allocation. A "clearly better opportunity" is a cross-company judgment, raised only by HQ on the basis of the quarterly ranking (17B, 17C).
- Memos are drafted only by HQ. The sell reason must be one of the five, and the four reasons that are not allowed are listed in `memo.forbidden_sell_reasons`. The default option is always to maintain the status quo; if the owner does not reply within 14 days, the default applies.

**Executable checks:** C-SELL-REASONS, C-DEFAULT-HOLD, C-SCHEMA

### R7 Opportunity cost

**Rule:** Won't buy or hold a position unless its expected long-term return/quality clears Berkshire or VOO as the comparison bar (when a holding falls below the comparison bar, HQ judges in 17C whether this constitutes R6's "clearly better long-term opportunity"). Investment ideas are compared against existing holdings and high-quality alternatives; only replaces or adds a name if it's higher-or-similar quality, better long-term return, and reasonably valued. Prefers owning a small number of excellent businesses for the long run over continuously hunting new names. Won't add capital purely to hit a target position weight, and won't invest simply because cash is sitting idle — treats un-deployed cash as optionality/dry powder rather than a gap that must be filled.

**Interpretation:** Buying or continuing to hold requires that the expected long-term return and quality clear the comparison bar of Berkshire or VOO. A new idea is compared with existing holdings and with high-quality alternatives: only if its quality is similar or higher, its long-term return better and its price reasonable does it replace a holding or get added. The owner would rather own a few excellent businesses for the long run than keep hunting for new names. Cash not yet invested is optionality and dry powder, not a gap that must be filled: the owner doesn't add money to reach a target weight, and doesn't invest because cash is sitting idle.

**How the system enforces it:**
- Private valuations record two reference anchors: the forward expected returns of Berkshire and of VOO, taken from the current versions of their own archives, with a reference date (00 §V6). The order of the hurdles is an engineering default taken from the practice of the series reports: Berkshire's long-term return estimate at its current price is the first hurdle, and the index's forward return is the second reference. The original says only "Berkshire or VOO" and does not say to take the higher of the two; the owner can overrule this default at any time.
- When a holding falls below the hurdle, HQ judges in the quarterly ranking (17C) whether this constitutes R6's "clearly better long-term opportunity"; a hurdle alarm by itself is not a reason to escalate (00 §G3).
- When no company clears the hurdle, HQ writes no buy memo and the cash stays.

**Executable checks:** C-HURDLE (enforced under the engineering default above, not taking the higher of the two; [decisions/0014](../docs/decisions/0014-r4-r7-checks-follow-owner-text.md))

### R8 Trade rarely

**Rule:** Prefers building a position in one order rather than several small tranches (per-trade minimum commission is material at the owner's portfolio size); dislikes frequent trading generally. Avoid chasing the "perfect" entry point.

**Interpretation:** At the owner's portfolio size the minimum commission per trade is material, so the owner prefers to build a position in one order rather than split it into several small ones; in general they dislike frequent trading. They do not chase the perfect entry point: putting off a purchase again and again to wait for a better price is exactly the bad trade-off that R1 describes.

**How the system enforces it:**
- `portfolio.single_order_entry: true`. Buy and add memos drafted by HQ describe a single order (`order.type: single`), with no staged plan and no prices tied to the timing of buying or selling (17B).
- The target is no more than 2 L3 memos a month. If there are more, they are not held back: HQ explains the reason in the letter to the owner and raises the escalation threshold (decision level L2, reported).
- The system never places orders: the owner executes the trade personally at the broker and replies "executed", and the system then completes the decision log.

**Executable checks:** C-SINGLE-ORDER

### R9 A decline is not a thesis

**Rule:** A market-wide decline is not, by itself, a reason to change the thesis; if the broad market or the core businesses fall meaningfully and long-term expected returns rise, the owner would consider adding fresh capital to increase core positions rather than treating the decline as risk (proposed by HQ, decided by the owner; decision level L3).

**Interpretation:** A market-wide decline is not, by itself, a reason to change the thesis. If the broad market or the core businesses fall sharply and long-term expected returns rise as a result, the owner would consider putting in fresh money to increase core holdings, rather than treating the decline as risk.

**How the system enforces it:**
- Price silence (H2): company managers cannot see daily share prices, so a decline by itself cannot become a reason in a quarterly update (03).
- Adding capital is a cross-company judgment; only HQ proposes it, in a memo based on the quarterly ranking (17B, 17C). A company manager's escalation request does not cover R9. Whether to add is decided by the owner (decision level L3), and the default is still to maintain the status quo.

**Executable checks:** C-DECISION-RIGHTS, C-SCHEMA

### R10 Five steps before a decision

**Rule:** Understand the business → evaluate moat and management → estimate long-term value creation → compare expected return against existing holdings → decide whether the current price offers a reasonable margin of safety.

**Interpretation:** Every decision goes through five steps first: understand the business; evaluate the moat and management; estimate long-term value creation; compare the expected return with existing holdings; judge whether the current price offers a reasonable margin of safety. The goal is not to find the cheapest stock but to own exceptional businesses at sensible prices.

**How the system enforces it:**
- The twelve parts of the dossier follow this order: they start with the business, and put moat, capital allocation and management before valuation (01A). The research report (02) and the complete company report (11) likewise put the business first and the price last.
- When HQ drafts an L3 memo (17B), it states the facts and sources, the tests triggered, the clauses cited, at least two options (one of them maintaining the status quo), "what the default would miss" and "where acting is most likely to be wrong". Buy and add memos also set out the comparison with the hurdle (R7) and the target weight (R4).

**Executable checks:** C-SCHEMA

### R11 Record the process; don't grade by results

**Rule:** The journal records decisions rather than grading them by results — the aim is to train the reasoning process, since good decisions can produce bad results and bad decisions good results.

**Interpretation:** The decision log records decisions themselves rather than grading them by their results: what the owner wants to train is the reasoning process. Each entry has nine fields: date, decision (buy, sell, add or trim), company, original thesis, assumptions, expected outcome, risks (what would prove the judgment wrong), review date, and the outcome, filled in later.

**How the system enforces it:**
- The decision log is kept only in the private repository (it carries amounts, H4), and its nine fields are fixed by the schema. Once an L3 memo is decided, the system generates a draft log entry linked to the PR.
- The same principle applies to the system's own judgments — write it down first, verify later. Pre-registrations are written, merged and timestamped before the results come out (15A), and their entries cannot change after the deadline; test thresholds are frozen once the earnings filings are in (00 §G7); the settler cannot see probabilities or authors, records only whether an expectation came true, and does not use that to judge the decision made at the time (15B); Brier scores are computed by the pipeline.

**Executable checks:** C-SCHEMA, C-PREREG-TIMING, C-PREREG-IMMUTABLE, C-TEST-FROZEN

### R12 Circle of competence

**Rule:** Maintains general humility about investing outside the owner's circle of competence; interest does not equal an investment decision without sufficient understanding. By the owner's own account: strongest understanding in cloud and enterprise software; semiconductors are an area where the owner continues learning, given their cyclicality and how hard it is to predict long-term winners.

**Interpretation:** By the owner's own account, the owner understands cloud and enterprise software best (MSFT is the example), and stays humble about investing outside that circle of competence. Semiconductors are an area where the owner is still learning: they are highly cyclical, and long-term winners are hard to predict. The owner follows themes such as AI applications, the Internet of Things, physical AI and factory automation, but interest is not an investment decision: without enough understanding, the owner does not invest.

**How the system enforces it:**
- Each company declares a calibration domain, `domain`, in `thesis.yml`. It is chosen by 01B when the company is set up and copied unchanged from then on (00 §G8); pre-registrations and forecasts are booked by domain.
- Settled forecasts get Brier scores and calibration curves by domain, with the system's forecasts and the owner's overrides kept in separate books. HQ's ranking (17C) takes calibration into account: companies in a domain with a clearly poor record are not put among the 10–20% candidates. Until the sample is large enough, only the trend is read and no conclusions are drawn.

**Executable checks:** C-SCHEMA

### R13 Temperament

**Rule:** Avoid over-trading, avoid changing long-term judgment because of price moves, avoid chasing the "perfect" entry point, keep price volatility separate from permanent business impairment, and stay patient.

**Interpretation:** These are the things the owner wants to be reminded of again and again: don't over-trade, don't change long-term judgments because of price moves, don't chase the perfect entry point, keep price volatility separate from permanent impairment of the business, and stay patient. The owner has a standing interest in the cognitive biases that bear on trading decisions.

**How the system enforces it:** These reminders are built into the system as defaults instead of being left to self-discipline:
- price silence (H2): daily share prices enter no judgment;
- money matters default to maintaining the status quo, and no reply within 14 days means the default; no more than 2 memos a month, and a buy is written as a single order;
- the blind read (14A) cannot see the thesis, which counters anchoring; the letter to the owner (18) puts bad news first, and a change of mind counts as an achievement, not a blemish.

**Executable checks:** C-SINGLE-ORDER, C-NO-TRADING, C-NO-PRICE-FEED, C-DEFAULT-HOLD

### Note: on position size (00 §C)

Company-level deliverables (01–16) state no position percentages, no position ladders and no tables of the kind "at price X, build a Y% position". The only exception: the private research report (02) and the complete company report (11) may include one sentence on "whether the company qualifies for the 10–20% entry bar, and where it falls short (business, management or price)", without a specific position size. The order of the series ranking is not position advice.

## II. Hard rules (H1–H5)

The hard rules rank above the thirteen rules above (00 §0); no role at any trust level may cross them. As far as possible they are CI checks, and a change that fails them cannot be merged. They were first written in the start instructions of the design document, and 00 §H gave them their present form; they are owner clauses as well.

> **Wording in the public version.** This file is public. A few terms in the original sentences of 00 §H2 item 1 and §H4 are themselves on the list of banned terms, and under H4 they may not appear in public files (not even in a negative sentence), so below they are replaced by descriptions with the same meaning. The complete list of banned terms is maintained by thesis-ci's public-content checks (C-PUBLIC-NO-VALUATION, C-PUBLIC-NO-ADVICE) and must match 00 §H4.

### H1 Sources

**Rule:** Every factual number in archives, reports and updates carries a source tag (§E1). A number without a source goes into no deliverable.

**How the system enforces it:** Markdown uses `[src:TAG#LOC]`, YAML uses `source: TAG#LOC`; each TAG is registered in the `sources.yml` of the corresponding repository (00 §E1). Numbers from training memory are not a source: what cannot be found is written as "not found / cannot be verified" and entered in the unknowns register (00 §E3, §E6). The extractor (16A) breaks the product into atomic facts, and the fact audit (04A, 09A, 12A) checks them one by one against the primary sources.

**Executable checks:** C-SRC-TAG, C-SRC-FACT, C-SRC-ACCESSION

### H2 Price silence

**Rule:** Do not fetch or display daily share prices, and do not write any price from memory. Only four kinds of price may appear, each with a date and a source: (1) the "price reference" in private files (valuations, research reports, complete company reports), used for the grade given to the price, for price ÷ central value, and for return estimates derived from the price; (2) the unadjusted year-end closing prices of past years that the five-year backtest needs, kept only in the private valuation files; (3) historical average buyback prices disclosed in company filings — these are facts about the company and may go into the public `ledger.yml`; (4) a one-time alert when a value range written in advance is crossed. (1) and (2) are supplied by the pipeline as the `price_reference` and `year_end_closes` inputs; if they are not supplied, nothing is written and the gap goes into the unknowns register. The reason for silence: daily share prices anchor judgment to the price, and this system judges the business.

**How the system enforces it:** Code and workflows connect to no market-data source. The price reference and year-end closes are supplied only by the pipeline, as inputs, and kept only in the private repository. Company managers cannot see daily share prices and do not need them (03).

**Executable checks:** C-NO-PRICE-FEED

### H3 No buy or sell advice, no trading

**Rule:** No output contains an order instruction.

**How the system enforces it:** The system has no broker or order interface of any kind and never trades on the owner's behalf. Money matters reach the owner only as a one-page memo (decision level L3), and the owner executes the trade personally at the broker. Public content contains no buy or sell advice wording.

**Executable checks:** C-PUBLIC-NO-ADVICE, C-NO-TRADING

### H4 Public/private separation

**Rule:** Value ranges, the central value, the discount rate, return estimates derived from the price, the grade given to the price, the price reference, L3 memos, escalation requests and decision records with amounts are kept only in the private repository. Public files (including negative sentences, comments, YAML notes, PR bodies, PR attachments and issues) may not contain these numbers, nor any term on the list of banned terms. Public files also state no multiples or ratios that take the current share price as an input (P/E, market capitalization, free-cash-flow yield, etc.). Where private content must be referred to, write only "valuation: see private files". Operating numbers such as returns on capital, margins, growth rates and test thresholds are written as usual.

**How the system enforces it:** The pipeline decides where each output goes, under 00 §F2. Valuations, memos, escalation requests, decision logs, rankings and most audit outputs go only into the private repository. The fact-audit conclusions, the inversion list, the divergence map and the qualitative-test verdicts that come with a quarterly-update PR are published with it, and they follow this rule too. The valuation section of the public archive states only the method ([decisions/0004](../docs/decisions/0004-valuation-private-even-for-msft.md)).

**Executable checks:** C-PUBLIC-NO-VALUATION, C-PUBLIC-NO-AMOUNTS

### H5 Reproducibility

**Rule:** Model calls go only through `pipeline/llm.py`, which records the model name, the version of 00, the prompt version (git hash) and the input hash. Prompts do not specify models. Content not produced through `llm.py` (including the output of manual conversations) cannot be merged directly; it serves only as a lead and can be merged only after the pipeline has rerun it.

**How the system enforces it:** The model each role uses is written in `agents/<role>.yml`. Steps that need isolation run only through the pipeline, and API calls have no memory (00 §G6). When a conclusion changes, one can tell whether the world changed or the prompt did.

**Executable checks:** C-LLM-ENTRY

## III. Amendment procedure

1. **Amending the constitution is decision level L3, and only the owner can decide it.** `decision-rights.yml` lists `amend_constitution` only under L3; no role may change the rules on its own.
2. **What counts as an amendment.**
   - Changing any rule in this file (R1–R13, H1–H5).
   - Changing the owner clauses of series rules 00 — §H, §C, §E, §P, §V, §M, §W — or the design system 00D. They come from the owner's investment philosophy, evidence discipline, and writing and design preferences, and changing them ranks with an amendment. The values in them marked "engineering default" are defaults added to make the rules executable: the owner can overrule them at any time, and the system cannot change them on its own.
   - Changing the parameters in `decision-rights.yml` that directly express rules (`memo`, `portfolio`, `ranking.rule`), or `levels` and `trust`, which set each role's own authority: a role cannot grant itself authority.
3. **What does not count as an amendment.** Changes to process and format in 00 §F and §G, and in the non-owner clauses of the prompts, are decision level L2 (`prompt_change`) and are reported in the monthly letter. Adding or replacing a check for a rule, or fixing wording where `rules.yml` and this file disagree, is an engineering decision recorded in `docs/decisions/`; but no rule may lose its checks as a result. The other operating parameters (phase advancement, escalation rules, the number of candidates and the rotation schedule) are defaults that the design document set on the owner's behalf; a change to them is recorded in `docs/decisions/` and reported in the monthly letter. Raising the budget cap is the owner's decision.
4. **A proposal is a one-page memo.** HQ drafts it (17B) with `action: amend_constitution`. It states which rule changes, the old and the new text, why (citing records such as the mistakes list, calibration data or divergence rulings), and which checks and parameters must change with it; it gives at least two options, one of which keeps the current text. The memo is kept only in the private repository.
5. **The default is to keep the current text.** If the owner does not reply within 14 days, the default applies and the rule stays as it is.
6. **The annual letter is the regular window.** Amendment proposals are, as a rule, saved up and put forward together in the annual letter to the owner, for the owner to decide at one time. A proposal made mid-year also goes through a one-page memo and counts toward the target of no more than 2 memos a month. The owner can also amend the constitution on their own initiative at any time; HQ then carries out the change as the owner decided and records it in one line in the next letter.
7. **Conflicts between rules.** When HQ finds a conflict between earlier instructions from the owner, it first makes a procedural ruling (decision level L2), reports it in the letter, and submits it to the owner for confirmation at the next revision of 00 (00 §G2).
8. **Once adopted, everything changes at once.** This file, the corresponding clauses of 00, `rules.yml` and `decision-rights.yml` are changed in the same change, and this file and 00 §C are kept identical in numbering and rule text; the version number goes up by one, and a line is added to the revision record below.

**Executable checks:** C-DECISION-RIGHTS, C-CONSTITUTION-MAP

## Revision record

| Version | Date | Change | Basis |
| --- | --- | --- | --- |
| v1 | 2026-09-24 | Compiled from the eight constitution points and five hard rules of the design document | Design document |
| v2 | 2026-09-24 | Rebuilt from the full text of the owner's own investment constitution: R1–R13 match 00 §C in numbering and rule text, and H1–H5 follow 00 §H; preamble added; the amendment procedure names the owner clauses of 00. Old wording that conflicted with 00 was removed: "a higher-quality company may not have a higher premium" (see R5 and decisions/0012); R7 using the higher of Berkshire and VOO as the hurdle (changed to the engineering default of 00 §V6); a quality-to-premium mapping table written into this rule (conflicted with 00 §V1); a ranking sorted mechanically in a fixed order (conflicted with 00 §V13); allowing the thesis to be maintained after a breaker failure, with the company manager proposing a trim (conflicted with 00 §G3); prices used only for alerts when a range is crossed (changed to the four kinds of 00 §H2). Alignment only; no new investment beliefs | The owner's original investment constitution; prompt set v3, confirmed by the owner on 2026-09-24 |
| v2.1 | 2026-09-24 | The "how the system enforces it" parts and the checks of R4 and R7 changed to follow the original text: a target weight below 10% is an error, above 20% is allowed with a stated reason, and more than about 4–5 holdings only warns; the hurdle has Berkshire as the first hurdle and VOO as the second reference, not the higher of the two. Rule text and interpretations unchanged | The owner's original text; decisions/0014 |
| v2.2 | 2026-09-25 | English-first: English text, Chinese in zh-CN/ | owner's instruction |

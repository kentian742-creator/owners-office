# Payments & Card Networks

A Chinese version is in [zh-CN/industries/payments-card-networks/README.md](../../zh-CN/industries/payments-card-networks/README.md).

Industry module `payments-card-networks` · data as of 2026-09-16 [src:IND-PAYMENTS-2026-09#p22] · reviewed 2026-09-24 · based on the Business Library industry study *Payments & Card Networks* (September 2026 edition) [src:IND-PAYMENTS-2026-09#p1]

> This module describes the industry only. It contains no investment view on any company and is not investment advice. For signposts, thresholds and readings, [`industry.yml`](industry.yml) is authoritative; this file is a one-page summary.

## In one paragraph

A card payment is a fee split: the merchant pays, the issuer takes most of the fee and returns much of it to cardholders as rewards, acquirers compete on price, and the network in the middle keeps only a small share [src:IND-PAYMENTS-2026-09#p3]. That thin slice is nonetheless the most valuable position in the chain, because the network controls the rules and the acceptance network that connect a large number of banks and merchants, and it bears no cardholder credit risk [src:IND-PAYMENTS-2026-09#p3]. Interchange caps have so far only moved profit among issuers, merchants, acquirers and consumers, and cards have lost their lead only in markets where the state built or protected an alternative rail, and even there what was displaced first was cash, transfers and debit cards, not credit cards [src:IND-PAYMENTS-2026-09#p3].

## Key numbers

- In 2024 US card spending was $11.9 trillion, and merchants paid $187.2 billion to accept cards, a rate of 1.57%, of which credit cards 2.30% and debit and prepaid cards 0.71% (derived) [src:IND-PAYMENTS-2026-09#p6].
- Visa's fiscal 2025 net revenue of $40 billion equalled 0.28% of $14.2 trillion of payments volume, and Mastercard's $32.8 billion in 2025 equalled 0.31% of $10.6 trillion of gross dollar volume (both derived) [src:IND-PAYMENTS-2026-09#p6].
- The networks' operating margins are 58% to 66% (Mastercard 58% on a GAAP basis in 2025, Visa 66% in fiscal 2025 excluding the litigation provision), and capital expenditure including capitalised software is about 3.7% of net revenue [src:IND-PAYMENTS-2026-09#p4].
- The issuing layer swings widely with the credit cycle: the pre-tax return on assets of US credit card banks was 3.9% in 2024 and −5.3% in 2009 [src:IND-PAYMENTS-2026-09#p4].
- In 2024 Visa and Mastercard together accounted for 87% of the purchase volume of the four major US general-purpose card brands [src:IND-PAYMENTS-2026-09#p4].

## How the money flows

For every $100 spent on a US rewards credit card (the study's illustrative estimate): the merchant pays about $2.30 → the issuer collects about $1.80 of interchange, of which about $1.50 goes back to the cardholder as rewards and related costs, leaving about $0.25 before servicing, fraud and funding costs → the network keeps about $0.15 to $0.25 from the two sides combined → acquirers and processors keep about $0.35 (the remainder) [src:IND-PAYMENTS-2026-09#p6]. Value is created at the two ends but kept by the middle layer, which is the hardest to replace [src:IND-PAYMENTS-2026-09#p8]. In the three-party model (American Express, and Discover's own cards) one company issues cards, acquires merchants and runs the network, and keeps the whole discount fee [src:IND-PAYMENTS-2026-09#p6].

## The study's four core judgements

1. **Thin slice, thick margins.** What the networks sell is coordination: a set of rules and an acceptance network that no bank can copy, charged as a percentage of nominal spending at almost zero marginal cost [src:IND-PAYMENTS-2026-09#p3].
2. **Caps only move profit around the networks.** After the Durbin Amendment, the networks' share of the US debit fee pool rose from 18% in 2011 to 28% in 2023 [src:IND-PAYMENTS-2026-09#p9]. After the EU cap (measured by the annual net effect over 2015 to 2017), the networks gained €550 million a year and the issuers lost €2.95 billion a year [src:IND-PAYMENTS-2026-09#p10].
3. **Only state-shaped markets have displaced cards, and debit went first.** In India debit card payments fell 67% from 2021 to 2025 while credit card payments grew 2.6-fold, and in Brazil, in the second half of 2025, debit card payments were roughly flat while credit card payments grew 9.4% [src:IND-PAYMENTS-2026-09#p3].
4. **Scale in acquiring looks like a moat, but is not.** FIS bought Worldpay for about $43 billion and then wrote down $24.4 billion, and Adyen kept a 53% EBITDA margin while its take rate fell from about 31 basis points to about 17 [src:IND-PAYMENTS-2026-09#p3].

The study's disruption-risk judgement by horizon: low over 1 to 5 years, medium over 5 to 10 years, medium over 10 to 20 years [src:IND-PAYMENTS-2026-09#p4]. Near-term pressure falls on price and is absorbed mainly by issuers and cardholders; in the medium term, A2A payments will take debit-like payments wherever a state or a bank consortium pushes them; the long-term question is whether the networks remain the payment credential for AI agents and tokenised money [src:IND-PAYMENTS-2026-09#p19].

The study's probability terms: very likely (above 80%), likely (60% to 80%), uncertain (40% to 60%), unlikely (20% to 40%), very unlikely (below 20%) [src:IND-PAYMENTS-2026-09#p3]. In `industry.yml`, numbers marked “derived” are the study authors' arithmetic on published figures, and sentences marked “this module's inference” are inferences by this module.

## Signposts

| ID | What to watch | Crossed when (summary) | Frequency | Current reading |
| --- | --- | --- | --- | --- |
| PAY-SP1 | US honour-all-cards and single-network routing rules | The settlement allowing acceptance by card category takes effect, or a credit card routing mandate is enacted, or the DOJ's Visa debit case produces a remedy, or Visa's share of US debit transactions falls below half | Monthly | Not crossed: the final approval hearing is set for 2026-11-16, the CCCA has not passed, and the DOJ case has no trial date yet [src:IND-PAYMENTS-2026-09#p17] |
| PAY-SP2 | Price controls on scheme and processing fees in major markets | Any major market formally adopts, after 2026-09-16, a binding price control on Visa's or Mastercard's scheme or processing fees; measures that only require transparency or pricing governance do not count | Monthly | Not crossed: the UK remedies of July 2026 only require transparency and pricing governance [src:IND-PAYMENTS-2026-09#p10] |
| PAY-SP3 | Net revenue per dollar and issuer incentives | Net revenue per dollar falls by a cumulative fifth from fiscal 2025 because of competition, or Visa's incentive share rises for three straight years from fiscal 2026 | Annual | Visa 0.28%, Mastercard 0.31% (Visa fiscal 2025, Mastercard 2025, derived), Visa incentives 28.3% of gross revenue [src:IND-PAYMENTS-2026-09#p6][src:IND-PAYMENTS-2026-09#p10] |
| PAY-SP4 | Whether A2A displaces credit cards in India and Brazil | The number of credit card payments in either country falls year on year for two consecutive half-years while credit on UPI or Pix is growing | Half-yearly | Brazil's credit card payments grew 9.4% year on year in the second half of 2025 (the study does not say whether by count or by value), and India's number of credit card payments grew 2.6-fold from 2021 to 2025 [src:IND-PAYMENTS-2026-09#p14][src:IND-PAYMENTS-2026-09#p18] |
| PAY-SP5 | A2A at the checkout in Europe and the US | wero or the digital euro reaches a tenth or more of in-store payments by count in the euro area, or digital euro legislation is passed, or US card spending lags household consumption for three straight years while A2A rises | Half-yearly | Not crossed: wero has 57 million users, in-store payments are planned to launch in 2026, and the digital euro legislation is not yet complete [src:IND-PAYMENTS-2026-09#p17] |
| PAY-SP6 | AI agents, stablecoins and cross-border yield | Most agent purchases do not use network tokens, or non-card wallets and stablecoins take more than a tenth of US in-store spending, or cross-border yield falls by a cumulative fifth because of competition | Half-yearly | No reading yet: the study gives none of the three metrics; Visa's annualised stablecoin settlement of more than $20 billion is the network's own settlement and does not count [src:IND-PAYMENTS-2026-09#p18] |

Compression of cross-border yield is already part of the study's base case (the study judges it “likely”, after 2030) [src:IND-PAYMENTS-2026-09#p18][src:IND-PAYMENTS-2026-09#p22], so when the cross-border branch of PAY-SP6 is crossed, what needs review is the speed of the compression; it does not mean that the study's judgement has been overturned.

## How this module is used

- Company archives declare in their own `thesis.yml` which industry modules they reference. The reference is recorded only on the company side; this module lists no companies.
- The industry researcher reads the data at each signpost's frequency. When any signpost crosses its threshold, CI opens a review issue for every company that declares a reference to this module and re-runs those of its thesis tests that cover the dimensions listed in the signpost's `tests_to_rerun` (such as moat or pricing power).
- Every number is quoted from the industry study above with its PDF page; the source tags are in [`sources.yml`](sources.yml). Thresholds are criteria written down in advance, not facts.

## Short summary

This industry module summarises the Business Library study of September 2026 (data as of 2026-09-16, reviewed 2026-09-24) [src:IND-PAYMENTS-2026-09#p1].
A card payment is a fee split: the merchant pays, the issuer collects most of the fee and returns much of it as rewards, acquirers compete on price, and the network in the middle keeps a thin but steady slice [src:IND-PAYMENTS-2026-09#p3].
In 2024 US merchants paid $187.2bn to accept cards, 1.57% of $11.9tn of purchases, while Visa's fiscal-2025 net revenue equalled 0.28% of its payments volume (derived) [src:IND-PAYMENTS-2026-09#p6].
Six signposts (PAY-SP1 to PAY-SP6) track what the study says could change the industry's economics: US routing and honour-all-cards rules, price controls on scheme fees, network pricing power, A2A rails reaching credit or the checkout, and control of the payment credential by AI agents and tokenised money, plus cross-border yield.
Thresholds are criteria written in advance, not facts; readings and sources live in `industry.yml`.
Company theses declare which industry modules they reference, and this module names none of them; when a signpost is crossed, CI opens review issues for those companies and re-runs the affected thesis tests.
The module describes the industry only and is not investment advice.

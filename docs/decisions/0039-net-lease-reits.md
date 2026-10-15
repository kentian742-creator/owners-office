# 0039 Research coverage adds three net-lease REITs: VICI Properties, NNN REIT and Agree Realty

> Adds to [0032](0032-research-coverage-widened.md). Holdings are unchanged.

## Background

On 2026-10-14 the owner named the companies they follow most closely: AppLovin (APP), VICI Properties (VICI), NNN
REIT (NNN) and Agree Realty (ADC). AppLovin is already a holding. The three REITs have no archive.

All three are net-lease REITs: they own properties let on long leases under which the tenant pays the property's
taxes, insurance and upkeep. VICI owns casinos and other experiential properties; NNN and Agree own single-tenant
retail properties. Their economics are read through funds from operations (FFO), adjusted FFO (AFFO), rent
escalators, lease terms, tenant concentration, occupancy, cap rates on acquisitions, and the cost and maturity of
debt and equity. The pipeline's dossier and valuation prompts were written for operating companies, whose cash comes
from selling goods and services.

## Decision

- **Research coverage (`status: candidate`)** grows from twelve companies to fifteen: VICI, NNN and ADC join.
  `ranking.candidates` in `constitution/decision-rights.yml` goes from 12 to 15.
- **The three go on `companies/intake.yml`** and are built from the SEC filings, like the other archives.
- **An industry module comes first:** `industries/net-lease-reits/` sets out how the pipeline reads a net-lease REIT:
  the definitions of FFO (Nareit) and of each company's AFFO, lease and tenant measures, the balance-sheet tests, and
  how a valuation treats a business that must pay out most of its taxable income and grows by issuing equity and debt.
  The dossiers cite it.
- **Order:** the owner's focus puts the three ahead of the other companies waiting for an archive. Each archive starts
  after the company's third-quarter 10-Q, so that it begins from the latest quarter: Agree in late October, VICI in
  late October, NNN in early November. AppLovin's rebuild (0032) follows its third-quarter 10-Q in early November.

## Consequences

- Fifteen archives in coverage cost more model calls. The owner's subscription is now smaller (2026-10-14), so the
  queue runs one company at a time and stops at the subscription's limits.
- A company joins the STATUS earnings calendar once its archive exists.
- Valuations and reports stay in the private repository (§H4).

## Date

2026-10-14

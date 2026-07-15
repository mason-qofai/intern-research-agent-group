name: private-data-approximator
description: When no provided source gives a desired statistic for a portfolio company, use this skill. Search SEC/EDGAR for comparable stats from publicly held companies. If a statistic is found or to be found in the sources/ directory files, this is not the right skill. Retrieve desired statistic from SEC filing and input into the portfolio company overview where it should be. This skill should stop being used when the desired statistic(s) for the current focus portfolio company is/are found.

--

# private-data-approximator: Input/Output Contract

Unlike the other seven skills, this skill uses live web search rather than
sources/. Per source-tier rules below, this is the one deliberate exception
to the pipeline's otherwise local-files-only sourcing model.

## Source-tier rules

This skill searches SEC/EDGAR only. For any comp company's own reported
financials (revenue, segment breakdown, margins), search and cite SEC
filings (10-K, 10-Q, proxy statements) directly. These are authoritative,
full-text, and never paywalled.

Do not use non-SEC sources for adjustment factors, margin benchmarks, or
market-position discounts. If no SEC comp exists at all for the target company's
sector and scale, withhold the estimate.

## Input contract

| Field | Type | Required | Source |
|---|---|---|---|
| portco_profile | object | yes | portco-profiler output |
| comp_set_criteria | {sector, size_range, geography} | no | user, else inferred from portco_profile |

## Output contract

| Field | Type |
|---|---|
| revenue_band | string (low-high, currency, as-of year), wide if directional, or null only if withheld |
| ebitda_band | string (low-high), wide if directional, or null only if withheld |
| margin_assumption | string (percent range), or null only if withheld |
| comp_set_used | list[{company, metric, value, source_url}] |
| estimate_type | enum: "analytical" (comp closely matches scale/model), "directional" (comp mismatched, band widened accordingly), or null if withheld |
| confidence | must be "low" or "medium", never "high" (this output is inherently an estimate); null if withheld |
| source_type | must be "public inference"; null if withheld |
| withheld | boolean, true only when fewer than one accessible SEC comp candidate exists after screening |

This skill must never label its own output as source_type "public document." If it does, that's a bug in the skill logic worth flagging in audit-pass.

If the only available SEC comp differs from the target company in scale, business model, or market position, produce a wide-band, explicitly labeled directional estimate: state the comp used and name the specific mismatch (scale, business model, market position). Confidence for this kind of estimate is always `low`. Do not withhold merely because the comp is mismatched; a wide range with a stated mismatch is more useful than no range at all.

Reserve `withheld: true` for the case where fewer than one accessible SEC comp candidate exists after screening. Withholding is the exception, not the default outcome whenever a comp isn't a close match.

A margin assumption should not compound onto a revenue figure that was itself withheld; two compounded approximations from no real base would produce a number with nothing underneath it.

## Output format (hybrid)

Output is comp_set_used as a short table in markdown, followed by a trailing fenced JSON block titled `## Claims`. Unlike the other research skills, this skill fills `confidence` and `source_type` itself rather than leaving them `null` (except when withheld, where both are null by design), since the constraint on those fields (never "high," never "public document") is this skill's own responsibility to enforce, not confidence-scorer's or source-typer's:

```json
[
  {
    "claim_id": "private-data-approximator-<company>-001",
    "field": "revenue_band",
    "text": "comp used, whether estimate is directional or analytical, specific mismatch if directional, or null if withheld",
    "confidence": "low",
    "source_type": "public inference",
    "estimate_type": "directional",
    "withheld": false,
    "citation": "SEC filing URL, or null if withheld"
  }
]
```

confidence-scorer and source-typer still run over these claims in the batch pass like any other, but should treat this skill's self-assigned values as a strong prior, not overwrite them without a documented reason in audit-pass. Withheld claims are excluded from confidence-scorer and source-typer's batch entirely, since there is nothing to rate.

name: portco-profiler
description: Used once per portfolio company to write the overview of each specific portco. References portfolio company list to profile one at a time. Profiles one company per invocation. After all sources relevant to the current focus portfolio company have been exhausted for information, the current invocation of this skill is complete.

--

# portco-profiler: Input/Output Contract

## Input contract

| Field | Type | Required | Source |
|---|---|---|---|
| company_name | string | yes | user or portfolio-discoverer entry |
| parent_firm | string | yes | user or firm-profiler output |
| company_website | string | no | user |
| depth | enum: quick / standard / deep | no, default standard | user |

## Output contract

| Field | Type |
|---|---|
| business_description | string |
| hq_location | string |
| employee_count_estimate | string (range) |
| sector | string |
| notable_events | list[{date, event}] |
| citations | list[{claim_id, url}] |

Every field above carries `confidence` (high/medium/low) and `source_type` (public document/public inference; no private inference category, the target fact being private doesn't change the classification). This output is the required input to private-data-approximator.

## Output format (hybrid)

Output is a markdown body (business_description, notable_events, etc, as normal prose/list sections) followed by a trailing fenced JSON block titled `## Claims`:

```json
[
  {
    "claim_id": "portco-profiler-<company>-001",
    "field": "hq_location",
    "text": "one-sentence statement of the claim",
    "confidence": null,
    "source_type": null,
    "citation": "url or filename, or null if uncited"
  }
]
```

Prefix claim_ids with the company name (or a slug of it), since portco-profiler runs once per company and ids must stay unique across every invocation in a run. `confidence` and `source_type` stay `null`, filled in downstream.

## Output requirement, non-negotiable

The response must end with the "## Claims" heading and its fenced JSON
block, always, regardless of what precedes it. If you perform a
self-check, a second pass over the sources, or any additional
verification step and conclude the profile is complete with no updates
needed, that conclusion is not the end of your response. State it, then
still produce the Claims block for everything you found. A response that
ends on a verification note instead of the Claims block is incomplete and
violates this skill's contract.

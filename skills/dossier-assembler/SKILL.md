name: dossier-assembler
description: Once the firm profile, portco compilation, portco profiles, financial estimates for portcos, source ratings, and confidence scores have been produced, assemble the dossier according to dossier-structure.md.

--

# dossier-assembler: Input/Output Contract

## Input contract

| Field | Type | Required | Source |
|---|---|---|---|
| firm_profile | object | yes | firm-profiler output |
| portfolio_list | list[object] | yes | portfolio-discoverer output |
| portco_profiles | list[object] | yes | portco-profiler output, one per company in scope |
| financial_estimates | list[object] | no | private-data-approximator output |
| confidence_tags | list[object] | yes | confidence-scorer output |
| source_tags | list[object] | yes | source-typer output |
| template_version | string | no, default latest | system |

## Output contract

A markdown document covering sections 1 through 6 of the dossier template, structured per the current template, with:

- Every load-bearing sentence carrying an inline confidence/source tag or footnote reference
- A citations section at the end, one entry per claim_id, link direct to document not homepage
- Engagement-level metrics kept in a separate section from opportunity-level (portfolio company) metrics, never merged
- No numeric claim appears without a confidence band attached
- Any subsection with no supporting source is written as explicitly unavailable in place, not omitted silently (e.g. "*Not available. No supporting source found.*")
- **A portfolio company can appear in `portco_profiles` with no corresponding entry in `financial_estimates`.** This happens when private-data-approximator fails or is skipped for that company independently of portco-profiler succeeding upstream, and is a distinct case from "no supporting source was found": the company was in scope and the profile step succeeded, only the financial-estimate step did not complete. Do not omit the company or its financial subsection. Write the financial subsection explicitly as unavailable for that reason, e.g. "*Financial estimate not available. Private-data-approximation was not completed for this company.*" The rest of that company's profile (from `portco_profiles`) is written normally in its own section, unaffected by the missing estimate. Determine which companies fall into this case by comparing the company identifiers in `portco_profiles` against `financial_estimates`; do not rely on an separate flag field for this, since none is guaranteed to be present.
- Section 7, "Review of Flagged and Satisfactory Content and Claims," is left as a placeholder heading only, with body text "*Pending audit-pass.*" dossier-assembler does not write section 7's content. That belongs to audit-pass, which runs after assembly and fills it in as the final step.
- **A required "Strategy Synthesis" subsection at the end of Section 1 (Firm Profile).** This is not a restatement of what the firm says about itself, that content already exists earlier in Section 1, cited to firm-profiler's claims. This subsection's job is to compare the firm's stated strategy against the actual, observed composition of its portfolio (from portfolio-discoverer and the portco-profiler profiles), and surface one or two non-obvious observations that the firm's own materials do not state directly. If the observation is just a paraphrase of something firm-profiler already asserted, it isn't synthesis, redo it. A synthesis claim is itself a claim: it needs a confidence band (usually medium at best, since it's inference layered on top of several underlying claims, never high) and a source_type of "public inference," with its reasoning chain stated plainly enough that a reader can check it against the same underlying claims.

**The distinction between restatement and synthesis, described abstractly:** a restatement takes one existing claim (whatever the firm's own materials say about its strategy) and rephrases it, this adds no information and isn't synthesis, even if it sounds analytical. A real synthesis claim combines at least two independently-sourced facts that no single source states together, typically the firm's stated strategy on one side and the observed, actual composition of its portfolio on the other, and draws a conclusion neither source asserts on its own. If removing the citation would leave a sentence indistinguishable from something firm-profiler already produced, it's a restatement, not synthesis, redo it. See this skill's own example files for a fully worked instance of both the restatement-to-avoid and the synthesis-to-produce, built from a real dossier.

Output is always the complete document, sections 1 through 6 (including the required Strategy Synthesis subsection) plus the section 7 placeholder. No diffs.

## Output format (hybrid) — note the exception

Every other skill in this pipeline outputs markdown plus a trailing JSON claims block. dossier-assembler is one of two exceptions (the other is audit-pass): its output is pure markdown, no trailing JSON. This is most of the terminal artifact a human reads, not an intermediate stage another skill parses, so the confidence/source tags get merged into the prose as inline markers or footnotes (per the bullet list above) rather than kept as a separate machine-readable block. audit-pass, the one skill downstream of this, takes the section-1-through-6 markdown (with the section 7 placeholder) as input, not a claims array, per its own input contract, and returns the same document with section 7 filled in.

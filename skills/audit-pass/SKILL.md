name: audit-pass
description: after the dossier is completely assembled, read the dossier and produce an audit report that flags inaccuracies or question claims/reasoning steps.

--

# audit-pass: Input/Output Contract

## Input contract

| Field | Type | Required | Source |
|---|---|---|---|
| dossier_markdown | string, sections 1 through 6 complete, section 7 present as a placeholder heading with body "*Pending audit-pass.*" | yes | dossier-assembler output |

## Output contract

Output is only the markdown content of section 7 itself, "## 7. Review of Flagged and Satisfactory Content and Claims," nothing else. audit-pass does not reproduce sections 1 through 6. Reproducing a long, multi-company document verbatim is not something a model should be trusted to do reliably, in practice it silently drops content rather than actually erroring when it can't keep up, and that failure is invisible until someone reads the output and finds sections missing. The orchestrator already holds sections 1-6 in memory from dossier-assembler's output and splices this section 7 content into the placeholder itself, with code, which never drops a character. audit-pass's only job is to read the full document (for context) and write section 7 in response to it, not to hand the whole thing back.

| Field | Type |
|---|---|
| section_7_markdown | string, just this one section, starting at the "## 7." heading |
| flagged_claims | list[{claim_id, issue, severity}] (tracked internally, referenced in section 7's prose, not output as a separate artifact) |
| missing_tags | list[claim_id] (claims lacking confidence or source_type) |
| metric_conflation_flags | list[{location, description}] (engagement vs opportunity metrics mixed) |
| suggested_fixes | list[{claim_id, suggested_revision}] |

audit-pass does not silently fix the dossier body (sections 1 through 6), it never even echoes that content back, so there is no way for it to accidentally alter it. It writes section 7 describing what it found: claims that are fully supported, claims flagged for human review, and coverage gaps already noted elsewhere in the dossier that don't need re-flagging. A human resolves anything flagged; audit-pass does not resolve it itself.

## Output format (hybrid) — note the exception

Output is pure markdown, section 7's content only, starting at its own heading. No trailing JSON block, and critically, no sections 1-6 included at all, that boundary is enforced by what this skill is asked to produce, not by hoping the model remembers to pass content through unchanged. The orchestrator is responsible for taking dossier-assembler's document (sections 1-6 plus a placeholder) and replacing the placeholder with this skill's output verbatim, via string operations in code, not another model call.

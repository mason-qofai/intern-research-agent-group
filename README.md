# v4 Parallel Orchestrator: run_research.py

Generate private equity firm dossiers by orchestrating eight discrete research skills in a six-stage pipeline, with bounded concurrency and isolated error handling per company.

## Install

1. Ensure the `claude` CLI is installed (Claude Code, with Read/WebSearch/WebFetch tools enabled).
2. Clone or download this repository.
3. Populate `skills/` with the eight SKILL.md directories:
   - `firm-profiler/SKILL.md`
   - `portfolio-discoverer/SKILL.md`
   - `portco-profiler/SKILL.md`
   - `private-data-approximator/SKILL.md`
   - `confidence-scorer/SKILL.md`
   - `source-typer/SKILL.md`
   - `dossier-assembler/SKILL.md`
   - `audit-pass/SKILL.md`

4. Populate `sources/` with local research documents (.md, .txt, .html). These are used by firm-profiler, portfolio-discoverer, and portco-profiler. If empty, those skills will fail.

## Usage

Generate a complete dossier for a firm:

```bash
python3 scripts/run_research.py "Palladium Equity Partners"
```

Dossier is written to `dossiers/v4-parallel.md` and run logs to `runs/[timestamp]/run_log.txt`.

### Options

`--model <name>`: Specify a Claude model (e.g., `claude-opus-4-6`). Default is the version configured in Claude Code.

`--max-budget-usd <float>`: Cap per-skill API spend (default: 2.0). Applied independently to each skill call, not to the run as a whole.

`--timeout <int>`: Seconds to wait for each skill call (default: 600). Applies to the entire subprocess including model response time.

`--max-concurrency <int>`: Maximum simultaneous `claude -p` processes (default: 4). Limits stages 2 and 3 (portco-profiler and private-data-approximator fan-out).

`--max-companies <int>`: Limit to the first N portfolio companies discovered. Useful for smoke-testing the concurrency model before a full run.

`--out <path>`: Write dossier to a custom path (default: `dossiers/v4-parallel.md`).

`--dry-run`: Print the planned call sequence and exit without calling the API.

### Examples

Smoke test: research only the first 3 portfolio companies.

```bash
python3 scripts/run_research.py "Palladium Equity Partners" --max-companies 3
```

Dry run to see what would happen:

```bash
python3 scripts/run_research.py "Palladium Equity Partners" --dry-run
```

Full run with tighter concurrency (one company at a time):

```bash
python3 scripts/run_research.py "Palladium Equity Partners" --max-concurrency 1
```

## Pipeline Architecture

The orchestrator executes six stages. Stages 1, 4 are concurrent within themselves (asyncio.gather); stages 2, 3 are fan-out with bounded concurrency (semaphore-controlled). Stages 5, 6 are sequential.

A failure in stage 1 is fatal and stops the run immediately (no firm profile means nothing downstream has an anchor). Failures in stages 2, 3 are isolated per company: one company's exhausted retries do not affect the other companies' fan-out. A company can survive stage 2 (portco-profiler) but fail in stage 3 (private-data-approximator), keeping its profile without financial estimates; this is intentional.

### Stage 1: Firm Profile + Portfolio Discovery (concurrent)

**Skills**: firm-profiler, portfolio-discoverer

**Inputs**: Firm name, research depth ("standard"), portfolio scope ("active_only")

**Output**: Firm profile markdown and claims list; portfolio markdown and claims list

**Error handling**: Fatal (raises to main(), stops the run)

### Stage 2: Portfolio Company Profiles (fan-out, bounded concurrency)

**Skill**: portco-profiler

**Input**: Each portfolio company name (extracted from portfolio-discoverer's claims)

**Output per company**: Profile markdown and claims list

**Error handling**: Per-company retry loop (up to 2 attempts per company). Exhausted retries log a message and skip that company; other companies continue unaffected.

**Retry logic**: If a company's portco-profiler call fails, the orchestrator retries once with the same input and semaphore constraints. A second failure logs it as skipped and moves on.

### Stage 3: Financial Estimates (fan-out, bounded concurrency)

**Skill**: private-data-approximator

**Input**: Each company's profile markdown (from stage 2 survivors only)

**Output per company**: Financial estimate markdown and claims list

**Error handling**: Per-company retry loop (up to 2 attempts per company), decoupled from stage 2. A company that survives stage 2 but fails stage 3 keeps its profile in the dossier (with no financial estimates); dossier-assembler detects the mismatch by comparing lists.

**Design note**: Stages 2 and 3 are separate, not folded into a single try/except per company, so a stage-3 failure does not throw away a stage-2 success.

### Stage 4: Confidence Scoring + Source Typing (concurrent)

**Skills**: confidence-scorer, source-typer

**Input**: All ratable claims (merged from stages 1-3); pre-flagged claims (already flagged or withheld) are excluded

**Output**: Confidence scores and source types merged back onto each claim

**Error handling**: Any failure here is fatal (raises to main()).

### Stage 5: Dossier Assembly (sequential)

**Skill**: dossier-assembler

**Input**: Firm profile, portfolio markdown, all portco profiles (even if some companies have no financial estimates), all financial estimates (subset if some stage-3 calls failed), all claims (both rated and pre-flagged), skipped companies list

**Output**: Markdown dossier with sections 1-6 plus a section-7 placeholder

**Error handling**: Fatal.

### Stage 6: Audit Review (sequential)

**Skill**: audit-pass

**Input**: The dossier from stage 5 (sections 1-6 plus placeholder)

**Output**: Section 7 content only (markdown, no JSON)

**Splice**: The section-7 content is spliced into the dossier using a regex replacement in Python code (splice_section_7), not by asking the model to reproduce sections 1-6 verbatim. This prevents silent loss of content, which occurred in v3 during the Palladium run.

**Error handling**: Fatal.

## Key Design Decisions

### Hybrid Output Format

Skills output markdown prose plus a trailing JSON block (either a `## Claims` heading followed by ```json``` or just a fenced ```json``` block at the end). This structure allows prose interpretation by humans while enabling machine-readable claim extraction for downstream skills.

### Per-Company Error Isolation

Stages 2 and 3 use per-company retry loops inside asyncio.gather. If company A exhausts retries, company B's work continues in parallel. This differs from v3, which tried both portco-profiler and private-data-approximator in a single try/except, so a private-data-approximator failure threw away a successful portco-profiler result.

### Decoupled Stage 3

Private-data-approximator is its own stage with its own retry loop, not folded into stage 2. A company can end up with a profile but no financial estimates. Dossier-assembler's contract explicitly handles this by comparing the two lists.

### Code-Based Section-7 Splice

The section-7 content (audit-pass output) is spliced into the dossier using Python regex, not by asking the model to combine it with sections 1-6. The v3 approach (model reproduction) silently dropped sections 1-6 during the Palladium run; text splicing in code cannot.

### Source Tiers

Firm-profiler, portfolio-discoverer, and portco-profiler use local files only (Read tool). Private-data-approximator uses live web search (SEC/EDGAR preferred, tier-2 sources with full readability only, no paywalled paraphrasing). Confidence-scorer and source-typer work from claims lists, not from source files.

### Pre-Flagged Claims

Portfolio-discoverer and private-data-approximator can flag claims that warrant review (untraceable hops, withheld estimates) or mark them as withheld. These bypass confidence-scorer and source-typer; dossier-assembler and audit-pass receive them separately.

## Run Log

Each run writes a detailed log to `runs/[timestamp]/run_log.txt`. Entries are ISO 8601 timestamps plus messages:

```
[2026-07-14T15:30:22] pipeline start for 'Palladium Equity Partners'
[2026-07-14T15:30:25] firm-profiler complete, 47 claims
[2026-07-14T15:30:30] portfolio-discoverer complete, 12 claims
[2026-07-14T15:30:30] extracted 8 company names: [...]
[2026-07-14T15:31:15] portco-profiler complete for Company A
[2026-07-14T15:31:22] FAILED portco-profiler Company B on attempt 1: SkillCallError: ...
[2026-07-14T15:31:35] RETRY portco-profiler Company B (attempt 2/2)
[2026-07-14T15:31:45] SKIPPED Company B (portco-profiler) after 2 attempts: ...
...
```

Each skill call is logged with its status (complete, failed, skipped). Retries and timeouts are explicit in the log.

Raw output from each skill is saved separately in `runs/[timestamp]/` as markdown files (e.g., `portco-profiler-Company-A.md`, `confidence-scorer.md`).

## Troubleshooting

### "claude CLI not found on PATH"

Install Claude Code (https://github.com/anthropics/claude-code). The `claude` CLI must be available and configured to call the Anthropic API.

### "missing SKILL.md for [skill-name]"

The `skills/` directory exists but the skill folders are empty. Place the finished SKILL.md files in the appropriate subdirectories (e.g., `skills/firm-profiler/SKILL.md`).

### "no source files found in sources/, required for firm-profiler"

Firm-profiler, portfolio-discoverer, and portco-profiler require local research documents in `sources/`. Add .md, .txt, .html, or .htm files to that directory.

### "[skill] timed out after 600s"

The skill call exceeded the timeout window (default 600 seconds). This can happen with large firm portfolios or slow API responses. Increase `--timeout` or check API quotas / rate limits.

### "[company] have a portco profile but no financial estimate"

Stage 2 succeeded for this company, but stage 3 (private-data-approximator) failed or was skipped. The dossier will include the company's profile but no financial data. Check the run log and raw output files in `runs/[timestamp]/` for details.

### "could not find the section 7 placeholder heading in dossier-assembler's output"

The dossier-assembler's section-7 placeholder heading format does not match the regex in splice_section_7. Verify the exact wording of the section-7 placeholder in dossier-assembler's SKILL.md and update the regex in run_research.py if needed (case-insensitive pattern: `r"##\s*7\.\s*Review of Flagged and Satisfactory Content and Claims.*"`).

## Configuration

### API Budget

Each skill call is capped at `--max-budget-usd` (default 2.0). This is a per-skill limit, not a run total. A run with 8 portfolio companies and default settings might spend up to 2.0 * (2 firm skills + 2 per-company skills + 2 batch skills + 2 final skills) = well over the single-skill budget.

### Concurrency Limits

Stages 2 and 3 use a semaphore to limit simultaneous `claude -p` processes to `--max-concurrency` (default 4). This prevents overwhelming the API or the host machine. Stages 1, 4 use asyncio.gather (unlimited parallelism within those stages, but only 1-2 concurrent tasks).

### Retry Strategy

Stages 2 and 3 retry failed companies up to 2 times (configurable via MAX_ATTEMPTS_PER_COMPANY in the code). Stages 1, 4, 5, 6 do not retry; a failure is fatal.

## Development

To modify the orchestrator:

1. Edit `scripts/run_research.py` directly.
2. Test with `--dry-run` first to check the call sequence without calling the API.
3. Run on a small test set (e.g., `--max-companies 1 --max-concurrency 1`) to verify error handling.
4. Check the run log in `runs/[timestamp]/run_log.txt` and raw skill outputs for debugging.

To add a new skill or stage, update the SKILL_CATEGORIES constants at the top of the script and add the appropriate call sequence in run_pipeline. Each new skill must output hybrid markdown + JSON claims.

## Input/Output Contracts

### Skill Inputs and Outputs

Each skill's contract (input format, output format, trigger condition) is defined in its SKILL.md. The orchestrator passes structured JSON to each skill and expects hybrid markdown + JSON back. Parsed claims from each skill are merged and passed downstream.

### Dossier-Assembler Input

Dossier-assembler receives:
- firm_profile_markdown: Prose from firm-profiler
- portfolio_markdown: Prose from portfolio-discoverer
- portco_profiles: List of {company, markdown} for each profiled company
- financial_estimates: List of {company, markdown} for each company with a financial estimate (subset if some stage-3 calls failed)
- rated_claims: All claims that passed confidence-scorer and source-typer, with confidence and source-type fields added
- pre_flagged_claims: Claims that were already flagged or withheld (skipped scoring)
- skipped_companies: List of company names that failed stage 2 entirely

### Output

A single markdown dossier with seven sections:
1. Firm Profile
2. Portfolio Overview
3. Individual Portfolio Company Profiles
4. Financial Estimates and Analysis
5. Risk and Opportunity Summary
6. Investment Thesis
7. Review of Flagged and Satisfactory Content and Claims

Section 7 is generated by audit-pass and spliced in by code.

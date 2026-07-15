#!/usr/bin/env python3
"""
v4 dossier agent, parallel orchestrator with subagent fan-out.

Module 8 refactor of v3 (sequential run_research.py). No research judgment
lives here; each skill's SKILL.md still carries the contract, trigger
condition, and worked examples. This script's job is call ordering,
concurrency, error isolation per company, and writing a run log to
runs/[timestamp]/.

Stages:
  1. firm-profiler + portfolio-discoverer, concurrent (asyncio.gather).
     A failure here is fatal to the whole run, so it's allowed to raise
     straight up to main() rather than being caught per-task.
  2. portco-profiler, one subagent per portfolio company, concurrent,
     bounded by --max-concurrency. Each company gets its own retry loop,
     isolated from the others: one company's exhausted retries can't take
     down the fan-out for the rest.
  3. private-data-approximator, one subagent per company that survived
     stage 2, concurrent, bounded by --max-concurrency. Deliberately its
     own stage with its own retry loop, not folded into stage 2's loop.
     v3 tried both skills inside a single try/except per company, which
     meant a private-data-approximator failure threw away an already-
     successful portco-profiler result for that company. Decoupled here:
     a company can now end up with a profile and no financial estimate.
     dossier-assembler's contract handles this by comparing portco_profiles
     against financial_estimates directly, see stage 5 below.
  4. confidence-scorer + source-typer, concurrent (asyncio.gather). These
     two don't depend on each other, only on the claims list from stages
     1 through 3.
  5. dossier-assembler, sequential.
  6. audit-pass, sequential. Returns only section 7; the splice into the
     rest of the dossier happens in code (splice_section_7), not by
     asking the model to reproduce sections 1-6 verbatim, which failed
     silently in practice during the v3 Palladium run.

Usage:
    python3 scripts/run_research.py "Palladium Equity Partners"
    python3 scripts/run_research.py "Palladium Equity Partners" --dry-run
    python3 scripts/run_research.py "Palladium Equity Partners" --max-concurrency 3
"""

import argparse
import asyncio
import datetime
import hashlib
import json
import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"
SOURCES_DIR = REPO_ROOT / "sources"
RUNS_DIR = REPO_ROOT / "runs"
DOSSIERS_DIR = REPO_ROOT / "dossiers"
DEFAULT_OUT = DOSSIERS_DIR / "v4-parallel.md"

# .pdf dropped: sources/ no longer contains any per the current source set.
SOURCE_SUFFIXES = (".md", ".txt", ".html", ".htm")

SOURCE_BASED_SKILLS = {"firm-profiler", "portfolio-discoverer", "portco-profiler"}
WEB_SEARCH_SKILLS = {"private-data-approximator"}
JSON_ONLY_SKILLS = {"confidence-scorer", "source-typer"}
PURE_MARKDOWN_SKILLS = {"dossier-assembler", "audit-pass"}

MAX_ATTEMPTS_PER_COMPANY = 2
MAX_NAME_LENGTH = 80


class SkillCallError(Exception):
    """Raised for any skill-call failure. Replaces v3's sys.exit calls
    inside the call path: sys.exit raises SystemExit, which is not an
    Exception subclass and is not handled predictably inside
    asyncio.gather. All process exits now happen once, in main(), after
    the event loop has finished."""


class RunBudgetExceededError(SkillCallError):
    """Raised when cumulative run spend crosses --max-run-budget-usd.
    Subclasses SkillCallError so main()'s existing except SkillCallError
    handler exits cleanly with no code change there. Must NOT be caught by
    profile_company/estimate_financials's per-company retry loops -- it's
    not a transient per-call failure, so retrying after the run budget is
    already blown would only spend more."""


class BudgetTracker:
    """Tracks cumulative spend across every call_skill invocation in a run
    and raises once a run-level cap is crossed. This is cheap insurance,
    not a hard real-time meter: the check happens once per call, right
    before that call's subprocess is spawned, so calls already in flight
    when the cap is crossed are left to finish rather than being killed --
    actual spend can overshoot the cap by up to max_concurrency in-flight
    calls' worth."""

    def __init__(self, cap_usd):
        self.cap_usd = cap_usd
        self.spent_usd = 0.0

    def check(self):
        if self.cap_usd is not None and self.spent_usd >= self.cap_usd:
            raise RunBudgetExceededError(
                f"run budget exceeded: spent ${self.spent_usd:.2f} of "
                f"${self.cap_usd:.2f} cap, stopping before starting a new skill call"
            )

    def add(self, cost_usd):
        self.spent_usd += cost_usd


def read_skill_md(skill_name):
    path = SKILLS_DIR / skill_name / "SKILL.md"
    if not path.exists():
        raise SkillCallError(
            f"missing SKILL.md for {skill_name}: {path}\n"
            f"(skills/ folders exist but are empty until you drop the "
            f"finished SKILL.md files in, per your own build process)"
        )
    text = path.read_text().strip()
    if not text:
        raise SkillCallError(f"SKILL.md is empty: {path}")
    return text


def list_source_files():
    if not SOURCES_DIR.exists():
        return []
    return sorted(
        p for p in SOURCES_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in SOURCE_SUFFIXES
    )


def _normalize_words(text):
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _acronym(text):
    return "".join(w[0] for w in re.findall(r"[a-z0-9]+", text.lower()))


def list_source_files_for_company(company_name):
    """Filters list_source_files() down to files whose name plausibly
    relates to company_name, instead of handing every file in sources/ to
    every portco-profiler call. portco-profiler runs once per portfolio
    company, so without this filter a firm with N portfolio companies
    re-reads the entire sources/ directory N times (plus once per retry),
    almost all of it irrelevant to any single company. Matches on shared
    words, or on the company's acronym appearing as a whole word in the
    filename (e.g. "Superior Environmental Solutions" -> "ses", matching
    "SES_Chemworx_addon.md") since add-on/acquisition docs are often named
    by acronym rather than full company name. Returns [] if nothing
    matches; callers should fall back to the full listing rather than
    starving the skill of source material over a naming mismatch."""
    company_words = _normalize_words(company_name)
    company_acronym = _acronym(company_name)
    matches = []
    for f in list_source_files():
        stem_words = _normalize_words(f.stem)
        if company_words & stem_words:
            matches.append(f)
        elif len(company_acronym) >= 3 and company_acronym in stem_words:
            matches.append(f)
    return matches


def extract_trailing_json_block(text):
    """
    Pulls the JSON content out of a '## Claims' fenced block at the end of
    a markdown + hybrid-format output. Returns (markdown_body, claims_list).

    Always takes the LAST ```json fenced block in the text as the claims
    payload (matches the contract that every skill in SOURCE_BASED_SKILLS |
    WEB_SEARCH_SKILLS must end in one). A '## Claims' heading, if present
    before that fence, marks where markdown_body ends. This also tolerates
    a heading that lands as the first line *inside* the fence instead of
    before it (a formatting slip some models produce, e.g. opening the
    fence before "## Claims" rather than after) by stripping it before
    parsing. Raises if no fenced json block is found at all.
    """
    fence_matches = list(re.finditer(r"```json\s*(.*?)```", text, re.DOTALL))
    if not fence_matches:
        raise ValueError("no fenced json block found in output at all")
    last = fence_matches[-1]
    json_text = re.sub(r"^\s*##\s*Claims\s*\n", "", last.group(1))

    claims_heading_match = re.search(r"##\s*Claims\s*\n", text[:last.start()])
    if claims_heading_match:
        markdown_body = text[:claims_heading_match.start()].strip()
    else:
        markdown_body = text[:last.start()].strip()

    try:
        claims = json.loads(json_text)
    except json.JSONDecodeError as e:
        raise ValueError(f"claims block did not parse as JSON: {e}\nraw:\n{json_text}")

    return markdown_body, claims


def parse_json_only(text):
    """For confidence-scorer / source-typer: whole output is a JSON array."""
    cleaned = re.sub(r"^```json\s*|\s*```$", "", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"expected pure JSON array, got unparseable text: {e}\nraw:\n{text}")


def build_prompt(skill_name, skill_md, input_payload, extra_instructions=""):
    return f"""You are the {skill_name} skill. Follow the contract, trigger condition, and worked examples in your SKILL.md exactly.

=== SKILL.md: {skill_name} ===

{skill_md}

=== INPUT (matches this skill's input contract) ===

{json.dumps(input_payload, indent=2)}

{extra_instructions}

=== OUTPUT ===

Return your output in exactly the format your SKILL.md's "Output format (hybrid)" section specifies for this skill. No preamble, no meta-commentary, no requests for permission. Your entire response is the deliverable.
"""


def save_raw_output(run_dir, label, raw_text):
    run_dir.mkdir(parents=True, exist_ok=True)
    safe_label = re.sub(r"[^A-Za-z0-9._-]", "-", label)
    # Cap well under filesystem limits (macOS/most Linux: 255 bytes). A
    # malformed label should never be able to crash the run over a
    # filename length.
    if len(safe_label) > 100:
        digest = hashlib.sha256(safe_label.encode()).hexdigest()[:8]
        safe_label = safe_label[:90] + "-" + digest
    (run_dir / f"{safe_label}.md").write_text(raw_text)


def append_log(run_dir, message):
    run_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().isoformat(timespec="seconds")
    with open(run_dir / "run_log.txt", "a") as f:
        f.write(f"[{timestamp}] {message}\n")


async def call_skill(skill_name, input_payload, model, max_budget_usd, timeout, run_dir,
                      semaphore, budget_tracker, invocation_label=None, reduced=False):
    """Async version of the v3 call_skill. Acquires `semaphore` for the
    full duration of the subprocess call, so concurrent fan-out (stage 2
    and stage 3) is bounded rather than launching every company at once
    against a live API budget and rate limit.

    `reduced` marks a retry attempt: portco-profiler gets only its single
    best-matching source file instead of every match, and web-search
    skills get an extra instruction to settle for the first usable finding
    instead of comparing options. A failed first attempt shouldn't cost as
    much as the first one did."""
    skill_md = read_skill_md(skill_name)

    extra = ""
    if skill_name in SOURCE_BASED_SKILLS:
        if skill_name == "portco-profiler":
            company_name = input_payload.get("company_name", "")
            source_files = list_source_files_for_company(company_name)
            if not source_files:
                append_log(
                    run_dir,
                    f"no sources/ filename matched company_name={company_name!r}, "
                    f"falling back to the full sources/ listing for this portco-profiler call"
                )
                source_files = list_source_files()
            if reduced and source_files:
                source_files = source_files[:1]
        else:
            source_files = list_source_files()
        if not source_files:
            raise SkillCallError(f"no source files found in {SOURCES_DIR}, required for {skill_name}")
        # Content is embedded directly rather than handed to the model as a
        # filename list to Read one-by-one. Reading N files via sequential
        # Read tool calls inside one turn-based conversation resends the
        # entire growing conversation history (every file already read, plus
        # SKILL.md, plus tool overhead) as input again on every subsequent
        # turn -- a cost that scales worse than linearly in file count and
        # was measured burning ~300k tokens on firm-profiler +
        # portfolio-discoverer alone, before any web search ever ran.
        # Embedding puts everything in the prompt in one shot, no tool
        # round-trips required.
        blocks = [f"### sources/{f.name}\n\n{f.read_text()}" for f in source_files]
        extra = (
            "All sources for this call are embedded in full below. Nothing is "
            "fetched from the web for this skill, and there is no need to Read "
            "any files -- the complete content of every relevant source is "
            "already here.\n\n=== SOURCE FILES (full content) ===\n\n"
            + "\n\n---\n\n".join(blocks)
        )
        allowed_tools = "Read"
    elif skill_name in WEB_SEARCH_SKILLS:
        extra = (
            "This skill uses live web search, per the source-tier rules in its "
            "own SKILL.md (SEC/EDGAR preferred, tier 2 sources only when fully "
            "readable, never paraphrase from a paywalled snippet)."
        )
        if reduced:
            extra += (
                " This is a retry attempt -- settle for the very first usable "
                "comp that satisfies the tier rules above; do not compare "
                "multiple candidates before choosing."
            )
        allowed_tools = "Read,WebSearch,WebFetch"
    else:
        allowed_tools = "Read"

    prompt = build_prompt(skill_name, skill_md, input_payload, extra)

    cmd = [
        "claude", "-p", prompt,
        "--allowedTools", allowed_tools,
        "--output-format", "json",
        "--max-budget-usd", str(max_budget_usd),
    ]
    if model:
        cmd += ["--model", model]

    label = invocation_label or skill_name

    async with semaphore:
        budget_tracker.check()
        print(f"[run_research] calling {label} ...", file=sys.stderr)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=REPO_ROOT,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            # asyncio.wait_for does not kill the underlying process on
            # timeout the way subprocess.run(timeout=...) does. Left
            # alone, this orphans a running `claude` process per timeout,
            # which is worse under concurrency than it was in v3.
            proc.kill()
            await proc.wait()
            raise SkillCallError(f"{label} timed out after {timeout}s")
        except FileNotFoundError:
            raise SkillCallError("claude CLI not found on PATH, install Claude Code first")

        if proc.returncode != 0:
            raise SkillCallError(
                f"{label} exited with status {proc.returncode}\n{stderr.decode(errors='replace')}"
            )

        stdout_text = stdout.decode(errors="replace").strip()
        if not stdout_text:
            raise SkillCallError(f"{label} produced no output\n{stderr.decode(errors='replace')}")

        try:
            envelope = json.loads(stdout_text)
        except json.JSONDecodeError as e:
            raise SkillCallError(f"{label} did not return valid --output-format json: {e}\nraw:\n{stdout_text}")

        cost_usd = envelope.get("total_cost_usd") or 0.0
        budget_tracker.add(cost_usd)

        if envelope.get("is_error"):
            raise SkillCallError(
                f"{label} returned an error (subtype={envelope.get('subtype')}): "
                f"{envelope.get('errors')}"
            )

        raw = (envelope.get("result") or "").strip()
        if not raw:
            raise SkillCallError(f"{label} produced no result text in its json output")

        save_raw_output(run_dir, label, raw)
        return raw


def collect_claims(claim_sources):
    """
    claim_sources: list of raw claims lists already extracted from upstream
    skill outputs (firm-profiler, portfolio-discoverer, each portco-profiler
    call, each private-data-approximator call).

    Splits into:
      - ratable: claims with no flagged/withheld True, these go into the
        confidence-scorer and source-typer batch.
      - pre_flagged: claims already flagged (e.g. portfolio-discoverer's
        untraceable-hop candidates) or withheld (private-data-approximator's
        withheld estimates). These skip confidence-scorer and source-typer
        entirely, per contract, and go straight into the flagged-for-review
        pile dossier-assembler and audit-pass need to know about.
    """
    ratable = []
    pre_flagged = []
    for claims_list in claim_sources:
        for claim in claims_list:
            if claim.get("flagged") or claim.get("withheld"):
                pre_flagged.append(claim)
            else:
                ratable.append(claim)
    return ratable, pre_flagged


def merge_rated_claims(ratable_claims, confidence_results, source_type_results):
    """Merges confidence-scorer and source-typer's batch outputs back onto
    the original claim objects by claim_id, so nothing downstream has to
    re-join three separate lists itself."""
    conf_by_id = {c["claim_id"]: c for c in confidence_results}
    source_by_id = {c["claim_id"]: c for c in source_type_results}

    merged = []
    for claim in ratable_claims:
        cid = claim["claim_id"]
        conf = conf_by_id.get(cid, {})
        source = source_by_id.get(cid, {})
        merged_claim = {
            **claim,
            "confidence": conf.get("confidence"),
            "confidence_rationale": conf.get("rationale"),
            "confidence_flagged": conf.get("flagged", False),
            "source_type": source.get("source_type"),
            "source_evidence": source.get("evidence_url_or_note"),
            "source_flagged": source.get("flagged", False),
        }
        merged.append(merged_claim)
    return merged


def extract_company_name(claim):
    name = claim.get("company_name")
    if not name:
        raise ValueError(f"claim has no company_name field, contract violation: {claim!r}")
    name = name.strip()
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(
            f"company_name is {len(name)} chars, too long to be a real "
            f"company name, likely a contract violation upstream: {name!r}"
        )
    return name


async def profile_company(company_name, firm_name, model, max_budget_usd, timeout, run_dir, semaphore, budget_tracker):
    """Stage 2 subagent: portco-profiler for one company, up to
    MAX_ATTEMPTS_PER_COMPANY tries. Returns (company_name, markdown, claims)
    on success, or (company_name, None, None) on exhausted retries. Never
    raises (except RunBudgetExceededError, which is not a per-company
    failure and must propagate rather than being retried or swallowed), so
    one company's failure can't take down asyncio.gather for the rest of
    the fan-out. A retry (attempt > 1) is cheaper than the first attempt --
    see call_skill's `reduced` param -- and runs at half the budget, since
    a failed first try shouldn't cost as much as it did."""
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS_PER_COMPANY + 1):
        try:
            if attempt > 1:
                append_log(run_dir, f"RETRY portco-profiler {company_name} (attempt {attempt}/{MAX_ATTEMPTS_PER_COMPANY})")
            raw = await call_skill(
                "portco-profiler", {"company_name": company_name, "parent_firm": firm_name},
                model, max_budget_usd if attempt == 1 else max_budget_usd / 2, timeout, run_dir, semaphore,
                budget_tracker,
                invocation_label=f"portco-profiler-{company_name}"
                + (f"-attempt{attempt}" if attempt > 1 else ""),
                reduced=(attempt > 1),
            )
            md, claims = extract_trailing_json_block(raw)
            append_log(run_dir, f"portco-profiler complete for {company_name}")
            return company_name, md, claims
        except RunBudgetExceededError:
            raise
        except (SkillCallError, ValueError) as e:
            last_error = e
            append_log(run_dir, f"FAILED portco-profiler {company_name} on attempt {attempt}: {type(e).__name__}: {e}")

    append_log(run_dir, f"SKIPPED {company_name} (portco-profiler) after {MAX_ATTEMPTS_PER_COMPANY} attempts: {last_error}")
    return company_name, None, None


async def estimate_financials(company_name, profile_markdown, model, max_budget_usd, timeout, run_dir, semaphore, budget_tracker):
    """Stage 3 subagent: private-data-approximator for one company, up to
    MAX_ATTEMPTS_PER_COMPANY tries. Deliberately decoupled from stage 2's
    retries: a company whose portco-profiler call succeeded but whose
    private-data-approximator call fails keeps its profile rather than
    losing both, which is what v3 did by trying both skills inside a
    single try/except per company. RunBudgetExceededError propagates
    rather than being retried, same reasoning as profile_company. A retry
    runs at half budget with an instruction to settle for the first usable
    comp instead of comparing options."""
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS_PER_COMPANY + 1):
        try:
            if attempt > 1:
                append_log(run_dir, f"RETRY private-data-approximator {company_name} (attempt {attempt}/{MAX_ATTEMPTS_PER_COMPANY})")
            raw = await call_skill(
                "private-data-approximator",
                {"portco_profile": {"company_name": company_name, "profile_markdown": profile_markdown}},
                model, max_budget_usd if attempt == 1 else max_budget_usd / 2, timeout, run_dir, semaphore,
                budget_tracker,
                invocation_label=f"private-data-approximator-{company_name}"
                + (f"-attempt{attempt}" if attempt > 1 else ""),
                reduced=(attempt > 1),
            )
            md, claims = extract_trailing_json_block(raw)
            append_log(run_dir, f"private-data-approximator complete for {company_name}")
            return company_name, md, claims
        except RunBudgetExceededError:
            raise
        except (SkillCallError, ValueError) as e:
            last_error = e
            append_log(run_dir, f"FAILED private-data-approximator {company_name} on attempt {attempt}: {type(e).__name__}: {e}")

    append_log(run_dir, f"SKIPPED {company_name} (private-data-approximator) after {MAX_ATTEMPTS_PER_COMPANY} attempts: {last_error}")
    return company_name, None, None


def splice_section_7(dossier_with_placeholder, section_7_content):
    """
    Replaces the "## 7. Review of Flagged and Satisfactory Content and
    Claims" placeholder section (and its "*Pending audit-pass.*" body)
    with audit-pass's actual section 7 content, entirely in code. This is
    the fix for a real failure: asking a model to reproduce a long
    multi-company dossier verbatim in order to append one new section
    resulted in it silently dropping sections 1-6 instead. Splicing text
    in Python cannot drop content the way a model reproduction can.
    """
    section_7_content = section_7_content.strip()
    pattern = re.compile(
        r"##\s*7\.\s*Review of Flagged and Satisfactory Content and Claims.*",
        re.DOTALL,
    )
    if pattern.search(dossier_with_placeholder):
        return pattern.sub(section_7_content, dossier_with_placeholder).strip() + "\n"
    else:
        raise ValueError(
            "could not find the section 7 placeholder heading in dossier-assembler's "
            "output to splice audit-pass's content into. Check dossier-assembler's "
            "exact placeholder wording against this regex."
        )


async def run_pipeline(firm_name, model, max_budget_usd, timeout, run_dir, max_concurrency,
                        max_companies=None, max_run_budget_usd=None):
    append_log(run_dir, f"pipeline start for {firm_name!r}")
    semaphore = asyncio.Semaphore(max_concurrency)
    budget_tracker = BudgetTracker(max_run_budget_usd)

    # --- Stage 1: firm-profiler + portfolio-discoverer, concurrent ---
    firm_task = call_skill(
        "firm-profiler", {"firm_name": firm_name, "research_depth": "standard"},
        model, max_budget_usd, timeout, run_dir, semaphore, budget_tracker,
    )
    portfolio_task = call_skill(
        "portfolio-discoverer", {"firm_name": firm_name, "scope": "active_only"},
        model, max_budget_usd, timeout, run_dir, semaphore, budget_tracker,
    )
    # A stage 1 failure is fatal to the whole run (no firm profile means
    # nothing downstream has an anchor), so this gather is allowed to
    # raise straight up to main() rather than being caught here.
    firm_raw, portfolio_raw = await asyncio.gather(firm_task, portfolio_task)

    firm_md, firm_claims = extract_trailing_json_block(firm_raw)
    append_log(run_dir, f"firm-profiler complete, {len(firm_claims)} claims")

    portfolio_md, portfolio_claims = extract_trailing_json_block(portfolio_raw)
    append_log(run_dir, f"portfolio-discoverer complete, {len(portfolio_claims)} claims")

    # Company names come from the required "company_name" field on each
    # claim, per the portfolio-discoverer contract, not parsed out of
    # prose in the "text" field.
    company_names = []
    for c in portfolio_claims:
        if c.get("field") != "company_name" or c.get("flagged"):
            continue
        try:
            company_names.append(extract_company_name(c))
        except ValueError as e:
            append_log(run_dir, f"SKIPPED a portfolio-discoverer claim during name extraction: {e}")

    append_log(run_dir, f"extracted {len(company_names)} company names: {company_names}")

    if max_companies is not None and len(company_names) > max_companies:
        append_log(
            run_dir,
            f"limiting to first {max_companies} of {len(company_names)} companies "
            f"(--max-companies): dropping {company_names[max_companies:]}"
        )
        company_names = company_names[:max_companies]

    # --- Stage 2: portco-profiler, one subagent per company, concurrent,
    # bounded by max_concurrency via the semaphore inside call_skill ---
    profile_results = await asyncio.gather(*[
        profile_company(name, firm_name, model, max_budget_usd, timeout, run_dir, semaphore, budget_tracker)
        for name in company_names
    ])

    portco_mds = [(name, md) for name, md, claims in profile_results if md is not None]
    portco_claims_lists = [claims for _, md, claims in profile_results if md is not None]
    profiled_ok = {name: md for name, md, claims in profile_results if md is not None}
    skipped_companies = [name for name, md, _ in profile_results if md is None]

    if skipped_companies:
        append_log(run_dir, f"SKIPPED {len(skipped_companies)} companies at portco-profiler stage: {skipped_companies}")

    # --- Stage 3: private-data-approximator, one subagent per profiled
    # company, concurrent. Only companies that survived stage 2 run here;
    # a stage-2 skip has no profile to hand this skill in the first place. ---
    financial_results = await asyncio.gather(*[
        estimate_financials(name, md, model, max_budget_usd, timeout, run_dir, semaphore, budget_tracker)
        for name, md in profiled_ok.items()
    ])

    financial_mds = [(name, md) for name, md, claims in financial_results if md is not None]
    financial_claims_lists = [claims for _, md, claims in financial_results if md is not None]
    financial_skipped = [name for name, md, _ in financial_results if md is None]

    if financial_skipped:
        append_log(
            run_dir,
            f"{len(financial_skipped)} companies have a portco profile but no financial "
            f"estimate (private-data-approximator failed independently): {financial_skipped}"
        )

    # --- Stage 4: confidence-scorer + source-typer, concurrent ---
    ratable_claims, pre_flagged_claims = collect_claims(
        [firm_claims, portfolio_claims] + portco_claims_lists + financial_claims_lists
    )
    append_log(run_dir, f"{len(ratable_claims)} claims to rate, {len(pre_flagged_claims)} pre-flagged")

    confidence_input = [
        {"claim_id": c["claim_id"], "claim_text": c["text"], "evidence": c.get("citation")}
        for c in ratable_claims
    ]

    confidence_task = call_skill(
        "confidence-scorer", confidence_input, model, max_budget_usd, timeout, run_dir, semaphore, budget_tracker,
    )
    source_task = call_skill(
        "source-typer", confidence_input, model, max_budget_usd, timeout, run_dir, semaphore, budget_tracker,
    )
    confidence_raw, source_raw = await asyncio.gather(confidence_task, source_task)

    confidence_results = parse_json_only(confidence_raw)
    source_results = parse_json_only(source_raw)

    merged_claims = merge_rated_claims(ratable_claims, confidence_results, source_results)
    append_log(run_dir, "confidence-scorer and source-typer complete")

    # --- Stage 5: dossier-assembler, sequential ---
    # financial_estimates is intentionally a partial list here: any company
    # in portco_profiles with no matching entry has had private-data-
    # approximator fail or get skipped independently. dossier-assembler's
    # contract now handles that case by comparing the two lists directly,
    # so no separate flag field is passed for it.
    assembler_input = {
        "firm_profile_markdown": firm_md,
        "portfolio_markdown": portfolio_md,
        "portco_profiles": [{"company": name, "markdown": md} for name, md in portco_mds],
        "financial_estimates": [{"company": name, "markdown": md} for name, md in financial_mds],
        "rated_claims": merged_claims,
        "pre_flagged_claims": pre_flagged_claims,
        "skipped_companies": skipped_companies,
    }
    dossier_raw = await call_skill(
        "dossier-assembler", assembler_input, model, max_budget_usd, timeout, run_dir, semaphore, budget_tracker,
    )
    append_log(run_dir, "dossier-assembler complete, sections 1-6 plus section 7 placeholder")

    # --- Stage 6: audit-pass, sequential ---
    section_7_raw = await call_skill(
        "audit-pass", {"dossier_markdown": dossier_raw}, model, max_budget_usd, timeout, run_dir, semaphore, budget_tracker,
    )
    full_dossier_raw = splice_section_7(dossier_raw, section_7_raw)
    append_log(run_dir, "audit-pass complete, section 7 spliced into dossier by code, not by model reproduction")

    return full_dossier_raw, skipped_companies, financial_skipped


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[1])
    parser.add_argument("firm_name")
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-budget-usd", type=float, default=2.0,
                         help="cap PER SKILL CALL (default: 2.0)")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--max-concurrency", type=int, default=15,
                         help="max simultaneous claude -p subprocesses (default: 15)")
    parser.add_argument("--max-companies", type=int, default=None,
                         help="limit the run to the first N portfolio companies found "
                              "(default: no limit, use all companies). Good for a smoke "
                              "test of the parallel fan-out before a full run.")
    parser.add_argument("--max-run-budget-usd", type=float, default=5.0,
                         help="cap on CUMULATIVE spend across every skill call in the "
                              "whole run (default: 15.0). Checked once per call, right "
                              "before that call's subprocess is spawned -- calls already "
                              "in flight when the cap is crossed are left to finish "
                              "rather than killed, so actual spend can overshoot by up "
                              "to --max-concurrency in-flight calls' worth. Pass 0 or "
                              "a negative number to disable.")
    parser.add_argument("--out", default=None)
    parser.add_argument("--dry-run", action="store_true",
                         help="print the planned call sequence and exit, do not call claude")
    args = parser.parse_args()

    run_id = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    run_dir = RUNS_DIR / run_id

    if args.dry_run:
        print(f"Would research: {args.firm_name}")
        print(f"Run log would be written to: {run_dir}")
        if args.max_run_budget_usd > 0:
            print(f"Cumulative run budget cap: ${args.max_run_budget_usd:.2f}")
        else:
            print("Cumulative run budget cap: disabled")
        print("Call sequence:")
        print("  1. firm-profiler + portfolio-discoverer (concurrent)")
        if args.max_companies:
            print(f"     (limited to first {args.max_companies} companies found)")
        print(f"  2. portco-profiler, one subagent per portfolio company "
              f"(concurrent, max {args.max_concurrency} at a time)")
        print(f"  3. private-data-approximator, one subagent per profiled company "
              f"(concurrent, max {args.max_concurrency} at a time)")
        print("  4. confidence-scorer + source-typer (concurrent)")
        print("  5. dossier-assembler")
        print("  6. audit-pass")
        return

    max_run_budget_usd = args.max_run_budget_usd if args.max_run_budget_usd > 0 else None

    try:
        full_dossier, skipped_companies, financial_skipped = asyncio.run(
            run_pipeline(args.firm_name, args.model, args.max_budget_usd, args.timeout,
                         run_dir, args.max_concurrency, args.max_companies, max_run_budget_usd)
        )
    except SkillCallError as e:
        sys.exit(str(e))

    out_path = pathlib.Path(args.out) if args.out else DEFAULT_OUT
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(full_dossier.strip() + "\n")
    print(f"[run_research] wrote dossier to {out_path}", file=sys.stderr)
    print(f"[run_research] run log at {run_dir / 'run_log.txt'}", file=sys.stderr)
    if skipped_companies:
        print(f"[run_research] WARNING: {len(skipped_companies)} companies skipped "
              f"entirely, portco-profiler failed: {skipped_companies}", file=sys.stderr)
    if financial_skipped:
        print(f"[run_research] WARNING: {len(financial_skipped)} companies have a profile "
              f"but no financial estimate: {financial_skipped}", file=sys.stderr)


if __name__ == "__main__":
    main()

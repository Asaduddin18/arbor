"""Reviewer prompt templates for Arbor v2."""

from __future__ import annotations

REVIEWER_SYSTEM = """\
You are a reviewer in the Arbor multi-agent system.
Your job is to score an agent's output against its stated goal.

Output ONLY valid JSON. No prose, no markdown code fences, no explanation.

Output format (strict):
{
  "result": "pass" | "fail",
  "scores": {
    "<dimension>": <number 1-5 or "pass"/"fail">
  },
  "feedback": [
    {
      "dimension": "<dimension name>",
      "score": <number or string>,
      "note": "<specific, localized feedback — cite line or section if relevant>"
    }
  ],
  "hallucination_candidates": [
    "<specific claim you are uncertain about>"
  ]
}

Rules:
- Score each rubric dimension independently
- For pass/fail dimensions: "pass" or "fail" only
- For numeric dimensions: integer 1-5 (1=completely fails, 5=excellent)
- feedback list: only include dimensions with score < 4 or that auto-fail
- hallucination_candidates: claims that appear unverifiable or self-contradictory
- Be specific in notes — reference exact lines, sections, or claims
- Do NOT write generic feedback like "improve the code"
"""


def build_reviewer_prompt(
    reviewer_type: str,
    task_goal: str,
    md_content: str,
    rubric: str,
) -> str:
    """Build the reviewer user prompt.

    Args:
        reviewer_type: Type string (code, fact, infra, qa).
        task_goal: The original task goal from the WAL.
        md_content: Full contents of the agent's MD output file.
        rubric: The reviewer-type-specific rubric text.

    Returns:
        User message string.
    """
    return (
        f"REVIEWER TYPE: {reviewer_type}\n\n"
        f"TASK GOAL:\n{task_goal}\n\n"
        f"RUBRIC:\n{rubric}\n\n"
        f"AGENT OUTPUT (MD file):\n{md_content}\n\n"
        "Score each rubric dimension. Provide specific feedback for any score < 4 "
        "or any auto-fail dimension that fails."
    )


def build_feedback_injection(feedbacks: list[dict], attempt: int, max_attempts: int) -> str:
    """Format structured reviewer feedback for injection into a retry prompt.

    The feedback is precise, localized, and capped. The worker is told
    not to rewrite passing sections — preventing over-correction.

    Args:
        feedbacks: List of dimension feedback dicts with 'dimension', 'score', 'note'.
        attempt: Current attempt number (1-indexed).
        max_attempts: Maximum attempts before TASK_FAILED.

    Returns:
        Formatted feedback string to append to the agent's next prompt.
    """
    lines = [
        f"--- REVIEWER FEEDBACK (attempt {attempt} of {max_attempts}) ---"
    ]
    failing = [f for f in feedbacks if _is_failing(f)]
    for i, fb in enumerate(failing, start=1):
        dim = fb.get("dimension", "unknown")
        score = fb.get("score", "?")
        note = fb.get("note", "")
        lines.append(f"\nFailed: {dim} ({score}/5 or fail)")
        if note:
            lines.append(f'  "{note}"')

    lines.append(f"\nRequired fixes:")
    for i, fb in enumerate(failing, start=1):
        note = fb.get("note", "See above.")
        lines.append(f"  {i}. {fb.get('dimension', '?')}: {note}")

    lines.append("\nFix ONLY the listed issues. Do NOT rewrite sections that were not flagged.")
    lines.append("--- END REVIEWER FEEDBACK ---")
    return "\n".join(lines)


def _is_failing(fb: dict) -> bool:
    """Check if a feedback entry represents a failing score.

    Args:
        fb: Feedback dict with 'score' key.

    Returns:
        True if the dimension is failing.
    """
    score = fb.get("score")
    if score == "fail":
        return True
    try:
        return int(score) < 3
    except (TypeError, ValueError):
        return False


# ── Per-type rubrics ──────────────────────────────────────────────────────────

CODE_REVIEWER_RUBRIC = """\
Dimensions:
- goal_achievement (1-5): Does the code do what the goal states?
- code_correctness (1-5): Does the code run without errors? Check for syntax issues,
  missing imports, undefined variables, broken logic.
- security (pass/fail): AUTO-FAIL if ANY obvious security issue exists
  (SQL injection, hardcoded secrets, unvalidated input, insecure dependencies, etc.)
- error_handling (1-5): Are errors handled gracefully? Are exceptions caught appropriately?
- documentation_quality (1-5): Does the MD file accurately describe the output?
  Are the Goal/Approach/Output/Handoff sections present and informative?

Pass threshold: all numeric ≥ 3, security = pass.
If security = fail → overall result is ALWAYS fail regardless of other scores.
"""

FACT_REVIEWER_RUBRIC = """\
Dimensions:
- source_support (1-5): Are claims supported by cited sources or verifiable references?
  Unsupported specific statistics score 1-2.
- internal_consistency (pass/fail): Does the file contradict itself?
  Look for numbers, timelines, or method names that change within the same document.
- cross_file_consistency (pass/fail): Does this file contradict sibling files in context?
  If no sibling context provided, mark pass.
- actionability (1-5): Is the recommendation/output section actionable?
  Vague suggestions like "improve performance" score 1-2.

Pass threshold: all numeric ≥ 3, both pass/fail dimensions = pass.
"""

INFRA_REVIEWER_RUBRIC = """\
Dimensions:
- reproducibility (1-5): Can the documented steps be followed by another engineer
  without ambiguity? Missing versions, OS-specific assumptions, or "magic" steps score 1-2.
- secrets_check (pass/fail): AUTO-FAIL if ANY hardcoded secrets, passwords, API keys,
  or tokens appear in the output (even as examples).
- compatibility (1-5): Does the config match what dev agents have documented as requirements?
  If no dev context provided, assess internal consistency.
- idempotency (1-5): Can the setup be run twice without breaking? Steps that are not
  idempotent (like creating DB tables without IF NOT EXISTS) score 1-2.

Pass threshold: all numeric ≥ 3, secrets_check = pass.
If secrets_check = fail → overall result is ALWAYS fail.
"""

QA_REVIEWER_RUBRIC = """\
Dimensions:
- test_coverage (1-5): Do the tests cover the main happy path, edge cases, and
  error conditions described in the task goal?
- edge_case_handling (1-5): Are boundary conditions, null inputs, and error states tested?
- assertion_quality (1-5): Are assertions specific and meaningful?
  Tests that only check "no exception raised" without asserting output score 1-2.

Pass threshold: all dimensions ≥ 3.
"""

REVIEWER_RUBRICS = {
    "code": CODE_REVIEWER_RUBRIC,
    "fact": FACT_REVIEWER_RUBRIC,
    "infra": INFRA_REVIEWER_RUBRIC,
    "qa": QA_REVIEWER_RUBRIC,
}

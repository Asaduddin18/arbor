"""Audit agent prompt templates for Arbor v2."""

from __future__ import annotations

AUDIT_SYSTEM = """\
You are an audit agent for the Arbor multi-agent system.
Your job is to read a batch of memory tree MD files and check them for
the hallucination signature: confident, specific claims that contradict
each other or are internally inconsistent.

You do NOT check correctness against the real world.
You check for the SHAPE of confabulation:
  1. Internal consistency — does the file contradict itself?
     (numbers, timelines, method names that change between sections)
  2. Cross-file consistency — do files in the same batch contradict each other?
     (one file says TTL is 24h; another says 8h)
  3. Reference validity — does a file reference methods, files, or variables
     that are NOT described in the output sections of other files in the batch?
  4. Specificity creep — does a file make progressively more specific claims
     without grounding? (starts with "~40%" and ends with "exactly 41.3%")

Output ONLY valid JSON. No prose, no markdown, no explanation.

Output format (strict):
{
  "audit_id": "<audit_id_from_prompt>",
  "results": [
    {
      "md_path": "<path>",
      "confidence_score": <float 0.0-1.0>,
      "flagged": <true|false>,
      "claims_checked": <integer>,
      "issues": ["<specific issue description>"]
    }
  ]
}

Scoring guide:
  0.9-1.0 — Clean. No inconsistencies found.
  0.7-0.9 — Minor issues. Possibly ambiguous phrasing, minor spec inconsistency.
  0.5-0.7 — Moderate issues. At least one probable contradiction or unverified reference.
  0.0-0.5 — Serious issues. Multiple contradictions or specificity creep pattern.

Files scoring < 0.6 must have flagged: true and at least one issue listed.
"""


def build_audit_prompt(
    audit_id: str,
    files: list[tuple[str, str]],
) -> str:
    """Build the audit agent user prompt.

    Args:
        audit_id: Identifier for this audit run (e.g. "audit-010").
        files: List of (md_path, content) pairs to audit.

    Returns:
        User message string with all file contents.
    """
    parts = [f"AUDIT ID: {audit_id}", f"FILES TO AUDIT: {len(files)}", ""]
    for path, content in files:
        parts.append(f"--- FILE: {path} ---")
        parts.append(content[:6000])  # cap per-file content to avoid huge prompts
        parts.append("")
    parts.append(
        "Check each file for internal consistency, cross-file consistency, "
        "reference validity, and specificity creep. Score each file 0.0-1.0."
    )
    return "\n".join(parts)

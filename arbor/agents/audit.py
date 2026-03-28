"""Audit agent for Arbor v2.

Reads batches of MD files from the same branch and checks them for
hallucination patterns: contradictions, unverified references, and
specificity creep.

Uses claude-sonnet-4-6. Cannot spawn children. Does not go through the
reviewer system (output is deterministic rules + LLM).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

from arbor.config import ArborConfig
from arbor.memory.flag_injector import inject_audit_flag
from arbor.memory.versioner import read_versioned_md
from arbor.prompts.audit import AUDIT_SYSTEM, build_audit_prompt
from arbor.wal import WalEventType, WalWriter

logger = logging.getLogger(__name__)

_MAX_JSON_RETRIES = 2
_FLAG_THRESHOLD = 0.6


@dataclass
class FileAuditResult:
    """Audit result for a single MD file.

    Attributes:
        md_path: Relative path to the audited file.
        confidence_score: 0.0–1.0. Below 0.6 → flagged.
        flagged: True if confidence < threshold.
        claims_checked: Number of distinct claims examined.
        issues: List of issue description strings.
    """

    md_path: str
    confidence_score: float
    flagged: bool
    claims_checked: int
    issues: list[str] = field(default_factory=list)


@dataclass
class AuditResult:
    """Result of an audit run.

    Attributes:
        audit_id: Identifier for this audit (e.g. "audit-010").
        files_audited: List of file paths audited.
        results: Per-file audit results.
    """

    audit_id: str
    files_audited: list[str]
    results: list[FileAuditResult]


class AuditAgent:
    """Audit agent that checks batches of MD files for hallucination patterns.

    Args:
        audit_id: Unique identifier for this audit run.
        config: ArborConfig.
        client: Optional AsyncAnthropic client.
    """

    def __init__(
        self,
        audit_id: str,
        config: ArborConfig,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self.audit_id = audit_id
        self.config = config
        self._client = client or anthropic.AsyncAnthropic()

    async def run_audit(self, files: list[Path]) -> AuditResult:
        """Audit a batch of MD files.

        Reads each file, strips frontmatter, sends to LLM for analysis,
        and returns structured results.

        Args:
            files: List of absolute Paths to MD files to audit.

        Returns:
            AuditResult with per-file confidence scores and issues.

        Raises:
            ValueError: If LLM returns invalid JSON after all retries.
        """
        # Read file contents (strip frontmatter for clean analysis)
        file_pairs: list[tuple[str, str]] = []
        for path in files:
            if not path.exists():
                logger.warning("Audit: skipping missing file %s", path)
                continue
            try:
                _, body = read_versioned_md(path)
                file_pairs.append((str(path), body))
            except OSError as exc:
                logger.warning("Audit: could not read %s: %s", path, exc)

        if not file_pairs:
            return AuditResult(
                audit_id=self.audit_id,
                files_audited=[],
                results=[],
            )

        user_prompt = build_audit_prompt(self.audit_id, file_pairs)
        last_error: Exception | None = None

        for attempt in range(1, _MAX_JSON_RETRIES + 1):
            prompt = user_prompt
            if last_error:
                prompt += f"\n\nPrevious response was not valid JSON: {last_error}. Output JSON only."

            logger.info(
                "AuditAgent %s calling LLM (attempt %d/%d, files=%d)",
                self.audit_id, attempt, _MAX_JSON_RETRIES, len(file_pairs),
            )

            response = await self._client.messages.create(
                model=self.config.agent_model_default,  # sonnet
                max_tokens=4096,
                system=AUDIT_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )

            raw = response.content[0].text.strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-z]*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw)
                raw = raw.strip()

            try:
                data = json.loads(raw)
                return self._parse_result(data, [str(p) for p in files])
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                last_error = exc
                logger.warning("AuditAgent JSON parse error (attempt %d): %s", attempt, exc)

        raise ValueError(
            f"AuditAgent {self.audit_id} failed to return valid JSON "
            f"after {_MAX_JSON_RETRIES} attempts: {last_error}"
        )

    def _parse_result(self, data: dict, file_paths: list[str]) -> AuditResult:
        """Parse raw LLM JSON into AuditResult.

        Args:
            data: Parsed JSON dict from LLM.
            file_paths: Original list of file paths (for fallback).

        Returns:
            AuditResult.
        """
        results: list[FileAuditResult] = []
        for r in data.get("results", []):
            score = float(r.get("confidence_score", 1.0))
            results.append(
                FileAuditResult(
                    md_path=r.get("md_path", ""),
                    confidence_score=score,
                    flagged=r.get("flagged", score < _FLAG_THRESHOLD),
                    claims_checked=r.get("claims_checked", 0),
                    issues=r.get("issues", []),
                )
            )

        files_audited = [r.md_path for r in results] or file_paths
        return AuditResult(
            audit_id=data.get("audit_id", self.audit_id),
            files_audited=files_audited,
            results=results,
        )

    async def run_and_record(
        self,
        files: list[Path],
        writer: WalWriter,
        run_id: str,
        memory_base: Path | None = None,
    ) -> AuditResult:
        """Run audit, write WAL entry, and inject flags for flagged files.

        Args:
            files: List of MD file Paths to audit.
            writer: WalWriter to append WAL entries.
            run_id: Active run ID.
            memory_base: Base path for the memory tree (for flag injection).

        Returns:
            AuditResult.
        """
        result = await self.run_audit(files)

        writer.write(
            WalEventType.AUDIT_RESULT,
            run_id=run_id,
            payload={
                "audit_id": self.audit_id,
                "files_audited": result.files_audited,
                "results": [
                    {
                        "md_path": r.md_path,
                        "confidence_score": r.confidence_score,
                        "flagged": r.flagged,
                        "claims_checked": r.claims_checked,
                        "issues": r.issues,
                    }
                    for r in result.results
                ],
            },
        )

        # Inject flags for files below threshold
        for file_result in result.results:
            if file_result.flagged:
                writer.write(
                    WalEventType.MD_FLAGGED,
                    run_id=run_id,
                    payload={
                        "md_path": file_result.md_path,
                        "audit_id": self.audit_id,
                        "confidence_score": file_result.confidence_score,
                        "issues": file_result.issues,
                    },
                )
                # Inject flag into the actual file if we can find it
                if memory_base:
                    # Try to find the file relative to memory_base or as absolute
                    candidate = Path(file_result.md_path)
                    if not candidate.is_absolute():
                        candidate = memory_base / file_result.md_path
                    if candidate.exists():
                        inject_audit_flag(
                            candidate,
                            audit_id=self.audit_id,
                            confidence=file_result.confidence_score,
                            issues=file_result.issues,
                        )

        logger.info(
            "AuditAgent %s: audited %d files, flagged %d",
            self.audit_id,
            len(result.results),
            sum(1 for r in result.results if r.flagged),
        )
        return result

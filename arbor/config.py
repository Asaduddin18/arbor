"""Configuration loader for Arbor v2.

Reads arbor.config TOML file and provides a typed ArborConfig dataclass.
"""

import tomllib
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ArborConfig:
    """Typed configuration for an Arbor run.

    Attributes:
        max_depth: Maximum tree depth allowed (1–10).
        context_budget_per_agent: Token budget per agent before handoff (>1000).
        reviewer_model: Model ID for reviewer agents.
        orchestrator_model: Model ID for the orchestrator.
        agent_model_default: Default model ID for worker agents.
        audit_every_n_tasks: Run audit after this many task completions.
        max_review_attempts: Max reviewer retries before TASK_FAILED (1–10).
        log_level: Python logging level string.
        wal_dir: Directory where wal.ndjson is stored.
        memory_dir: Root directory for the memory tree.
    """

    max_depth: int = 4
    context_budget_per_agent: int = 8000
    reviewer_model: str = "claude-haiku-4-5-20251001"
    orchestrator_model: str = "claude-opus-4-6"
    agent_model_default: str = "claude-sonnet-4-6"
    audit_every_n_tasks: int = 10
    max_review_attempts: int = 3
    log_level: str = "INFO"
    wal_dir: str = "arbor-run"
    memory_dir: str = "memory"


def _validate(cfg: ArborConfig) -> None:
    """Validate config values and raise ValueError on bad input.

    Args:
        cfg: The ArborConfig to validate.

    Raises:
        ValueError: If any field is out of range.
    """
    if not (1 <= cfg.max_depth <= 10):
        raise ValueError(f"max_depth must be 1–10, got {cfg.max_depth}")
    if cfg.context_budget_per_agent <= 1000:
        raise ValueError(
            f"context_budget_per_agent must be >1000, got {cfg.context_budget_per_agent}"
        )
    if not (1 <= cfg.max_review_attempts <= 10):
        raise ValueError(
            f"max_review_attempts must be 1–10, got {cfg.max_review_attempts}"
        )
    if cfg.audit_every_n_tasks < 1:
        raise ValueError(
            f"audit_every_n_tasks must be ≥1, got {cfg.audit_every_n_tasks}"
        )


def load_config(path: Path) -> ArborConfig:
    """Load ArborConfig from a TOML file.

    Args:
        path: Path to the arbor.config TOML file.

    Returns:
        Validated ArborConfig populated from the file, with defaults for
        any missing keys.

    Raises:
        FileNotFoundError: If the config file does not exist.
        ValueError: If any config value is invalid.
        tomllib.TOMLDecodeError: If the file is not valid TOML.
    """
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    section = raw.get("arbor", raw)  # support [arbor] table or flat file

    defaults = ArborConfig()
    cfg = ArborConfig(
        max_depth=section.get("max_depth", defaults.max_depth),
        context_budget_per_agent=section.get(
            "context_budget_per_agent", defaults.context_budget_per_agent
        ),
        reviewer_model=section.get("reviewer_model", defaults.reviewer_model),
        orchestrator_model=section.get(
            "orchestrator_model", defaults.orchestrator_model
        ),
        agent_model_default=section.get(
            "agent_model_default", defaults.agent_model_default
        ),
        audit_every_n_tasks=section.get(
            "audit_every_n_tasks", defaults.audit_every_n_tasks
        ),
        max_review_attempts=section.get(
            "max_review_attempts", defaults.max_review_attempts
        ),
        log_level=section.get("log_level", defaults.log_level),
        wal_dir=section.get("wal_dir", defaults.wal_dir),
        memory_dir=section.get("memory_dir", defaults.memory_dir),
    )

    _validate(cfg)
    logger.debug("Loaded config from %s: %s", path, cfg)
    return cfg


def get_default_config() -> ArborConfig:
    """Return a default ArborConfig without reading any file.

    Returns:
        ArborConfig with all hardcoded defaults.
    """
    return ArborConfig()

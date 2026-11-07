"""Stage prompts loaded from ``prompts/`` and the versions recorded on each ModelCall."""

from pathlib import Path

from contextforge.core.entities.model_call import Stage

# Resolved from this file (src/contextforge/core/context/), not the CWD, so prompts load
# wherever the process is started.
PROMPTS_DIR = Path(__file__).resolve().parents[4] / "prompts"

# Version of prompts/agent-memory.md; bump it with any edit to that file.
MEMORY_VERSION = "2"

# The stage prompt file each stage uses; its name is the version written to context_version.
_STAGE_PROMPTS: dict[Stage, str] = {
    Stage.PLAN: "plan.4",
    Stage.EXECUTE: "execute.5",
    Stage.COMPRESS: "compress.2",
}


def load_prompt(name: str) -> str:
    """Return ``prompts/<name>.md`` with trailing whitespace stripped."""
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8").rstrip()


def prompt_version(stage: Stage) -> str:
    """The prompt version for ``stage``, or ``"none"`` for stages without a prompt file."""
    return _STAGE_PROMPTS.get(stage, "none")

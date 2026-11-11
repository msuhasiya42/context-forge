"""Stage prompts loaded from ``prompts/`` and the versions recorded on each ModelCall."""

import re
from pathlib import Path

from contextforge.core.entities.model_call import Stage

# Resolved from this file (src/contextforge/core/context/), not the CWD, so prompts load
# wherever the process is started.
PROMPTS_DIR = Path(__file__).resolve().parents[4] / "prompts"

# Version of prompts/agent-memory.md; bump it with any edit to that file.
MEMORY_VERSION = "2"

# The stage prompt file each stage uses; its name is the version written to context_version.
_STAGE_PROMPTS: dict[Stage, str] = {
    Stage.PLAN: "plan.6",
    Stage.EXECUTE: "execute.9",
    Stage.COMPRESS: "compress.2",
    Stage.SUMMARY: "summary.1",
    Stage.CITE: "cite.1",
    Stage.REPORT: "report.1",
}


# A leading HTML comment is a note for maintainers (an attribution), never sent to a model.
_LEADING_COMMENT = re.compile(r"\A\s*<!--.*?-->\s*", re.DOTALL)


def load_prompt(name: str, **values: int | str) -> str:
    """Return ``prompts/<name>.md`` without its leading comment or trailing whitespace, each
    ``{key}`` of ``values`` replaced, so a limit the prompt states is the configured one."""
    text = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8").rstrip()
    text = _LEADING_COMMENT.sub("", text)
    for key, value in values.items():
        text = text.replace(f"{{{key}}}", str(value))
    return text


def prompt_version(stage: Stage) -> str:
    """The prompt version for ``stage``, or ``"none"`` for stages without a prompt file."""
    return _STAGE_PROMPTS.get(stage, "none")

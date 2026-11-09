"""Token estimation for context budgeting."""

import math


def estimate_tokens(text: str) -> int:
    """Estimate the tokens in ``text`` as one per four characters, at least one.

    A phase 1 heuristic; phase 2 replaces it with a real tokenizer behind this signature.
    """
    return max(1, math.ceil(len(text) / 4))

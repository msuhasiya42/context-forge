"""The one credential guard every memory writer uses (system design section 5).

A guard against the common shapes of secrets, not a guarantee: a credential that looks like
ordinary prose passes. It errs towards refusing, since a refused edit costs the user one retry
and a stored secret is sent with every later model call.
"""

import re

_SECRET_SHAPES = [
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),  # OpenAI and Anthropic style keys
    re.compile(r"\blsv2_[A-Za-z0-9_]{16,}"),  # LangSmith
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),  # GitHub
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),  # Slack
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWT
]
# URLs are removed before the long-token check: their paths are long mixed runs too.
_URL = re.compile(r"https?://\S+")
_LONG_TOKEN = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
_HEX = re.compile(r"[0-9a-fA-F]{32,}")


def contains_credential(text: str) -> bool:
    """True when ``text`` holds something shaped like a key, token, password or secret."""
    if any(shape.search(text) for shape in _SECRET_SHAPES):
        return True
    return any(_looks_random(token) for token in _LONG_TOKEN.findall(_URL.sub(" ", text)))


def _looks_random(token: str) -> bool:
    """A long hex run with digits and letters, or 40+ characters mixing upper case, lower case
    and digits: the shape of generated secrets such as AWS secret keys, unlike words."""
    has_digit = any(c.isdigit() for c in token)
    if _HEX.fullmatch(token):
        return has_digit and any(c.isalpha() for c in token)
    has_upper = any(c.isupper() for c in token)
    has_lower = any(c.islower() for c in token)
    return len(token) >= 40 and has_digit and has_upper and has_lower

"""Model output that failed its schema, as one line for a retry constraint or failure reason."""

from pydantic import ValidationError


def one_line(error: ValidationError) -> str:
    """Each error as ``<field path>: <message>`` joined by "; "."""
    return "; ".join(
        f"{'.'.join(str(part) for part in e['loc'])}: {e['msg']}" if e["loc"] else e["msg"]
        for e in error.errors()
    )

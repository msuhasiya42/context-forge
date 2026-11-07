"""Tools the execute stage offers the model, and which task kinds may call them."""

from contextforge.core.entities.task import TaskKind

# A kind missing here gets no tools; phase 1 implements only sql_analysis.
TOOL_NAMES_BY_KIND: dict[TaskKind, tuple[str, ...]] = {
    TaskKind.SQL_ANALYSIS: ("sql_query",),
    TaskKind.WEB_SEARCH: ("web_search", "fetch_url"),
}

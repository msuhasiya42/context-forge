import json

from contextforge.core.models.scripted import ScriptedModel
from contextforge.infra.web_guard import SeenUrls, urls_in
from tests.helpers import make_deps
from tests.test_web_research import _ctx, _fetcher, _page, _web


def test_seen_urls_preserve_balanced_parentheses_and_safe_equivalent_spellings() -> None:
    url = "https://example.org/wiki/Test_(example)?q=public"
    seen = SeenUrls(lambda _: f"Read ({url}).")
    assert url in urls_in(f"Read ({url}).")
    assert seen.allows(_ctx(), url)
    assert "https://example.org/Test_(example)" in urls_in(
        "Read https://example.org/Test_(example)."
    )
    seen.add("t1", ["https://EXAMPLE.org/%7Eteam#section", "https://example.org"])
    assert seen.allows(_ctx(), "https://example.org/~team")
    assert seen.allows(_ctx(), "https://example.org/")
    assert not seen.allows(_ctx(), "https://example.org/~team?secret=new")
    assert not seen.allows(_ctx(), "https://example.org/~team/private")


def test_fetched_evidence_keeps_provenance_without_inventing_publication_dates() -> None:
    worker_dependencies = make_deps(ScriptedModel({}))
    html = (
        '<meta property="article:published_time" content="2026-01-02T00:00:00Z">'
        '<meta property="og:site_name" content="Example Research">'
        "<p>The retrieval failure rate fell from 5.7% to 1.9%.</p>"
    )
    web = _web(lambda _: _page(html, "Measured result"))
    tool = _fetcher(web, worker_dependencies, "Read https://example.org/report")
    result = tool.run({"url": "https://example.org/report"}, _ctx())
    artifact = worker_dependencies.repositories.artifacts.get("c1", result.artifact_ids[0])
    snapshot = json.loads(worker_dependencies.blobs.get(artifact.storage_ref))
    assert snapshot["requestedUrl"] == snapshot["url"] == "https://example.org/report"
    assert snapshot["title"] == "Measured result"
    assert snapshot["publisher"] == "Example Research"
    assert snapshot["publishedAt"] == "2026-01-02T00:00:00Z"
    assert snapshot["retrievedAt"] != snapshot["publishedAt"]
    assert len(snapshot["contentHash"]) == 64

    undated = _web(lambda _: _page("<p>No publication date. Copyright 2026.</p>"))
    page = undated.fetch("https://example.org/undated")
    assert page["publishedAt"] is None

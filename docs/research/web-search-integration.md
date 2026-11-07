# Web search integration: first working research slice

Researched 7 November 2026 against primary sources. This is a recommendation, not proof of a successful live search. No account was created and no paid API was called.

## Recommendation

Keep Bedrock and the existing LangGraph engine. Add a small provider-backed `web_search` Tool and a bounded `fetch_url` Tool, then expose them to a new `web_search` task kind. A web-first request can have no uploaded inputs. Search results and fetched pages become scoped raw-result Artifacts, giving the existing Task.result and report claims something concrete to cite.

For the user's Google preference, use a configurable Serper adapter once a `SERPER_API_KEY` is available. Serper is an independent service returning Google results, not Google's own search API. Its official page advertises JSON results (including organic title/link/snippet/position), a 2,500-query trial without a credit card, and Google location/language selection. Availability and quotas remain account-specific. [Serper](https://serper.dev/)

Do not start a new integration with Google's Custom Search JSON API: Google says it is closed to new customers and existing customers must transition by 1 January 2027. Google points narrow-domain search to Vertex AI Search and full-web users to an inquiry form, which is not the quickest self-service route for this project. [Google API overview](https://developers.google.com/custom-search/v1/overview)

An explicit DuckDuckGo provider can be offered for development without a search API key, but it must be labeled as DuckDuckGo rather than silently substituted for Google. The community MCP server supplies search and fetch tools. Its own documentation warns that plain httpx requests can be blocked with empty 202 responses; its automatic search fallback needs the optional `browser` extra (`curl_cffi`). Therefore it is useful for learning and a smoke test, not a reliability guarantee. [DuckDuckGo MCP](https://github.com/nickclyde/duckduckgo-mcp-server)

## MCP versus a native Tool

MCP is a transport/interface for discovering and calling tools. It does not provide a search index or Google credentials. Either a direct provider client or an MCP client still needs a small adapter to the project's Tool Protocol and result/evidence conventions. The official SDK documents `list_tools` and `call_tool` for that route. [MCP Python client](https://py.sdk.modelcontextprotocol.io/client/)

The fastest Google-backed route is a native Serper Tool behind the current Tool boundary. A later MCP provider adapter can expose the same app-level contract without changing the planner/task workflow. If practicing MCP immediately is a requirement, the DuckDuckGo MCP server can be launched locally over stdio; no separate remote MCP deployment is needed. Its tools return formatted text, so persist the returned content and stable full URLs rather than inventing structured provenance.

The currently inspected DuckDuckGo source declares package version 0.7.0 and MCP SDK `>=2.1.1,<3`; its optional browser extra adds `curl_cffi`. The official fetch server source declares 2026.11.4 and MCP `>=2.3,<3`. Pin and resolve actual released distributions before implementation rather than copying old MCP 1.x examples. [DuckDuckGo package source](https://github.com/nickclyde/duckduckgo-mcp-server/blob/main/pyproject.toml), [fetch package source](https://github.com/modelcontextprotocol/servers/blob/main/src/fetch/pyproject.toml)

Serper's unauthenticated public page did not expose its request-header/endpoint reference during this research; its playground is interactive. Validate the intended `POST https://google.serper.dev/search` and `X-API-KEY` contract against the account's generated playground example before claiming live acceptance. No credential was inspected or printed.

## Minimal application contract

These names and limits are suggested implementation defaults, not newly approved entity fields:

- `web_search(query, max_results=5)`: validate a nonempty bounded query; use a server-configured provider; return a bounded list with title, full HTTP(S) URL, snippet, position and a source Artifact ID. Reject absent Google credentials with an actionable configuration error. Cap result count server-side and keep provider credentials out of arguments and returned content.
- `fetch_url(url)`: read public HTTP(S) content, extract readable text, retain original/final URL and retrieval time, and store the bounded downloaded snapshot in an Artifact. Return a bounded excerpt plus the Artifact ID and any coverage/truncation information. Keep credentials, cookies and browser sessions out of the fetcher.
- Configure connect/read timeouts, total download deadline, byte ceiling, text/row ceiling, redirect ceiling, request search budget and per-host concurrency. Suggested first-slice values: five results, 15-second search timeout, 20-second fetch deadline, 1 MB download ceiling, 8,000-character excerpt and at most three redirects. Put selected values in config rather than model instructions.
- A search hit is discovery evidence; read its page before presenting a detailed factual claim as supported. Preserve unsupported or inaccessible points under uncertainties. Search failure is an error outcome, not an empty successful answer.
- Treat all snippets and pages as untrusted data. A page cannot add tools, override instructions, grant permissions or request external writes.

## SSRF and fetch behavior

Both the current DuckDuckGo server and the reference fetch server document refusal of private, loopback, link-local and cloud metadata addresses, plus redirect checks. Leave those guards enabled. The reference server also documents a DNS-rebinding gap between validation and connection. A native implementation should validate all resolved addresses, revalidate redirect hops, disallow credentials in URLs and avoid carrying sensitive headers across hosts; stronger isolation/egress policy is required before a public multiuser deployment. [DuckDuckGo security model](https://github.com/nickclyde/duckduckgo-mcp-server/blob/main/SECURITY.md), [reference fetch behavior](https://github.com/modelcontextprotocol/servers/blob/main/src/fetch/README.md)

The reference fetch server supports `max_length`, `start_index` and per-call timeout, and normally obeys robots rules for tool-originated requests. Its documented timeout bounds connection/read operations rather than the total download, so an application-wide deadline and byte ceiling are still needed. It also optionally invokes Node extraction and installs dependencies on first use; pin dependencies and disable that path if an entirely Python, predictable runtime is preferred. [Reference fetch README](https://github.com/modelcontextprotocol/servers/blob/main/src/fetch/README.md)

## Fit with the code inspected today

- `core/values.py` already has `LocatorKind.URL` and EvidenceRef; no new evidence entity is necessary.
- The existing ToolResult supports rows and text. Structured result rows are enough for initial search results without building a new generic framework.
- Add the task kind to TaskKind, planner accepted output, stage prompts, tool-name map and execution dispatch. Currently only `sql_analysis` reaches the tool/model loop; adding a tool alone does not make web requests execute.
- Permit an input-free web task. Authorize a search/page Artifact created by that task as evidence, while continuing to reject arbitrary foreign-chat or unrelated-task Artifact IDs.
- Persist source snapshots in the configured BlobStore and return EvidenceRef with `{artifactId, locator: {kind: "url", value: full_url}, excerpt}`. A provider's temporary `ref://` token is not a durable public citation; expand it before storing.
- Include provider, retrieval time and snapshot reference in trace/result metadata. Keep an answer UNKNOWN when the code has only checked reference validity rather than factual entailment.

## Acceptance and outstanding setup

Offline integration: API submit -> queue consumer -> planner -> web tool -> source Artifact -> result -> report Message -> SSE; assert actual cited URLs, no upload requirement, bounded output, search failure handling and blocked private redirects. Mock external HTTP only at the provider boundary. Add a separately enabled live smoke using a real Bedrock model and actual search.

Google route needs the user to supply `SERPER_API_KEY` through local settings. Bedrock needs the already discussed dev profile permissions; LangSmith needs its API key if tracing is enabled. The no-key DuckDuckGo route avoids search signup but still requires outbound network and a real live check. No fake response or fixture-backed test should be described as working Google search.

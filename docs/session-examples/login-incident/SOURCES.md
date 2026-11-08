# Verified tool names for the incident walkthrough

Checked 2026-11-08 against primary sources. These are documented external interfaces, not
integrations already installed in ContextForge. No live server schema or incident data was
queried. The JSON fixtures remain fictional.

| Purpose | Provider and registered tool name | Primary reference |
|---|---|---|
| Query login metrics | Grafana MCP: `query_prometheus` | [Grafana implementation](https://github.com/grafana/mcp-grafana/blob/main/tools/prometheus.go) |
| Find Jira site IDs | Atlassian Rovo MCP: `getAccessibleAtlassianResources` | [Atlassian tool catalog](https://developer.atlassian.com/cloud/rovo-mcp/guides/supported-tools/) |
| Search issues | Atlassian Rovo MCP: `searchJiraIssuesUsingJql` | [Atlassian tool catalog](https://developer.atlassian.com/cloud/rovo-mcp/guides/supported-tools/) |
| Create an issue | Atlassian Rovo MCP: `createJiraIssue` | [Atlassian tool catalog](https://developer.atlassian.com/cloud/rovo-mcp/guides/supported-tools/) |
| Locate release notes | Google Drive MCP: `search_files` | [Google search tool](https://developers.google.com/workspace/drive/api/reference/mcp/tools_list/search_files) |
| Read their contents | Google Drive MCP: `read_file_content` | [Google content tool](https://developers.google.com/workspace/drive/api/reference/mcp/tools_list/read_file_content) |

## Names are not a complete integration contract

MCP clients discover definitions with `tools/list`; each includes a name and an `inputSchema`.
Invocation uses `tools/call` with that name and arguments. Preserve the registered name in
the server request. A host may assign its own provider prefix to distinguish tools; that
host-side alias must map back to the discovered server name. A REST operation name is not
sufficient evidence of an MCP tool. [MCP tools specification](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)

Atlassian exposes primary tools directly and supports deferred discovery through `discover`,
then `executeRead`, `executeWrite` or `executeDestructive`. Actual availability depends on
access and configuration. Obtain current schemas before constructing executable Jira
arguments; the public catalog verifies names but is not a complete argument specification.
[Atlassian discovery and availability](https://developer.atlassian.com/cloud/rovo-mcp/guides/supported-tools/)

## Provider-specific corrections

Grafana's query tool accepts a datasource UID and PromQL expression. Its range-query fields
include `startTime`, `endTime`, `stepSeconds` and `queryType`. The datasource and metric names
are deployment-specific: our login metric names remain fictional. The returned time-series
structure is not automatically the tutorial's row table; any conversion into rows is a
separate, proposed adapter step. Pin and inspect the deployed server version because the
linked source is a moving branch. [Grafana query implementation](https://github.com/grafana/mcp-grafana/blob/main/tools/prometheus.go)

Google's official Drive MCP tools are marked **Developer Preview**. `search_files` takes
`query`, with optional pagination and snippet controls; its documented search syntax includes
`title` and `parentId`. Do not substitute Drive REST query syntax blindly. Use the returned
file ID for the separate content read. [Google search schema](https://developers.google.com/workspace/drive/api/reference/mcp/tools_list/search_files)

`read_file_content` takes `fileId` and optional `includeComments`. Google warns that large
files may produce incomplete content. Saving that response as an Artifact cannot recover
text the provider omitted. [Google content schema and limitations](https://developers.google.com/workspace/drive/api/reference/mcp/tools_list/read_file_content)

Finally, `query_artifact` is our **proposed application-owned tool**, not a tool supplied by
Grafana, Atlassian or Google. Its schema in this walkthrough is a design contract requiring
implementation and registration in ContextForge.


---

# Response formats: verified boundaries

Checked 2026-11-08. These sources support synthetic response examples; they do not turn the
fixtures into captured live responses. Preserve the distinction between provider data, MCP
transport results, and the application's normalized tool result.

## MCP envelope

A successful JSON-RPC response places a tool result under `result`. The tool result can
contain `content` blocks and optional `structuredContent`. Structured content is an object;
servers returning it should also supply a serialized text block for compatibility. An
optional advertised `outputSchema` constrains that structured result. This is server data,
not the LLM's schema-constrained generation. Do not assume every server supplies both forms.
[MCP tools specification](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)

## Grafana `query_prometheus`

The inspected implementation serializes `QueryPrometheusResult`: `data` is a Prometheus
`model.Value`; optional fields include `hints`, `warnings` and `exploreUrl`. It serializes
that object into a text block and attaches the same object as structured content only when
within its view budget. Therefore a fixture shaped as
`{"status":"success","data":{"resultType":"matrix","result":[...]}}` would describe a
Prometheus HTTP API envelope, not this MCP handler's result payload.
[Grafana implementation](https://raw.githubusercontent.com/grafana/mcp-grafana/main/tools/prometheus.go)

For an ordinary float range result, `data` is a matrix: an array of streams containing
`metric` labels and `values` sample pairs. Instant vectors instead contain individual
samples. Native histogram results require other fields. A tutorial adapter can transform
these streams into rows, but those rows are application output rather than the untouched
MCP response. Both linked source branches move; verify against the deployed version.
[Prometheus model serialization](https://raw.githubusercontent.com/prometheus/common/main/model/value.go)

## Google Drive

`search_files` documents an output object with `files` and optional `nextPageToken`. Its
linked File definition includes fields such as `id`, `title`, `mimeType` and `viewUrl`.
Use the documented schema, not invented search-result fields, for provider-level examples.
[Google search output schema](https://developers.google.com/workspace/drive/api/reference/mcp/tools_list/search_files)

`read_file_content` documents `fileContent`, comment collections, and optional unsupported-
feature indicators such as `textFormattingNotSupported` and `commentsNotSupported`. The
reference warns that very large files can yield incomplete text. It does not promise a
universal `truncated` boolean or an offset-based continuation API. These tools are Developer
Preview. A fabricated continuation token must not be presented as a provider feature.
[Google content output schema](https://developers.google.com/workspace/drive/api/reference/mcp/tools_list/read_file_content)

## Jira REST is not a verified Rovo MCP wrapper

The official Jira create REST example returns `id`, `key`, `self` and potentially transition
information. `self` identifies an API resource; it is not the human-facing browse URL.
[Jira create issue response](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-post)

Enhanced JQL search REST examples contain `issues` with `id`, `key`, `self`, `fields`, plus
pagination state such as `isLast`. Requested fields affect issue detail.
[Jira enhanced search response](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/#api-rest-api-3-search-jql-get)

Those REST examples do **not** establish that Rovo's `searchJiraIssuesUsingJql` or
`createJiraIssue` returns identical content or wrappers. Label any REST-derived MCP fixture
as an illustrative normalization until an authenticated Rovo tool result/schema is captured.
The public Rovo catalog verifies tool names and availability, not these exact response bytes.
[Atlassian Rovo catalog](https://developer.atlassian.com/cloud/rovo-mcp/guides/supported-tools/)

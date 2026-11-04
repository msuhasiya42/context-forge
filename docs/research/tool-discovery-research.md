# Selective tool discovery for the research agent

Researched 3 November 2026. This is a design recommendation, not an implemented integration.

## Simple explanation

The user's “REG for tools” is retrieval-augmented tool selection, often called tool retrieval, tool search, or progressive disclosure. For “check my credit card bill,” retrieve Gmail search/read capabilities and relevant payment-query tools; do not put every Gmail/Drive operation into every model request. Retrieval chooses candidate capabilities; execution remains a separate authorized operation.

## First-party findings

### Provider-native discovery

Anthropic documents deferred tool definitions that load into model context when discovered. Server-side discovery still receives full definitions in the API request, so deferred loading does not imply that the application never sends the catalog. Current documentation explicitly limits Bedrock server-side tool search to InvokeModel, excluding Converse. Availability depends on the selected model/API; verify those and the deployment region when implementing rather than assuming any Bedrock model supports it. [Tool search documentation](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool)

This is an optional adapter optimization, not our portable baseline. Direct-provider model changes must not alter authorization or silently drop tool discovery.

### LLM selector versus retrieval

LangChain's LLMToolSelectorMiddleware invokes a model to choose tools, supports a maximum and always-included tools, then filters the tools passed to the main model. Inspection of its implementation shows that the selection response schema contains candidate tool names and descriptions, rather than each candidate's complete argument schema. It still adds a selection-model call and catalog-description input; it is not a vector index or free selection. Account for selector calls in usage and evaluate whether they save total tokens/latency. [API reference](https://reference.langchain.com/python/langchain/agents/middleware/tool_selection/LLMToolSelectorMiddleware), [official source](https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/middleware/tool_selection.py)

Do not use a parsing-failure fallback that exposes every tool without applying our own authorization and token bounds. Middleware defaults and versions must be pinned and checked before implementation.

### MCP discovery is not model-context selection

MCP exposes named tools with schemas and supports paginated discovery and catalog-change notification. Discovering tools from a server does not require passing every discovered schema to the model. [MCP tools specification](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)

Current LangChain documentation describes MCPAdapter in the beta langchain.mcp namespace, requiring langchain[mcp]>=1.4.0; it points existing langchain-mcp-adapters users to migration guidance. Select and pin a verified adapter version rather than copying an older example blindly. Authentication setup and credential lifetime still need explicit application design. [LangChain MCP documentation](https://docs.langchain.com/oss/python/langchain/mcp)

## Proposed app pipeline

1. Discover provider capabilities and persist IntegrationTool metadata with stable namespaced ID, description, input/output schema, version/hash, tags, and known permission requirements. Bind account access through IntegrationConnection; a provider catalog record is not a user's authorization.
2. Construct the candidate catalog from connected accounts, allowed operations, read/write policy, current scopes, and task purpose. Filter unauthorized capabilities before relevance ranking. Do not index tokens or account secrets.
3. For the initial 20–30 tools, use capability tags, namespaces, aliases, and lexical/BM25 ranking in Python over an authorized catalog. This avoids a separate vector service or selection model call. It is an engineering recommendation, not a DynamoDB full-text search capability.
4. Return a small candidate set constrained by schema-token budget. Include essential companion capabilities, such as reading an item after searching it. Start with a configurable limit rather than a universal hardcoded top-k.
5. Provide a compact search_tools capability or an equivalent graph-controlled discovery step so the agent can refine an unsuccessful search or obtain a missing tool. No match should produce an explicit discovery gap, not an invented tool call. Bound discovery rounds.
6. Load current full schemas only for selected tools into the task's model call. Validate generated arguments and recheck current authorization at execution; discovery relevance is not permission. Preserve schema/tool-call compatibility across subsequent messages.
7. Add semantic/hybrid retrieval only when catalog growth, terminology mismatch, or measured tool-recall failures justify it. If S3 Vectors is later used, use versioned metadata, authorized namespaces, appropriate embedding model/version, and revalidation after retrieval. Document RAG and tool retrieval share a technique but have different indexes and evaluation targets.

For complex requests, discovery may legitimately include multiple provider categories. Do not filter so narrowly that a refund investigation cannot discover Gmail and payment-query tools together. The planner's task goal, current evidence gap, and source scope should influence the retrieval query, not just the last user sentence.

## Entities and observability

No new ToolIndex business entity is necessary initially: IntegrationTool is the authoritative catalog; an index/cache is derived infrastructure. Store catalog/schema revisions with ModelCall context references. Trace the discovery operation and any selector-model call, attach jobId/taskId, and capture candidate count, selected tools, schema tokens, discovery retries, tool failures, latency, and cost.

LangSmith traces reveal behavior; efficiency requires evaluation against representative outcomes. Use synthetic queries with known necessary capability sets, test renamed/overlapping tools, verify permission filtering and revoked connections, and compare retrieval recall, final-answer correctness, unnecessary calls, and total cost against a small all-tools baseline. Do not equate fewer calls with better results. [LangSmith evaluation types](https://docs.langchain.com/langsmith/evaluation-types)

## Mental model and diagram

Catalog discovery inventories capabilities. Retrieval selects relevant capabilities. Authorization governs use. Tracing measures what happened; evaluation establishes whether it helped.

```text
MCP discovery -> IntegrationTool catalog
                         |
Task goal -> permission-filter -> rank -> budgeted schemas -> model
                                                         |
                                             validate + authorize -> tool
```

One liner: select relevant authorized tools, expose only necessary schemas, and validate permission again before execution.

Interview question: Does tool RAG replace access control? No. Relevance ranking neither proves authorization nor guarantees tool correctness; apply access filters before retrieval and enforce current permissions at execution.

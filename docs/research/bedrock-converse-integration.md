# Bedrock Converse integration notes

Researched 7 November 2026 from AWS documentation. No account calls or paid inference were made. These are adapter implementation notes for the existing Python/LangGraph stack.

## Model selection and access

Use boto3's `bedrock-runtime` client in `ap-southeast-2`. AWS's current model cards list `amazon.nova-pro-v1:0` and `amazon.nova-lite-v1:0`, with geo profiles `apac.amazon.nova-pro-v1:0` and `apac.amazon.nova-lite-v1:0`; Sydney supports both in-region and geo inference for these models. Both support Converse and client-side tool calling. The cards mark native Structured outputs as unsupported, so do not send an unsupported `outputConfig.textFormat` expecting it to work on v1. [Nova Pro model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-amazon-nova-pro.html), [Nova Lite model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-amazon-nova-lite.html)

An APAC profile invoked in Sydney may route to other APAC Regions. Confirm its actual destinations with GetInferenceProfile before claiming Sydney-only inference, and permit inference in all its destination Regions. Profile IDs do not prove account-level access. Converse requires `bedrock:InvokeModel`; streaming additionally requires the corresponding streaming action. [Inference profiles](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-support.html), [Converse API](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html)

## Minimum request mapping

The call consists of `modelId`, `system`, `messages`, `inferenceConfig` and, only when needed, `toolConfig`. The request uses the Bedrock schema rather than Anthropic-specific messages or OpenAI function-call fields. For the current text-only path, pass a list of `{text: ...}` system blocks, and user/assistant turns with lists of content blocks. Nova v1 documents a user turn first. `maxTokens`, `temperature` and `topP` are ordinary inference fields; model-specific parameters use `additionalModelRequestFields`. [Nova v1 Converse guide](https://docs.aws.amazon.com/nova/latest/userguide/using-converse-api.html), [Nova v1 request schema](https://docs.aws.amazon.com/nova/latest/userguide/complete-request-schema.html)

Suggested adapter normalization (application policy, not a claim that every provider needs it):

1. Collect only the leading trusted SystemMessages into top-level `system`.
2. HumanMessage becomes `role=user`, AIMessage becomes `role=assistant`.
3. Merge adjacent turns with the same role by concatenating their block lists, preserving block order.
4. Treat a later compaction SystemMessage as quoted conversation-summary data in a user block. Do not promote a summary derived from untrusted sources into new top-level instructions.
5. Ensure a nonempty initial user turn; if a compression transcript starts with assistant history, prefix an explicit user instruction explaining that the following transcript is data to summarize.
6. If a compression call supplies no tools, render historical tool requests/results as plain quoted text. Sending live tool blocks without their definitions/pairing is not a safe general-purpose compression format.

## Tools and structured result

A tool definition has this shape:

```json
{
  "toolSpec": {
    "name": "web_search",
    "description": "Search public web sources for a query.",
    "inputSchema": {
      "json": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"]
      }
    }
  }
}
```

Nova supports `auto`, `any` and a specific named `tool` choice. Its v1 tool-definition page documents only `type`, `properties` and `required` at the top schema level, with top-level `type=object`. Resolve local Pydantic `$ref`/`$defs` before serialization, avoid unsupported top-level metadata such as `additionalProperties`, and keep validation of the full original application schema locally. Unsupported/cyclic schemas should fail clearly rather than be silently broadened. AWS recommends temperature 0 for tool use. [Nova v1 tool definitions](https://docs.aws.amazon.com/nova/latest/userguide/tool-use-definition.html)

For planner/result output, define a synthetic result tool whose input schema is the expected output shape. On calls that have no operational tools, force it using `toolChoice={"tool":{"name":"submit_result"}}`. For an executor that may still need web/SQL tools, offer operational tools plus the synthetic result tool with `auto`; a forced final-result choice would prevent research. Intercept the result tool locally and validate its `input` as the structured response; it is not an external side effect. AWS documents named-tool choice for schema-shaped output. [Nova structured generation](https://docs.aws.amazon.com/nova/latest/userguide/concept-chapter-servicename.html), [ToolChoice API](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ToolChoice.html)

An AI tool request is `{toolUse:{toolUseId,name,input}}` in an assistant content block. Keep its `toolUseId` exactly and return it as `{toolResult:{toolUseId,content:[{text:...}],status:"success"|"error"}}` in a user turn. All requested tools need an outcome, including rejected/not-executed ones. Map application `ok` to `success`, and application errors or refused execution to `error`; explain refusal in the content. Do not invent a new tool ID when responding. [ToolUseBlock](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ToolUseBlock.html), [ToolResultBlock](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ToolResultBlock.html)

## Response mapping

Read `output.message.content` in order. Join text blocks for response text and map every `toolUse` block to the engine's ToolCall (ID, name, arguments). A synthetic result block becomes `structured`; malformed arguments or an invalid result schema must not be reported as an accepted result. Keep the actual `stopReason`. `tool_use` means local work is requested; `max_tokens` can mean an incomplete answer, and filter/guardrail/malformed-output reasons must not be treated as a normal completed answer. [Converse response fields](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html)

Map reported `usage.inputTokens`, `outputTokens`, `cacheReadInputTokens` and `cacheWriteInputTokens` to Usage. Leave missing usage unknown. These are token counts, not dollar amounts, and unknown after timeout is not zero spend. [TokenUsage](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_TokenUsage.html)

## Timeouts and retries

Botocore Config has independent `connect_timeout` and `read_timeout` in seconds. Those do not implement an entire request's wall-clock deadline. For this project's short bounded calls, use the configured 60/120-second stage policy rather than copying Nova's generic 60-minute recommendation; a caller can deliberately reject a slow call. Configure `retries={"mode":"standard","total_max_attempts":1}` when the application owns retries. A `total_max_attempts` value of 1 includes the initial attempt; `max_attempts=1` instead permits one additional retry. Do not mutate one shared client's timeout while parallel tasks are using it; build/cache clients per timeout. [Botocore Config](https://docs.aws.amazon.com/botocore/latest/reference/config.html)

The application policy is one bounded retry with jitter for transient network/timeouts, throttling/model-not-ready and 5xx; no retry for validation, missing resource, authentication or access errors. Map the AWS codes explicitly: AccessDenied 403, Validation 400, ResourceNotFound 404, Throttling/ModelNotReady 429, ModelTimeout 408, InternalServer 500, ServiceUnavailable 503. ModelError 424 exposes `originalStatusCode`; classify that rather than assuming every 424 is transient. Record each actual attempt once, preserve unknown timeout usage, and recheck cancellation/budget before the next attempt. [Converse errors](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html)

## Pricing: verification boundary

The official [Bedrock pricing page](https://aws.amazon.com/bedrock/pricing/) and [Nova pricing page](https://aws.amazon.com/nova/pricing/) were checked. Their Nova price tables are loaded interactively and were not present in the tool-readable page. A direct public AWS Price List regional JSON read was unavailable: shell network DNS was denied, and the browsing fetch could not open the regional JSON URL. Therefore **this note does not certify Sydney/APAC prices**.

Do not label the familiar US Nova Pro $0.80/$3.20 or Lite $0.06/$0.24 per million as verified Sydney rates. Resolve the selected source Region, inference profile and service tier in the AWS calculator/pricing UI or AWS Price List data, then date/version those rates in config. Until verified, any configured cost is an explicit estimate and live spend admission must use a conservative configured allowance rather than a zero/unknown price. This pricing lookup limitation does not block writing and offline-testing the adapter.

## Minimum offline acceptance

Use botocore Stubber or an injected Bedrock runtime client. Cover a plain text response; planner synthetic result; executor operational tool -> matching tool result -> synthetic final result; two tool calls in one turn; same-role merging with a nudge; compression without toolConfig; malformed/truncated output; 429 retry then success; access-denied without retry; timeout with unknown usage; concurrent task budget admission. Then run a separately enabled real Nova smoke once credentials and model access are confirmed. Offline tests cannot establish model access or measured inference cost.

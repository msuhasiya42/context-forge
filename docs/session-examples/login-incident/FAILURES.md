# Failure walkthrough — what Python and the LLM actually see

Read after the [main loop](README.md). These examples use current native-tool behavior;
private incident MCP failure adapters are not implemented. Error values and subsequent model
choices are scripted. Python error serialization and the full next-request formatting below
were generated with the existing `_error_content`, `_tool_message` and `converse_payload`.
No failed external call or paid model call was made to produce these examples.

## 1. A tool fails: Python receives a ToolResult

Say: “The model asked Python to search. The search failed. We represent failure as data,
then give that data back to the model with the original tool-call ID.”

This is the Python-side `ToolResult` dumped with Python field names (including defaults):

```json
{
  "status": "error",
  "rows": null,
  "text": null,
  "error": "Search provider timed out.",
  "error_class": "system",
  "retryable": true,
  "artifact_ids": []
}
```

`error_class: system` identifies a service/runtime failure. `retryable: true` means a later
attempt might work. It is not a guarantee and does not trigger an automatic executor retry.
The `rows`, `text` and artifact fields contain no successful evidence in this case.

The executor reduces this to four model-visible fields:

```json
{
  "status": "error",
  "error": "Search provider timed out.",
  "error_class": "system",
  "retryable": true
}
```

The Bedrock result message contains that JSON as a text string and marks the tool result as
an error. The matching ID tells the model which attempted operation failed:

```json
{
  "role": "user",
  "content": [
    {
      "toolResult": {
        "toolUseId": "search-001",
        "content": [
          {
            "text": "{\"status\": \"error\", \"error\": \"Search provider timed out.\", \"error_class\": \"system\", \"retryable\": true}"
          }
        ],
        "status": "error"
      }
    }
  ]
}
```

## 2. Python calls the LLM again with the failed exchange included

This is the complete illustrative next input, formatted by the real Bedrock adapter. The
schema is the current `TaskResult` schema, not the incident-specific result used in the main
teaching scenario. System wording and task text here are illustrative instructions.

```json
{
  "modelId": "openai.gpt-oss-120b-1:0",
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "text": "Find public evidence for the assigned research task."
        }
      ]
    },
    {
      "role": "assistant",
      "content": [
        {
          "toolUse": {
            "toolUseId": "search-001",
            "name": "web_search",
            "input": {
              "query": "auth-api login failure release notes"
            }
          }
        }
      ]
    },
    {
      "role": "user",
      "content": [
        {
          "toolResult": {
            "toolUseId": "search-001",
            "content": [
              {
                "text": "{\"status\": \"error\", \"error\": \"Search provider timed out.\", \"error_class\": \"system\", \"retryable\": true}"
              }
            ],
            "status": "error"
          }
        }
      ]
    }
  ],
  "inferenceConfig": {
    "maxTokens": 4000,
    "temperature": 0
  },
  "system": [
    {
      "text": "Use authorized tools for evidence. Inspect tool errors before choosing the next action. Retry only when useful and within the allowed budget. If evidence remains unavailable, return_result with explicit uncertainties; do not invent facts."
    }
  ],
  "toolConfig": {
    "tools": [
      {
        "toolSpec": {
          "name": "web_search",
          "description": "Search public web pages.",
          "inputSchema": {
            "json": {
              "type": "object",
              "properties": {
                "query": {
                  "type": "string"
                }
              },
              "required": [
                "query"
              ]
            }
          }
        }
      },
      {
        "toolSpec": {
          "name": "return_result",
          "description": "Return the final structured result after completing the task.",
          "inputSchema": {
            "json": {
              "properties": {
                "summary": {
                  "title": "Summary",
                  "type": "string"
                },
                "facts": {
                  "default": [],
                  "items": {
                    "additionalProperties": false,
                    "properties": {
                      "text": {
                        "title": "Text",
                        "type": "string"
                      },
                      "evidenceRefs": {
                        "items": {
                          "additionalProperties": false,
                          "description": "A pointer to evidence; Artifacts are immutable, so `artifact_id` names the exact version.",
                          "properties": {
                            "artifactId": {
                              "title": "Artifactid",
                              "type": "string"
                            },
                            "locator": {
                              "additionalProperties": false,
                              "description": "Position of the evidence inside an Artifact.",
                              "properties": {
                                "kind": {
                                  "enum": [
                                    "page",
                                    "row",
                                    "cell",
                                    "range",
                                    "message",
                                    "section",
                                    "url"
                                  ],
                                  "title": "LocatorKind",
                                  "type": "string"
                                },
                                "value": {
                                  "title": "Value",
                                  "type": "string"
                                }
                              },
                              "required": [
                                "kind",
                                "value"
                              ],
                              "title": "Locator",
                              "type": "object"
                            },
                            "excerpt": {
                              "anyOf": [
                                {
                                  "type": "string"
                                },
                                {
                                  "type": "null"
                                }
                              ],
                              "default": null,
                              "title": "Excerpt"
                            }
                          },
                          "required": [
                            "artifactId",
                            "locator"
                          ],
                          "title": "EvidenceRef",
                          "type": "object"
                        },
                        "title": "Evidencerefs",
                        "type": "array"
                      }
                    },
                    "required": [
                      "text",
                      "evidenceRefs"
                    ],
                    "title": "Fact",
                    "type": "object"
                  },
                  "maxItems": 6,
                  "title": "Facts",
                  "type": "array"
                },
                "conclusions": {
                  "default": [],
                  "items": {
                    "additionalProperties": false,
                    "description": "A judgement, comparison or recommendation drawn from the result's facts (F10 ruling 1).\nNo source states it, so it is never a fact and cites no evidence of its own:\n``supporting_facts`` are the positions, from 0, of the facts of this result it rests on.",
                    "properties": {
                      "text": {
                        "title": "Text",
                        "type": "string"
                      },
                      "supportingFacts": {
                        "default": [],
                        "items": {
                          "minimum": 0,
                          "type": "integer"
                        },
                        "title": "Supportingfacts",
                        "type": "array"
                      },
                      "confidence": {
                        "default": "low",
                        "enum": [
                          "high",
                          "medium",
                          "low"
                        ],
                        "title": "Confidence",
                        "type": "string"
                      }
                    },
                    "required": [
                      "text"
                    ],
                    "title": "Conclusion",
                    "type": "object"
                  },
                  "maxItems": 3,
                  "title": "Conclusions",
                  "type": "array"
                },
                "structuredData": {
                  "additionalProperties": true,
                  "default": {},
                  "title": "Structureddata",
                  "type": "object"
                },
                "evidenceRefs": {
                  "default": [],
                  "items": {
                    "additionalProperties": false,
                    "description": "A pointer to evidence; Artifacts are immutable, so `artifact_id` names the exact version.",
                    "properties": {
                      "artifactId": {
                        "title": "Artifactid",
                        "type": "string"
                      },
                      "locator": {
                        "additionalProperties": false,
                        "description": "Position of the evidence inside an Artifact.",
                        "properties": {
                          "kind": {
                            "enum": [
                              "page",
                              "row",
                              "cell",
                              "range",
                              "message",
                              "section",
                              "url"
                            ],
                            "title": "LocatorKind",
                            "type": "string"
                          },
                          "value": {
                            "title": "Value",
                            "type": "string"
                          }
                        },
                        "required": [
                          "kind",
                          "value"
                        ],
                        "title": "Locator",
                        "type": "object"
                      },
                      "excerpt": {
                        "anyOf": [
                          {
                            "type": "string"
                          },
                          {
                            "type": "null"
                          }
                        ],
                        "default": null,
                        "title": "Excerpt"
                      }
                    },
                    "required": [
                      "artifactId",
                      "locator"
                    ],
                    "title": "EvidenceRef",
                    "type": "object"
                  },
                  "title": "Evidencerefs",
                  "type": "array"
                },
                "artifactRefs": {
                  "default": [],
                  "items": {
                    "additionalProperties": false,
                    "properties": {
                      "artifactId": {
                        "title": "Artifactid",
                        "type": "string"
                      },
                      "role": {
                        "enum": [
                          "input",
                          "output"
                        ],
                        "title": "Role",
                        "type": "string"
                      }
                    },
                    "required": [
                      "artifactId",
                      "role"
                    ],
                    "title": "ArtifactRef",
                    "type": "object"
                  },
                  "title": "Artifactrefs",
                  "type": "array"
                },
                "uncertainties": {
                  "default": [],
                  "items": {
                    "type": "string"
                  },
                  "title": "Uncertainties",
                  "type": "array"
                },
                "coverage": {
                  "additionalProperties": true,
                  "default": {},
                  "title": "Coverage",
                  "type": "object"
                }
              },
              "required": [
                "summary"
              ],
              "type": "object"
            }
          }
        }
      }
    ]
  }
}
```

The model sees: task → its own search call → the error for that call. It does not read a
Python exception stack from nowhere. Python has deliberately placed the error in context.

One possible next assistant response is a retry. A new call ID identifies this new attempt:

```json
{
  "output": {
    "message": {
      "role": "assistant",
      "content": [
        {
          "toolUse": {
            "toolUseId": "search-002",
            "name": "web_search",
            "input": {
              "query": "auth-api login failure release notes"
            }
          }
        }
      ]
    }
  },
  "stopReason": "tool_use"
}
```

Python checks budget/allowlist again and invokes the tool. Failed calls are not cached as
successful results, so the same name/arguments can actually run again. If the next call
succeeds, Python returns a success `toolResult` under `search-002`; the loop continues with
evidence. If it fails again, another error enters history. The shown model choice is possible,
not guaranteed: it may change the query, use another allowed tool, or finish with a gap.

## 3. The requested tool does not exist or is not allowed

Imagine a current web task asks for `query_artifact`, which is not registered. `_invoke_tool`
returns this model-visible error (the allowed names depend on the task and registry):

```json
{
  "status": "error",
  "error": "unknown tool query_artifact; this task may call: web_search, fetch_url",
  "error_class": "code",
  "retryable": false
}
```

Say: “Retrying this exact name cannot install the tool. Python tells the model what is
allowed. The model must choose an applicable existing tool or return a limitation.”

A typo such as `web_serch` can be corrected to `web_search` with a new call ID. But public
web search cannot substitute for private metrics or an inaccessible saved artifact. In that
case the task must report missing capability, and a developer/configuration change is needed.
There is no automatic tool installation or permission expansion.

## 4. The tool exists, but its arguments are invalid

For `web_search` called with a non-string query, the native adapter returns:

```json
{
  "status": "error",
  "error": "query must be text",
  "error_class": "code",
  "retryable": false
}
```

A useful next assistant response corrects the arguments instead of repeating the same error:

```json
{
  "output": {
    "message": {
      "role": "assistant",
      "content": [
        {
          "toolUse": {
            "toolUseId": "search-fixed-001",
            "name": "web_search",
            "input": {
              "query": "auth-api release notes"
            }
          }
        }
      ]
    }
  },
  "stopReason": "tool_use"
}
```

Here `retryable: false` means the unchanged request is not expected to recover on its own;
it does not forbid a corrected request. Current code carries the hint to the model but does
not enforce a universal retry policy based on that flag. Backend permissions and budgets
still control execution.

## 5. The runtime refuses another call because the budget is spent

The request-wide web budget refusal is:

```json
{
  "status": "not_executed",
  "error": "Request web-tool budget exhausted; return the evidence gathered so far.",
  "error_class": null,
  "retryable": false
}
```

No external call is made for that refusal. Bedrock represents it as `toolResult.status:
error`, while the JSON text preserves `status: not_executed`. This distinction tells us it
was refused before execution, not an operation that ran and failed.

The runtime also limits task tool calls and no-progress turns. When a stop condition is
reached, it restricts the next model opportunity to returning a result. A tool-only reply
then ends the attempt with an error; an accepted result carries the stopping limitation.

An illustrative valid current `TaskResult` can explicitly return no supported findings:

```json
{
  "output": {
    "message": {
      "role": "assistant",
      "content": [
        {
          "toolUse": {
            "toolUseId": "partial-result-001",
            "name": "return_result",
            "input": {
              "summary": "Could not obtain the required evidence.",
              "facts": [],
              "conclusions": [],
              "structuredData": {},
              "evidenceRefs": [],
              "artifactRefs": [],
              "uncertainties": [
                "Search remained unavailable; no supported incident conclusion can be drawn."
              ],
              "coverage": {}
            }
          }
        }
      ]
    }
  },
  "stopReason": "tool_use"
}
```

A validated result ends the task loop; it does not mean the research objective was met.
The downstream report must preserve missing coverage and avoid unsupported conclusions.

## 6. Separate the three retry layers

| Layer | Current behavior |
|---|---|
| Inside web search | Native search tries its primary provider twice with a configured delay, then an available fallback. Serper fallback needs its key. These HTTP attempts can happen before the model sees an error. |
| Inside the model/tool loop | The model can request another tool call after an error; normal tool-call budgets apply. The executor does not automatically apply exponential backoff based on `retryable`. |
| Entire task attempt | `_run_model_tool_loop` can restart a failed attempt from fresh task state, up to `task_retries + 1` attempts. This is broader than retrying one tool. |

Configured defaults currently include one task retry (two attempts) and three no-progress
turns. Effective tool limits can be overridden by task/settings. A completed result does
not trigger a whole-task retry simply because it contains uncertainties. Failed native
search/fetch invocations count against the shared web budget; internal HTTP retries are not
separate tool invocations. Successful repeated calls can be replayed within the task loop.

Do not teach “all failures retry three times.” The layer, exception mapping and settings
matter. Model-provider failures have separate routing/retry behavior; they are not ordinary
tool error messages covered by this example.

## 7. Important limits of today's error handling

- Generic exceptions from `tool.run` are converted to `system` errors with `retryable: true`.
  That broad classification does not prove an exception is transient. Native web adapters
  classify refusals separately; future MCP adapters need explicit auth/rate-limit/timeout mapping.
- A permission error should lead to an access/configuration resolution or reported gap, not
  repeated guesses. We have not implemented a full private-MCP authentication recovery flow.
- Errors during result shaping or blob storage occur outside the `tool.run` exception guard.
  Do not claim every artifact-upload failure already becomes a neat model-visible ToolResult.
- Jira writes need stricter handling: a timeout can mean “created, response lost.” Do not
  blindly retry the create. Reconcile a durable action identity first. This write workflow
  remains proposed and must not inherit read-tool retry behavior without review.
- The model's recovery choice is probabilistic. Python enforces limits; an error prompt is
  not an enforcement boundary. Exhausted task attempts end in FAILED with a failure reason.

## Code to show

All paths below are under `src/contextforge/`:

- `worker/stages/execute.py`: `_invoke_tool`, `_execute_tool_call`, `_error_content`,
  `_result_message`, `_stop_reason`, `_run_model_tool_loop`.
- `core/tools/base.py`: `ToolResult` fields and defaults.
- `infra/bedrock.py`: `_result_status`, tool-message conversion.
- `infra/web.py`: `_failure`, `_search_plan` and `search`.

**One sentence to remember:** “Errors are new evidence for the next decision, but Python
controls whether another attempt is allowed.”

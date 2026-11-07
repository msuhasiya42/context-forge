# ContextForge research terminology

Shared terms for understanding a research request. Entity definitions also appear in
[the system design](docs/system-design.md#3-glossary).

## Language

**Request state**:
The accumulated goal, constraints, plan, findings and open questions for one research request.
_Avoid_: Working brief, brief.

**Task state**:
The changing conversation, tool results and progress of one task within a request.
_Avoid_: Calling both request state and task state simply “state” when their scope is unclear.

**Task context**:
The request information, sources and dependency evidence available to an individual task.
_Avoid_: Task run when referring to this information rather than execution itself.

**Model context**:
The instructions, messages, evidence and tool definitions assembled for an individual model call.
_Avoid_: Using this term for the entire request's accumulated state.

**Ready tasks**:
Planned tasks whose prerequisites have finished and which may be selected for execution.
_Avoid_: Wave as the primary name for these tasks.

**Task outcome**:
The final status, findings, conclusions and limitations returned by a task. A finished task
may have succeeded, failed or been skipped.
_Avoid_: Calling all finished tasks successful or completed.

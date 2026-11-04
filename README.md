# ContextForge

AI Research Agent with Context Engineering and Persistent Memory. A private assistant for spreadsheet analysis, PDF questions, and later Gmail/Drive research, built to learn context engineering, LangGraph, AWS, NoSQL modeling, MCP, observability and evaluations at very low cost.

Three documents hold everything:

1. [System design](docs/system-design.md): what the system is, fixed choices, vocabulary, architecture, memory and context-engineering design, decisions log, phases.
2. [Implementation plan](docs/implementation-plan.md): entities and fields, Message shapes, working brief, DynamoDB physical design, context flow and compaction, worked prompt examples, stage prompts, repository layout, phase 1 tasks.
3. [Progress](docs/progress.md): status, open decisions, phase checklist, session log. Read this first when resuming.

React, FastAPI/Python, LangGraph and LangSmith are fixed. DynamoDB and S3 hold application data; Bedrock is the first model provider behind an adapter. The Python scripts at the repository root are an earlier event-delivery prototype, not the application.

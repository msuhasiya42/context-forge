# ContextForge

AI Research Agent with Context Engineering and Persistent Memory.

Start with the [system design and implementation plan](docs/system-design-and-implementation-plan.md).

The project is in design/early prototype work. React and FastAPI/Python are fixed; LangGraph is the orchestration core and LangSmith is the selected observability system. AWS is the target for application data and storage, with Bedrock first for models.

- [Entity model: finalized fields, relationships and lifecycles](docs/entity-model.md)
- [Entity-model review history](docs/entity-model-handoff.md)
- [Glossary](GLOSSARY.md)
- [Decision history](docs/architecture-context-decisions.md)
- [Memory research](docs/research/memory-design-research.md)
- [Tool discovery research](docs/research/tool-discovery-research.md)
- [Agent components research](docs/research/agent-components-research.md)

The existing Python bridge/template scripts are an earlier limited AWS event-delivery prototype, not the complete implemented application.


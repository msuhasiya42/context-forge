"""Workflow stages as plain functions: plan, plan gate, execute, cite and report, with guards.

Every model call and repository write lives inside a stage; the workflow wraps each one as a
LangGraph task so its result is checkpointed.
"""

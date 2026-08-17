"""Surfaces: read-only views over the signal store (PRD 10).

A surface reads signals and draws them. It holds no business logic: rank,
tier, facts and context are decided by the core and arrive already decided
(SUR-2). Nothing here may import a pipeline stage.
"""

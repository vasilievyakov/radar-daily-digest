"""Theme config loading.

CFG-1: moving the pipeline to another domain is a config swap, so nothing here
may hardcode a vendor, a change type or a threshold. Stage code reads values
through this module and never reaches into the YAML directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from radar.adapters.base import SourceConfig


class ConfigError(ValueError):
    pass


class ThemeConfig:
    def __init__(self, data: dict[str, Any], path: Path | None = None) -> None:
        self.data = data
        self.path = path
        self._validate()

    @classmethod
    def load(cls, path: str | Path) -> ThemeConfig:
        path = Path(path)
        if not path.exists():
            raise ConfigError(f"theme config not found: {path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(data, path)

    def _validate(self) -> None:
        """Fail at load time rather than midway through a backfill."""
        for section in ("theme", "corpus", "sources"):
            if section not in self.data:
                raise ConfigError(f"missing section: {section}")
        if not self.vendors:
            raise ConfigError("corpus.vendors is empty; retrieval filters need it")
        if not self.change_types:
            raise ConfigError("corpus.change_types is empty")
        seen: set[str] = set()
        for source in self.data["sources"]:
            sid = source.get("id")
            if not sid:
                raise ConfigError(f"source without id: {source}")
            if sid in seen:
                raise ConfigError(f"duplicate source id: {sid}")
            seen.add(sid)
            if not source.get("type"):
                raise ConfigError(f"source {sid} has no type")

    # theme

    @property
    def name(self) -> str:
        return self.data["theme"].get("name", "")

    @property
    def description(self) -> str:
        return self.data["theme"].get("description", "")

    @property
    def relevance_criteria(self) -> str:
        return self.data["theme"].get("relevance_criteria", "")

    @property
    def exclusion_criteria(self) -> str:
        return self.data["theme"].get("exclusion_criteria", "")

    # corpus

    @property
    def vendors(self) -> list[dict[str, Any]]:
        return self.data.get("corpus", {}).get("vendors", [])

    @property
    def change_types(self) -> list[dict[str, Any]]:
        return self.data.get("corpus", {}).get("change_types", [])

    @property
    def vendor_ids(self) -> list[str]:
        return [v["id"] for v in self.vendors]

    @property
    def change_type_ids(self) -> list[str]:
        return [c["id"] for c in self.change_types]

    @property
    def readiness(self) -> dict[str, Any]:
        return self.data.get("corpus", {}).get("readiness", {})

    # sources

    @property
    def sources(self) -> list[SourceConfig]:
        return [SourceConfig.from_dict(s) for s in self.data.get("sources", [])]

    def enabled_sources(self, type_filter: str | None = None) -> list[SourceConfig]:
        return [
            s
            for s in self.sources
            if s.enabled and (type_filter is None or s.type == type_filter)
        ]

    def backfillable_sources(self) -> list[SourceConfig]:
        return [s for s in self.enabled_sources() if s.backfill_supported]

    def source(self, source_id: str) -> SourceConfig | None:
        return next((s for s in self.sources if s.id == source_id), None)

    def source_priority(self, source_id: str) -> int:
        source = self.source(source_id)
        return source.priority if source else 5

    # sections used by stages

    def section(self, name: str) -> dict[str, Any]:
        return self.data.get(name, {}) or {}

    @property
    def retrieval(self) -> dict[str, Any]:
        return self.section("retrieval")

    @property
    def trends(self) -> dict[str, Any]:
        return self.section("trends")

    @property
    def scoring(self) -> dict[str, Any]:
        return self.section("scoring")

    @property
    def enrichment(self) -> dict[str, Any]:
        return self.section("enrichment")

    @property
    def delivery(self) -> dict[str, Any]:
        return self.section("delivery")

    @property
    def collection(self) -> dict[str, Any]:
        return self.section("collection")

    @property
    def models(self) -> dict[str, str]:
        """Stage to model mapping. Kept in config so routing is not hardcoded."""
        return self.section("models")

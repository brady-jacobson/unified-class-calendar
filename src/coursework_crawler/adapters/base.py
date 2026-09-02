from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from ..models import CrawlResult, SourceConfig


class Adapter(ABC):
    @abstractmethod
    def crawl(self, page: Any, source: SourceConfig) -> CrawlResult:
        raise NotImplementedError


class AdapterRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, Callable[[], Adapter]] = {}

    def register(self, name: str, factory: Callable[[], Adapter]) -> None:
        self._factories[name] = factory

    def create(self, name: str) -> Adapter:
        try:
            return self._factories[name]()
        except KeyError as exc:
            raise KeyError(f"No adapter registered for {name!r}") from exc


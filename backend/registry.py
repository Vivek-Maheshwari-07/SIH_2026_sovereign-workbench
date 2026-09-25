"""
Model registry for Track A. Loads config/models.yaml once and caches it in
memory, and exposes typed lookups so nothing else in the backend needs to
parse the YAML or hard-code a model name (AGENTS.md rule 6).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

from backend.settings import settings
from shared.contracts import ModelInfo, TaskType

_REPO_ROOT = Path(__file__).resolve().parent.parent

_REQUIRED_KEYS = ("models", "task_to_model", "rules", "similarity_threshold", "examples", "default_task_type")


class RegistryError(Exception):
    """Raised when config/models.yaml is missing, malformed, or missing a required field."""


def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else _REPO_ROOT / path


def _load_raw() -> dict[str, Any]:
    path = _resolve(settings.WB_MODELS_FILE)
    if not path.exists():
        raise RegistryError(f"models registry file not found: {path}")

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RegistryError(f"models registry file is not valid YAML: {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise RegistryError(f"models registry file is empty or not a mapping: {path}")

    missing = [key for key in _REQUIRED_KEYS if key not in data]
    if missing:
        raise RegistryError(f"models registry file {path} is missing required key(s): {missing}")

    return data


class Registry:
    """Loads config/models.yaml lazily on first use, then serves it from memory."""

    def __init__(self) -> None:
        self._data: Optional[dict[str, Any]] = None

    def reload(self) -> None:
        """Force config/models.yaml to be re-read on the next lookup."""
        self._data = None

    def _get(self) -> dict[str, Any]:
        if self._data is None:
            self._data = _load_raw()
        return self._data

    def all_models(self) -> list[ModelInfo]:
        data = self._get()
        models: list[ModelInfo] = []
        for entry in data["models"]:
            for required_field in ("id", "ollama_name"):
                if required_field not in entry:
                    raise RegistryError(f"model entry is missing '{required_field}': {entry}")
            models.append(
                ModelInfo(
                    id=entry["id"],
                    ollama_name=entry["ollama_name"],
                    tasks=[TaskType(t) for t in entry.get("tasks", [])],
                    supports_tools=bool(entry.get("supports_tools", False)),
                    supports_vision=bool(entry.get("supports_vision", False)),
                )
            )
        return models

    def model_by_id(self, model_id: str) -> ModelInfo:
        for model in self.all_models():
            if model.id == model_id:
                return model
        raise RegistryError(f"no model with id '{model_id}' in models registry")

    def model_for_task(self, task_type: TaskType) -> ModelInfo:
        data = self._get()
        task_to_model: dict[str, str] = data["task_to_model"]
        key = task_type.value if isinstance(task_type, TaskType) else str(task_type)
        model_id = task_to_model.get(key)
        if model_id is None:
            raise RegistryError(f"no task_to_model mapping for task type '{key}'")
        return self.model_by_id(model_id)

    def embedding_model(self) -> ModelInfo:
        data = self._get()
        for entry in data["models"]:
            if entry.get("embedding"):
                return self.model_by_id(entry["id"])
        raise RegistryError("no embedding model configured (expected one model entry with embedding: true)")

    def rules(self) -> list[dict[str, Any]]:
        return list(self._get()["rules"])

    def examples(self) -> dict[str, list[str]]:
        return {k: list(v) for k, v in self._get()["examples"].items()}

    def similarity_threshold(self) -> float:
        return float(self._get()["similarity_threshold"])

    def default_task_type(self) -> TaskType:
        return TaskType(self._get()["default_task_type"])


registry = Registry()

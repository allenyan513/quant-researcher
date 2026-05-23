"""Load a Claude/user-authored strategy from a .py file (`--strategy-file`).

The vision (features.md §H) is "Claude 编写策略规格 → 在回测引擎运行", so beyond
the built-in registry we let a backtest point at an arbitrary module that
defines a `BaseStrategy` subclass. This executes the file's top-level code —
acceptable because `qr` runs locally on the user's own machine (same trust
model as running any local script); we do NOT sandbox it.

Resolution: import the file, then find `BaseStrategy` subclasses *defined in
that module* (imported ones are ignored). Exactly one → use it. Several →
require `class_name` to disambiguate. None → error.
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

from quant_researcher.engine.strategy.base import BaseStrategy


def load_strategy_from_file(
    path: str | Path, *, class_name: str | None = None
) -> type[BaseStrategy]:
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"strategy file not found: {file_path}")

    spec = importlib.util.spec_from_file_location(f"qr_strategy_{file_path.stem}", file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load strategy module from {file_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    candidates = [
        obj
        for _name, obj in inspect.getmembers(module, inspect.isclass)
        if issubclass(obj, BaseStrategy)
        and obj is not BaseStrategy
        and obj.__module__ == module.__name__
    ]
    if class_name is not None:
        for obj in candidates:
            if obj.__name__ == class_name:
                return obj
        found = ", ".join(c.__name__ for c in candidates) or "none"
        raise KeyError(f"class '{class_name}' not found in {file_path} (found: {found})")

    if not candidates:
        raise KeyError(f"no BaseStrategy subclass defined in {file_path}")
    if len(candidates) > 1:
        names = ", ".join(c.__name__ for c in candidates)
        raise KeyError(
            f"multiple strategies in {file_path} ({names}); pass class_name to pick one"
        )
    return candidates[0]

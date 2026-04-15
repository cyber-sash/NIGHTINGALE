"""Snapshot persistence: one JSON file per day under data/snapshots/."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def snapshot_path(root: Path, date: str) -> Path:
    return root / "data" / "snapshots" / f"{date}.json"


def save_snapshot(root: Path, date: str, payload: dict[str, Any]) -> Path:
    path = snapshot_path(root, date)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_snapshot(root: Path, date: str) -> dict[str, Any] | None:
    path = snapshot_path(root, date)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_previous(root: Path, date: str, max_lookback_days: int = 14) -> dict[str, Any] | None:
    """Walk backwards up to `max_lookback_days` to find the most recent
    prior snapshot. Skips days we never ran."""
    d = datetime.strptime(date, "%Y-%m-%d").date()
    for i in range(1, max_lookback_days + 1):
        candidate = (d - timedelta(days=i)).isoformat()
        prev = load_snapshot(root, candidate)
        if prev is not None:
            return prev
    return None

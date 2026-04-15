"""CLI entry: `python -m nightingale.cli --date 2026-04-15`.

Run order for a daily cron job:

    cd /srv/nightingale
    python -m nightingale.cli            # today
    python -m nightingale.cli --date 2026-04-15
"""

from __future__ import annotations

import argparse
import json
from datetime import date as date_cls
from pathlib import Path

from .collect import COLLECTORS
from .report import render
from .storage import load_previous, save_snapshot


def _load_site_config(root: Path) -> dict:
    cfg_path = root / "config" / "sites.json"
    return json.loads(cfg_path.read_text(encoding="utf-8"))


def _collect_all(root: Path, run_date: str) -> dict:
    cfg = _load_site_config(root)
    cache_dir = root / "data" / "raw"
    sites_out: dict = {}
    for site_key, site_cfg in cfg.get("sites", {}).items():
        collector_cls = COLLECTORS.get(site_key)
        if collector_cls is None:
            continue
        collector = collector_cls(site_cfg, cache_dir=cache_dir)
        record = collector.collect(run_date)
        sites_out[site_key] = record.to_dict()
    return {"date": run_date, "sites": sites_out}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nightingale")
    parser.add_argument(
        "--date",
        default=date_cls.today().isoformat(),
        help="Snapshot date (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Repo root (defaults to cwd).",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Skip collection; re-render the report from an existing snapshot.",
    )
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    if args.report_only:
        snap_path = root / "data" / "snapshots" / f"{args.date}.json"
        if not snap_path.exists():
            print(f"No snapshot at {snap_path}; run without --report-only first.")
            return 2
        snapshot = json.loads(snap_path.read_text(encoding="utf-8"))
    else:
        snapshot = _collect_all(root, args.date)
        save_snapshot(root, args.date, snapshot)
        print(f"snapshot written: data/snapshots/{args.date}.json")

    previous = load_previous(root, args.date)
    md = render(args.date, snapshot, previous)
    report_path = root / "reports" / f"{args.date}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(md, encoding="utf-8")
    print(f"report written:   reports/{args.date}.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

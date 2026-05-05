"""Multi-day trend analysis across stored snapshots.

Consumes data/snapshots/*.json files and produces a structured trend
report: day-over-day deltas, directional streaks, category shifts,
and data-quality flags.
"""

from __future__ import annotations

import json
from datetime import date as date_cls, timedelta
from pathlib import Path
from typing import Any

from .taxonomy import CANONICAL_CATEGORIES

SITES_ORDER = ("toysreloved", "stuffle", "sellpy", "tildi")


def analyze(root: Path, end_date: str, days: int = 7) -> dict[str, Any]:
    """Return a trend report dict spanning `days` days ending on `end_date`."""
    end = date_cls.fromisoformat(end_date)
    dates = [(end - timedelta(days=i)).isoformat() for i in range(days)]
    dates.reverse()  # oldest → newest

    snapshots: list[dict[str, Any]] = []
    for d in dates:
        path = root / "data" / "snapshots" / f"{d}.json"
        if path.exists():
            snapshots.append(json.loads(path.read_text(encoding="utf-8")))

    if not snapshots:
        return {"error": "no snapshots found", "dates_checked": dates}

    return {
        "period": {"start": dates[0], "end": dates[-1], "days": days},
        "snapshots_found": len(snapshots),
        "dates_with_data": [s["date"] for s in snapshots],
        "sites": {site: _site_trends(site, snapshots) for site in SITES_ORDER},
        "category_shifts": _category_shifts(snapshots),
        "alerts": _alerts(snapshots),
    }


def _site_trends(site: str, snapshots: list[dict]) -> dict[str, Any]:
    """Per-site time series and derived metrics."""
    series: list[dict[str, Any]] = []
    for snap in snapshots:
        rec = snap.get("sites", {}).get(site, {})
        series.append({
            "date": snap["date"],
            "status": rec.get("status", "missing"),
            "total": rec.get("total_listings"),
            "categories": rec.get("categories", {}),
        })

    totals = [s["total"] for s in series if isinstance(s["total"], int)]
    statuses = [s["status"] for s in series]

    dod_deltas: list[dict[str, Any]] = []
    for i in range(1, len(series)):
        prev_t, curr_t = series[i - 1]["total"], series[i]["total"]
        if isinstance(prev_t, int) and isinstance(curr_t, int) and prev_t > 0:
            delta = curr_t - prev_t
            pct = delta / prev_t * 100.0
            dod_deltas.append({
                "date": series[i]["date"],
                "prev": prev_t,
                "curr": curr_t,
                "delta": delta,
                "pct": round(pct, 2),
            })

    streak = _directional_streak(dod_deltas)
    wow = _week_over_week(totals, series) if len(totals) >= 2 else None

    return {
        "latest_status": statuses[-1] if statuses else "missing",
        "latest_total": totals[-1] if totals else None,
        "min_total": min(totals) if totals else None,
        "max_total": max(totals) if totals else None,
        "day_over_day": dod_deltas,
        "streak": streak,
        "week_over_week": wow,
        "ok_days": statuses.count("ok"),
        "blocked_days": statuses.count("blocked"),
        "error_days": statuses.count("error"),
        "missing_days": statuses.count("missing"),
    }


def _directional_streak(deltas: list[dict]) -> dict[str, Any] | None:
    """Detect sustained growth or decline (3+ consecutive same-sign days)."""
    if len(deltas) < 2:
        return None
    direction = 0
    count = 0
    for d in reversed(deltas):
        sign = 1 if d["delta"] > 0 else (-1 if d["delta"] < 0 else 0)
        if sign == 0:
            break
        if direction == 0:
            direction = sign
            count = 1
        elif sign == direction:
            count += 1
        else:
            break
    if count >= 2:
        label = "growing" if direction > 0 else "declining"
        return {"direction": label, "consecutive_days": count}
    return None


def _week_over_week(totals: list[int], series: list[dict]) -> dict[str, Any] | None:
    """Compare the last available total to the earliest available total."""
    if len(totals) < 2:
        return None
    first, last = totals[0], totals[-1]
    if first <= 0:
        return None
    delta = last - first
    pct = delta / first * 100.0
    return {"first": first, "last": last, "delta": delta, "pct": round(pct, 2)}


def _category_shifts(snapshots: list[dict]) -> list[dict[str, Any]]:
    """Find the biggest category movements across all sites over the period."""
    if len(snapshots) < 2:
        return []
    first, last = snapshots[0], snapshots[-1]
    shifts: list[dict[str, Any]] = []

    for site in SITES_ORDER:
        first_cats = first.get("sites", {}).get(site, {}).get("categories", {})
        last_cats = last.get("sites", {}).get(site, {}).get("categories", {})
        if not first_cats and not last_cats:
            continue
        all_cats = set(first_cats) | set(last_cats)
        for cat in all_cats:
            f = first_cats.get(cat, 0)
            l = last_cats.get(cat, 0)
            if not isinstance(f, int) or not isinstance(l, int):
                continue
            delta = l - f
            if delta == 0:
                continue
            pct = (delta / f * 100.0) if f > 0 else None
            shifts.append({
                "site": site,
                "category": cat,
                "start": f,
                "end": l,
                "delta": delta,
                "pct": round(pct, 2) if pct is not None else None,
            })

    shifts.sort(key=lambda x: abs(x["delta"]), reverse=True)
    return shifts[:20]


def _alerts(snapshots: list[dict]) -> list[dict[str, Any]]:
    """Generate actionable alerts from the snapshot series."""
    alerts: list[dict[str, Any]] = []

    for site in SITES_ORDER:
        statuses = []
        for snap in snapshots:
            rec = snap.get("sites", {}).get(site, {})
            statuses.append((snap["date"], rec.get("status", "missing")))

        # Consecutive blocked days
        blocked_streak = 0
        for _, status in reversed(statuses):
            if status == "blocked":
                blocked_streak += 1
            else:
                break
        if blocked_streak >= 3:
            alerts.append({
                "severity": "high",
                "site": site,
                "type": "consecutive_blocked",
                "message": f"{site} has been BLOCKED for {blocked_streak} consecutive days. URL or anti-bot config needs attention.",
                "days": blocked_streak,
            })

        # Big single-day swing (>5%)
        for i in range(1, len(snapshots)):
            prev_rec = snapshots[i - 1].get("sites", {}).get(site, {})
            curr_rec = snapshots[i].get("sites", {}).get(site, {})
            prev_t = prev_rec.get("total_listings")
            curr_t = curr_rec.get("total_listings")
            if isinstance(prev_t, int) and isinstance(curr_t, int) and prev_t > 0:
                pct = (curr_t - prev_t) / prev_t * 100.0
                if abs(pct) >= 5.0:
                    direction = "surged" if pct > 0 else "dropped"
                    alerts.append({
                        "severity": "medium",
                        "site": site,
                        "type": "big_swing",
                        "message": f"{site} inventory {direction} {abs(pct):.1f}% on {snapshots[i]['date']} ({prev_t:,} -> {curr_t:,})",
                        "date": snapshots[i]["date"],
                        "pct": round(pct, 2),
                    })

        # Status transition (newly blocked or newly unblocked)
        if len(statuses) >= 2:
            prev_status = statuses[-2][1]
            curr_status = statuses[-1][1]
            if prev_status == "ok" and curr_status == "blocked":
                alerts.append({
                    "severity": "high",
                    "site": site,
                    "type": "newly_blocked",
                    "message": f"{site} was OK yesterday but is now BLOCKED. Check config/sites.json URLs.",
                    "date": statuses[-1][0],
                })
            elif prev_status == "blocked" and curr_status == "ok":
                alerts.append({
                    "severity": "info",
                    "site": site,
                    "type": "newly_unblocked",
                    "message": f"{site} is back online after being BLOCKED.",
                    "date": statuses[-1][0],
                })

    return alerts


def format_text(report: dict[str, Any]) -> str:
    """Render the trend report as human-readable text for CLI output."""
    if "error" in report:
        return f"Trend analysis error: {report['error']}\nDates checked: {', '.join(report.get('dates_checked', []))}"

    lines: list[str] = []
    period = report["period"]
    lines.append(f"=== Trend Analysis: {period['start']} to {period['end']} ({report['snapshots_found']} snapshots) ===\n")

    # Per-site summaries
    for site, data in report["sites"].items():
        lines.append(f"--- {site} ---")
        lines.append(f"  Latest status: {data['latest_status']}")
        lines.append(f"  Latest total:  {_fmt(data['latest_total'])}")
        lines.append(f"  Range:         {_fmt(data['min_total'])} - {_fmt(data['max_total'])}")
        lines.append(f"  Data quality:  {data['ok_days']} OK, {data['blocked_days']} blocked, {data['error_days']} error, {data['missing_days']} missing")

        if data.get("week_over_week"):
            wow = data["week_over_week"]
            lines.append(f"  Period change:  {_fmt(wow['first'])} -> {_fmt(wow['last'])} ({wow['pct']:+.1f}%)")

        if data.get("streak"):
            s = data["streak"]
            lines.append(f"  Streak:        {s['direction']} for {s['consecutive_days']} consecutive days")

        if data.get("day_over_day"):
            lines.append("  Day-over-day:")
            for dod in data["day_over_day"]:
                lines.append(f"    {dod['date']}: {_fmt(dod['prev'])} -> {_fmt(dod['curr'])} ({dod['pct']:+.1f}%)")
        lines.append("")

    # Category shifts
    shifts = report.get("category_shifts", [])
    if shifts:
        lines.append("--- Top category shifts (period start vs end) ---")
        for s in shifts[:10]:
            pct_str = f" ({s['pct']:+.1f}%)" if s["pct"] is not None else ""
            lines.append(f"  {s['site']}: {s['category']}: {s['start']} -> {s['end']} ({s['delta']:+d}{pct_str})")
        lines.append("")

    # Alerts
    alerts = report.get("alerts", [])
    if alerts:
        lines.append("--- Alerts ---")
        for a in alerts:
            severity = a["severity"].upper()
            lines.append(f"  [{severity}] {a['message']}")
        lines.append("")

    return "\n".join(lines)


def _fmt(n: int | None) -> str:
    return f"{n:,}" if isinstance(n, int) else "—"

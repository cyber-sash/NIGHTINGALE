"""Canonical toy taxonomy.

Every site labels its toy subcategories differently (Spielzeug, Leksaker,
"Kids & Toys", etc.). Adapters map the site's local label to one of these
canonical buckets so daily snapshots compare like-for-like across platforms.
"""

from __future__ import annotations

CANONICAL_CATEGORIES: tuple[str, ...] = (
    "Action Figures & Collectibles",
    "Dolls & Soft Toys",
    "LEGO & Building Sets",
    "Puzzles & Board Games",
    "Educational & Learning",
    "Vehicles, RC & Models",
    "Arts & Crafts",
    "Baby & Toddler Toys",
    "Outdoor & Sports Toys",
    "Electronic & Tech Toys",
    "Role Play & Dress-Up",
    "Other / Uncategorized",
)


def normalize_category(raw: str, mapping: dict[str, str]) -> str:
    """Map a site-specific label to the canonical taxonomy.

    `mapping` comes from config/sites.json (per-site). Unknown labels fall
    through to "Other / Uncategorized" so the daily diff never silently
    drops volume.
    """
    if not raw:
        return "Other / Uncategorized"
    key = raw.strip().lower()
    for site_label, canonical in mapping.items():
        if site_label.lower() == key:
            return canonical
    # substring fallback — catches "Lego Technic" → "LEGO & Building Sets"
    for site_label, canonical in mapping.items():
        if site_label.lower() in key:
            return canonical
    return "Other / Uncategorized"

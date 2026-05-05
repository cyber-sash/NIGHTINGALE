You are producing the **Daily ToysReloved Competitor Intelligence Briefing**. This is a comprehensive snapshot of the secondhand-toy marketplace covering our own store (toysreloved.de) and three competitors (stuffle.com, sellpy.com, tildi.com).

Follow these steps in order. Do not skip any step.

---

## Step 1: Collect today's snapshot

Run the Nightingale snapshot harness to fetch fresh inventory data from all four platforms:

```bash
cd /home/user/NIGHTINGALE && python3 -m nightingale.cli --date "$(date -u +%Y-%m-%d)"
```

If this fails (e.g., network errors), proceed with the most recent existing snapshot data instead.

## Step 2: Read today's snapshot and recent history

Read today's snapshot JSON and the trend analysis:

```bash
cd /home/user/NIGHTINGALE && python3 -m nightingale.cli --trends --days 7
```

Also read the raw snapshot file for the full data:

```bash
cat data/snapshots/$(date -u +%Y-%m-%d).json
```

If today's snapshot doesn't exist yet, use the most recent one:

```bash
ls -t data/snapshots/*.json | head -1 | xargs cat
```

## Step 3: Search for market news

Search the web for relevant secondhand-toy and recommerce market news. Run these searches:

1. `"secondhand toys" OR "pre-owned toys" market 2026` — general market trends
2. `"Sellpy" OR "Stuffle" OR "Tildi" toys 2026` — competitor-specific news
3. `recommerce kids toys Europe trends` — European recommerce landscape
4. `"second hand" Spielzeug Deutschland Markt` — German market specifically

Extract the 5-8 most relevant headlines and developments.

## Step 4: Produce the briefing

Write the complete briefing as a single, well-structured output. Use this exact format:

---

# Daily Competitor Intelligence Briefing — [DATE]

**Prepared for:** ToysReloved.de team
**Data freshness:** [timestamp from snapshot or "using cached data from DATE"]

---

## Executive Summary

Write 3-4 sentences covering: our inventory position, biggest competitor movement, and one market signal worth watching. Be specific with numbers where available.

---

## Inventory Snapshot

### Run Status

For each of the 4 sites, report:
- Status (OK / BLOCKED / ERROR)
- Which URL succeeded (from diagnostics.winning_url if available)
- Total toy listings count

### Our Inventory (toysreloved.de)

Report from the snapshot data:
- **Total listings**: N
- **Top categories** (table): category name, count, share of total
- **Top brands** (table): brand name, count
- **Price distribution**: bucket breakdown (0-10, 10-25, 25-50, 50-100, 100+ EUR)
- **Condition mix**: new, like new, good, acceptable

If our data is BLOCKED, state that clearly and note what URL pattern failed.

### Competitor Inventory Comparison

Side-by-side table of all 4 sites:
| Metric | toysreloved.de | stuffle.com | sellpy.com | tildi.com |
|--------|---------------|-------------|------------|-----------|
| Status | ... | ... | ... | ... |
| Total listings | ... | ... | ... | ... |
| Top category | ... | ... | ... | ... |

### Category Breakdown (Canonical Taxonomy)

Show the full 12-category matrix from the report, with counts per site.

---

## 7-Day Trends

Using the trend analysis output, report:

### Inventory Movement
- Day-over-day and week-over-week changes for each site
- Which site grew/shrank the most
- Any sustained directional trends (3+ days in same direction)

### Category Shifts
- Which categories gained or lost the most inventory across competitors
- Any emerging patterns (e.g., "LEGO listings up 15% across all 3 competitors")

### Signals & Alerts
- Any site showing >5% single-day change
- Any site newly blocked or newly unblocked
- Data quality notes (consecutive blocked days, missing data)

---

## Market Intelligence

### Recent News & Developments
List the 5-8 most relevant items found in web searches:
- **[Source]**: Headline — one sentence summary and relevance to ToysReloved

### Competitive Landscape
Based on available data and news:
- What are competitors doing differently?
- Any new market entrants or exits?
- Pricing or category strategy shifts visible in the data?

---

## Recommended Actions

Based on today's data and trends, list 2-4 specific, actionable recommendations for the ToysReloved team. Focus on:
- Categories to expand or reduce
- Pricing opportunities
- Competitive responses
- Inventory gaps vs competitors

---

## Data Quality Notes

- List any sites that returned BLOCKED or ERROR
- Note the diagnostics (page title, platform, response size) for blocked sites
- Suggest specific URL or config changes to fix broken collectors
- Reference `config/sites.json` for URL adjustments

---

*Generated via `/daily-snapshot` — Nightingale Competitor Intelligence System*
*Snapshot data: `data/snapshots/[DATE].json` | Report: `reports/[DATE].md`*

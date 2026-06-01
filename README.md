# PoE2 Item Filters - auto-priced

Path of Exile 2 item filters with current white-base trade prices folded into the visual tier rules. Updated automatically every few hours.

## What to grab

The files at the **repo root** ending in `updated.filter` are the ones you want to download.

Do **not** use anything under [`internal/`](internal) — those are the un-priced source filters that the generator reads from.

## Installation

1. Download a `<name> updated.filter` from the file list above.
2. Drop it into `%userprofile%\Documents\My Games\Path of Exile 2\` (Windows) or the equivalent on your OS.
3. In-game: Escape → Options → UI → Item Filter → select it.

## How it works

A short generator script (`internal/update_filter.py`) reads [`latest.json`](latest.json) and prepends a managed block of `Show` rules at the top of each source filter, bucketing each tracked white base into a price tier:

| Tier | Median value            | Visual                                          |
|------|-------------------------|-------------------------------------------------|
| S    | ≥ 90% of a divine       | font 45, red border, red star on minimap, loud alert |
| A    | ≥ 20% of a divine       | font 45, red text on white bg, red circle       |
| B    | ≥ 8% of a divine        | font 40, white-on-dark-red, brown circle        |
| no-data | (currently no listings) | font 40, black box with white text          |
| (below B floor) | < 8% of a divine | no rule emitted - falls through to base filter |

Tier thresholds are percentages of a Divine Orb, converted to exalts at generation time using the live `divine` rate in [`latest.json`](latest.json), so the ladder tracks the divine price as the exalt/divine rate drifts during the league. The percentages may also change during the league.

## `latest.json` shape

```json
{
  "rates_to_exalt": { "exalted": 1.0, "chaos": 0.59, "divine": 53.57, "annul": 7.70 },
  "items": [
    { "base": "Ancestral Tiara", "min_ilvl": 82, "median_exalts": 28.78 },
    { "base": "Sekhema Sandals", "min_ilvl": 82, "median_exalts": 209.02 },
    { "base": "Gold Ring",       "min_ilvl": 80, "median_exalts": null  }
  ]
}
```

- `rates_to_exalt` - currency conversion rates used to normalise prices to exalts.
- `items` - the list of priced bases. Each entry has:
  - `base` - exact in-game base name.
  - `min_ilvl` - minimum item level the entry tracks. Same base can appear at multiple ilvls.
  - `median_exalts` - median exalt-equivalent value of the 10 cheapest current listings (across exalted / chaos / annul / divine), or `null` if the base has no listings.

The generator also still accepts the older top-level-array shape (just the `items` list) for backward compatibility.

Source data comes from the private [PoE-Base-Pricer](https://github.com/T1mothy-L/PoE-Base-Pricer) repo and is mirrored here by a GitHub Action.

## Refresh cadence

Every push to `latest.json` (auto-mirrored from the price tracker every few hours) triggers a regeneration of every `<name> updated.filter`. 

## Credits

- Base filter content: [NeverSink's Indepth Loot Filter for PoE2](https://github.com/NeverSinkDev/NeverSink-Filter-for-PoE2). The `updated.filter` files leave NeverSink's content untouched; only a small managed block is prepended.
- Pricing data: trade2 + [poe2scout](https://poe2scout.com).

## Disclaimer

Not affiliated with or endorsed by Grinding Gear Games.

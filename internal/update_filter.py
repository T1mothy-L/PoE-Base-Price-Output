"""
Price-driven filter rule generator with a small Tkinter UI.

Reads latest.json (produced by poe2_price_tracker.py), buckets each priced
white base by (min_ilvl, price_tier), and emits a managed block of Show
rules prepended to every *.filter file sitting next to this script.

Naming convention: for each source `Foo.filter`, output is written to
`Foo updated.filter` in the same directory. Sources are never modified.
Re-runs regenerate the derived file in place — the discovery step skips
any *.filter whose name already ends in " updated", so you can't end up
with `Foo updated updated.filter`. Delete a derived file to force a
fresh generation; delete a source filter to stop processing it.

Rules inserted at the top of the derived file win because PoE2 filters
are first-match-wins; the rest of the source content carries over as
fallback styling for bases not in latest.json.

Idempotent: on re-run, the block between '# BEGIN AUTO' and '# END AUTO'
is replaced. The previous '# BEGIN HIPNO_AUTO' / '# END HIPNO_AUTO' marker
pair is still recognised so derived files migrate cleanly on first run
after the rename. Headless mode (--headless) uses hardcoded
DEFAULT_THRESHOLDS and doesn't read filter_config.json -- it's the path
the GitHub Action takes.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Tkinter is optional: CI runners on Ubuntu without python3-tk should
# still be able to run --headless. With `from __future__ import
# annotations`, type hints referring to `tk.Tk` resolve as strings so
# the class definition doesn't need tkinter at module load time.
try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:  # pragma: no cover -- only hit on tk-less runners
    tk = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]
    messagebox = None  # type: ignore[assignment]


# Sources + script live in this directory; derived `<name> updated.filter`
# files and `latest.json` live one level up (the repo root that public
# viewers see). When the script is run from the original PoE-Base-Pricer
# working tree where everything sat at one level, point both at the same
# directory by editing these constants.
SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent
LATEST_PATH = REPO_ROOT / "latest.json"
CONFIG_PATH = SCRIPT_DIR / "filter_config.json"

BEGIN_MARKER = "# BEGIN AUTO"
END_MARKER = "# END AUTO"
# Older marker pair we still recognise for migration. If splice() finds
# these in the input and the current markers aren't present, it replaces
# the legacy block with the current marker pair — so a derived file that
# was generated under the old name self-heals on the next run.
LEGACY_MARKER_PAIRS = [("# BEGIN HIPNO_AUTO", "# END HIPNO_AUTO")]

# On a fresh source filter (no markers yet), the managed block is inserted
# right after the NeverSink block whose header contains this anchor — i.e.
# just below the "twice-corrupted magic" exotic-state rule. If the anchor
# isn't present in the file, the block is prepended at the very top instead.
ANCHOR = "twicecorruptedmagic !exotics_ctier"

# Source .filter files are anything in SCRIPT_DIR without this suffix on the stem.
# Derived files (the ones we write) get the suffix appended before the .filter
# extension. Sources are read but never written; derived are regenerated
# every run, so they can be deleted or moved without consequence.
DERIVED_SUFFIX = " updated"

TIER_ORDER = ["S", "A", "B"]

# Render order for buckets: S/A/B by price tier, then NO_DATA (no listings,
# still want to know about it). Entries priced below B's floor get no rule
# at all and fall through to the downstream filter rules.
RENDER_ORDER = ["S", "A", "B", "NO_DATA"]

# Tier thresholds are expressed as a percentage of a Divine Orb's value
# (S=90 means a base whose median is >= 90% of a divine). At generation time
# they're converted to absolute exalt floors using the divine->exalt rate
# carried in latest.json ("rates_to_exalt.divine"), so the ladder tracks the
# divine price through the league instead of drifting as exalt/divine moves.
DEFAULT_THRESHOLDS = {"S": 90.0, "A": 20.0, "B": 8.0}

# Visual styling per class. Colors and sound ids for S/A/B are lifted from
# existing high-tier blocks in Hipno T16 Base Farm.filter so the auto-section
# blends with the file's palette. NO_DATA is user-specified: size 40 black
# box with white text and PlayAlertSound 2 300.
TIER_STYLES = {
    "S": {
        "font_size": 45,
        "text_color": (255, 0, 0, 255),
        "border_color": (255, 0, 0, 255),
        "bg_color": (255, 255, 255, 255),
        "alert_sound": (6, 300),
        "play_effect": "Red",
        "minimap_icon": (0, "Red", "Star"),
    },
    "A": {
        "font_size": 45,
        "text_color": (255, 255, 255, 255),
        "border_color": (255, 255, 255, 255),
        "bg_color": (245, 105, 90, 255),
        "alert_sound": (1, 300),
        "play_effect": "Red",
        "minimap_icon": (0, "Red", "Circle"),
    },
    "B": {
        "font_size": 40,
        "text_color": (255, 255, 255, 255),
        "border_color": (255, 0, 0, 255),
        "bg_color": (60, 0, 0, 255),
        "alert_sound": (3, 200),
        "play_effect": "Red",
        "minimap_icon": (1, "Red", "Diamond"),
    },
    "NO_DATA": {
        "font_size": 40,
        "text_color": (255, 255, 255, 255),
        "bg_color": (0, 0, 0, 255),
        "alert_sound": (2, 300),
        # no border / effect / minimap icon — per spec, just a black box.
    },
}


# ============================================================
# Pure logic
# ============================================================

def load_latest(path: Path) -> tuple[list[dict], dict[str, float]]:
    """Read latest.json -> (items, rates_to_exalt).

    Two shapes are accepted:
      - current: an object ``{"rates_to_exalt": {...}, "items": [...]}`` where
        the priced bases live under "items" and currency rates (divine, chaos,
        ...) expressed in exalts live under "rates_to_exalt".
      - legacy: a top-level JSON array of entries, with no rate block — rates
        come back as {} and the divine-percentage ladder can't be resolved
        (see generate()).
    Null medians are kept and classified as NO_DATA."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        return data.get("items", []), data.get("rates_to_exalt", {})
    return data, {}


def classify(median: float | None, floors: dict[str, float]) -> str | None:
    """Map a median (in exalts) to one of S/A/B/NO_DATA, or None when below
    B's floor. `floors` are absolute exalt cutoffs (see resolve_floors()).

    - None median -> NO_DATA (no listings; user wants to see it as a black box).
    - median >= S/A/B floor -> that tier.
    - Otherwise (0 <= median < B floor) -> None, meaning no rule will be
      emitted and the drop falls through to the downstream filter rules.
    """
    if median is None:
        return "NO_DATA"
    for tier in TIER_ORDER:
        if median >= floors[tier]:
            return tier
    return None


def resolve_floors(thresholds: dict[str, float], divine_rate: float
                   ) -> dict[str, float]:
    """Convert percent-of-divine tier thresholds into absolute exalt floors.

    `thresholds` map each tier to a percentage of a Divine Orb (S=90 means 90%
    of a divine). `divine_rate` is the value of one divine in exalts
    (latest.json's rates_to_exalt["divine"]). The result is in the same unit
    as median_exalts, so classify() can compare directly."""
    return {tier: (pct / 100.0) * divine_rate for tier, pct in thresholds.items()}


def bucket(entries: list[dict], floors: dict[str, float]
           ) -> dict[tuple[int, str], list[str]]:
    """Bucket entries by (min_ilvl, class). `floors` are absolute exalt cutoffs
    (see resolve_floors()). Entries classified as None (below B's floor) are
    dropped so no rule is emitted for them. Bases sorted within a bucket for
    stable diffs."""
    buckets: dict[tuple[int, str], list[str]] = {}
    for e in entries:
        klass = classify(e.get("median_exalts"), floors)
        if klass is None:
            continue
        buckets.setdefault((e["min_ilvl"], klass), []).append(e["base"])
    for bases in buckets.values():
        bases.sort()
    return buckets


def _color(c: tuple) -> str:
    return " ".join(str(x) for x in c)


def render_block(ilvl: int, klass: str, bases: list[str]) -> str:
    """One Show block for a single (ilvl, class) bucket."""
    quoted = " ".join(f'"{b}"' for b in bases)
    plural = "s" if len(bases) != 1 else ""
    style = TIER_STYLES[klass]
    label = "no-listings" if klass == "NO_DATA" else f"tier {klass}"
    lines = [
        f"# {label}  ilvl>={ilvl}  ({len(bases)} base{plural})",
        "Show",
        f"BaseType == {quoted}",
        f"ItemLevel >= {ilvl}",
        "Rarity Normal",
        "Corrupted False",
        "Mirrored False",
        f"SetFontSize {style['font_size']}",
        f"SetTextColor {_color(style['text_color'])}",
    ]
    if "border_color" in style:
        lines.append(f"SetBorderColor {_color(style['border_color'])}")
    lines.append(f"SetBackgroundColor {_color(style['bg_color'])}")
    if "alert_sound" in style:
        sid, vol = style["alert_sound"]
        lines.append(f"PlayAlertSound {sid} {vol}")
    if "play_effect" in style:
        lines.append(f"PlayEffect {style['play_effect']}")
    if "minimap_icon" in style:
        size, color, shape = style["minimap_icon"]
        lines.append(f"MinimapIcon {size} {color} {shape}")
    return "\n".join(lines) + "\n"


def render_managed_block(buckets: dict[tuple[int, str], list[str]],
                         source_label: str) -> str:
    """Wrap all Show blocks in BEGIN/END markers."""
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    head = [
        f"{BEGIN_MARKER}  ============================================",
        "# Generated by update_filter.py -- do not edit between markers.",
        f"# Source: {source_label} @ {ts}",
        "# ============================================================",
    ]
    body: list[str] = []
    if not buckets:
        body.append("# (no priced bases in latest.json)")
    else:
        # ilvl desc, then S/A/B/NO_DATA/HIDE so more emphatic rules match first
        keys = sorted(buckets.keys(),
                      key=lambda k: (-k[0], RENDER_ORDER.index(k[1])))
        for ilvl, klass in keys:
            body.append("")
            body.append(render_block(ilvl, klass, buckets[(ilvl, klass)]).rstrip("\n"))
    tail = [END_MARKER]
    return "\n".join(head + body + tail) + "\n"


def splice(file_text: str, managed_block: str) -> str:
    """Insert managed_block into file_text, or replace the existing one in place.

    If the file already carries a marker pair (the current one or any
    LEGACY_MARKER_PAIRS), the block between the markers is replaced in place,
    so files generated under an older marker name migrate cleanly rather than
    ending up with two stacked managed blocks.

    On a fresh file with no markers (the normal path — source filters carry
    none), the block is inserted right after the ANCHOR block; if the anchor
    isn't found, it's prepended at the very top."""
    pairs_to_check = [(BEGIN_MARKER, END_MARKER), *LEGACY_MARKER_PAIRS]
    for begin, end in pairs_to_check:
        begin_idx = file_text.find(begin)
        end_idx = file_text.find(end)
        if begin_idx == -1 and end_idx == -1:
            continue  # try the next pair
        if begin_idx == -1 or end_idx == -1:
            raise ValueError(
                f"Found one '{begin}'/'{end}' marker but not the other "
                f"(BEGIN at {begin_idx}, END at {end_idx}). Refusing to "
                "write - fix the markers manually first."
            )
        if begin_idx > end_idx:
            raise ValueError(
                f"END '{end}' marker appears before BEGIN '{begin}' in "
                "target file. Refusing to write."
            )
        end_line_end = file_text.find("\n", end_idx)
        end_line_end = (end_line_end + 1) if end_line_end != -1 else len(file_text)
        return file_text[:begin_idx] + managed_block.rstrip("\n") + "\n" + file_text[end_line_end:]

    # No marker pair found in the file (the normal path — source filters
    # carry no markers). Insert the managed block right after the ANCHOR
    # block so our price rules sit just below it.
    anchor_idx = file_text.find(ANCHOR)
    if anchor_idx != -1:
        # Filter blocks are contiguous (no internal blank lines) and separated
        # by a blank line, so the first '\n\n' at/after the anchor marks the
        # gap between the anchor block and whatever follows. Insert there.
        gap = file_text.find("\n\n", anchor_idx)
        if gap != -1:
            block_end = gap + 1  # index of the '\n' terminating the last block line
            return (
                file_text[:block_end]
                + "\n" + managed_block.rstrip("\n") + "\n"
                + file_text[block_end:]
            )

    # Anchor missing (or it ran to EOF with no trailing blank) — prepend at
    # the top with a blank line separating from the original content.
    return managed_block.rstrip("\n") + "\n\n" + file_text


def validate_thresholds(thresholds: dict[str, float]) -> None:
    """Tier thresholds are percentages of a Divine Orb. Require strictly
    decreasing S > A > B and B > 0, so anything between 0 and B is
    unambiguously below the show ladder."""
    s, a, b = thresholds["S"], thresholds["A"], thresholds["B"]
    if b <= 0:
        raise ValueError(f"Tier B threshold (% of divine) must be > 0 (got {b}).")
    if not (s > a > b):
        raise ValueError(
            f"Tier thresholds (% of divine) must strictly decrease: "
            f"S({s}) > A({a}) > B({b})."
        )


# ============================================================
# I/O + config
# ============================================================

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError):
            cfg = {}
    else:
        cfg = {}
    thresholds = {**DEFAULT_THRESHOLDS, **(cfg.get("thresholds") or {})}
    return {"thresholds": thresholds}


def save_config(thresholds: dict[str, float]) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"thresholds": thresholds}, f, indent=2)


def find_source_filters() -> list[Path]:
    """Every *.filter in SCRIPT_DIR that isn't itself a derived 'updated' file."""
    return sorted(
        p for p in SCRIPT_DIR.glob("*.filter")
        if not p.stem.endswith(DERIVED_SUFFIX)
    )


def derived_path_for(source: Path) -> Path:
    """Map `<SCRIPT_DIR>/Foo.filter` -> `<REPO_ROOT>/Foo updated.filter`,
    so what external viewers see at the repo root are only the derived
    filters they should be subscribing to."""
    return REPO_ROOT / f"{source.stem}{DERIVED_SUFFIX}.filter"


def write_atomic(target: Path, content: str) -> None:
    """Write to a temp file in the same dir, then atomic-rename."""
    fd, tmp_path = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def generate(thresholds: dict[str, float]) -> dict:
    """Generate `<name> updated.filter` for every source `<name>.filter` in
    SCRIPT_DIR. Sources are never modified; their derived twin (written to
    REPO_ROOT) is rewritten from
    scratch each run, so the script is naturally idempotent and the
    'filter a updated updated' double-suffix can't happen."""
    validate_thresholds(thresholds)
    if not LATEST_PATH.exists():
        raise FileNotFoundError(
            f"{LATEST_PATH.name} not found — run poe2_price_tracker.py first."
        )
    sources = find_source_filters()
    if not sources:
        raise FileNotFoundError(
            f"No source *.filter found in {SCRIPT_DIR}. Drop your filter "
            "file(s) next to this script and re-run."
        )

    items, rates = load_latest(LATEST_PATH)
    divine_rate = rates.get("divine")
    if not divine_rate or divine_rate <= 0:
        raise ValueError(
            f"{LATEST_PATH.name} is missing a positive 'rates_to_exalt.divine' "
            "rate, which is needed to convert the percent-of-divine tier "
            "thresholds into prices. Re-run the price tracker to regenerate "
            "latest.json with currency rates."
        )
    floors = resolve_floors(thresholds, divine_rate)
    buckets = bucket(items, floors)
    block = render_managed_block(buckets, source_label=LATEST_PATH.name)

    written: list[str] = []
    for source in sources:
        with open(source, encoding="utf-8") as f:
            original = f.read()
        updated = splice(original, block)
        target = derived_path_for(source)
        write_atomic(target, updated)
        written.append(target.name)

    ilvls = sorted({k[0] for k in buckets}, reverse=True)
    return {
        "rule_count": len(buckets),
        "ilvl_bands": ilvls,
        "tracked_entries": len(items),
        "sources": [s.name for s in sources],
        "written": written,
    }


# ============================================================
# Tkinter UI
# ============================================================

class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("Hipno filter updater")
        root.geometry("520x500")
        root.minsize(460, 460)

        cfg = load_config()
        self.saved_thresholds: dict[str, float] = dict(cfg["thresholds"])
        self.entries: dict[str, tk.StringVar] = {}
        self.old_labels: dict[str, ttk.Label] = {}

        main = ttk.Frame(root, padding=12)
        main.pack(fill="both", expand=True)

        # Source-filter discovery
        ttk.Label(main, text=f"Sources: {SCRIPT_DIR}\nDerived: {REPO_ROOT}",
                  foreground="#444", wraplength=480).grid(
            row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(main, text="Sources detected (each gets a '<name> updated.filter'):").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(8, 2))
        ttk.Button(main, text="Rescan", command=self.refresh_sources).grid(
            row=1, column=2, sticky="e", pady=(8, 2))
        self.sources_list = tk.Listbox(main, height=4, activestyle="none")
        self.sources_list.grid(row=2, column=0, columnspan=3, sticky="we", pady=(0, 6))
        self.refresh_sources()

        # Tier ladder
        ttk.Separator(main).grid(row=3, column=0, columnspan=3, sticky="we", pady=(8, 8))
        ttk.Label(main, text="Price-tier thresholds (% of a Divine Orb)").grid(
            row=4, column=0, columnspan=3, sticky="w")
        ttk.Label(main, text="Each tier triggers when a base's median is >= that % of a "
                  "divine's value (converted to exalts using the divine rate in latest.json). "
                  "S > A > B > 0. Items priced below B get no rule (fall through to downstream "
                  "filter rules); items with no listings show as a black box.",
                  foreground="#666", wraplength=480).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(0, 6))

        for i, tier in enumerate(TIER_ORDER):
            row = 6 + i
            ttk.Label(main, text=tier, width=3, anchor="center").grid(
                row=row, column=0, sticky="w", pady=2)
            var = tk.StringVar(value=str(self.saved_thresholds[tier]))
            var.trace_add("write", lambda *_a, t=tier: self._update_old_label(t))
            entry = ttk.Entry(main, textvariable=var, width=10)
            entry.grid(row=row, column=1, sticky="w", pady=2)
            self.entries[tier] = var
            old = ttk.Label(main, text="", foreground="#888")
            old.grid(row=row, column=2, sticky="w", padx=(8, 0))
            self.old_labels[tier] = old

        # Status + Run
        self.status = tk.StringVar(value="Ready.")
        ttk.Separator(main).grid(row=11, column=0, columnspan=3, sticky="we", pady=(12, 8))
        ttk.Label(main, textvariable=self.status, foreground="#444",
                  wraplength=480).grid(row=12, column=0, columnspan=3, sticky="w")
        ttk.Button(main, text="Run", command=self.run).grid(
            row=13, column=0, columnspan=3, pady=(12, 0))

        main.columnconfigure(1, weight=1)

    def refresh_sources(self) -> None:
        self.sources_list.delete(0, tk.END)
        sources = find_source_filters()
        if sources:
            for s in sources:
                self.sources_list.insert(tk.END, s.name)
        else:
            self.sources_list.insert(tk.END, "(none — drop a .filter file here)")

    def _update_old_label(self, tier: str) -> None:
        try:
            current = float(self.entries[tier].get())
        except ValueError:
            self.old_labels[tier].config(text="(invalid)")
            return
        saved = self.saved_thresholds[tier]
        if current == saved:
            self.old_labels[tier].config(text="")
        else:
            self.old_labels[tier].config(text=f"was {saved:g}")

    def _parse_thresholds(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for tier in TIER_ORDER:
            raw = self.entries[tier].get().strip()
            try:
                out[tier] = float(raw)
            except ValueError:
                raise ValueError(f"Tier {tier} floor is not a number: {raw!r}")
        return out

    def run(self) -> None:
        try:
            thresholds = self._parse_thresholds()
            validate_thresholds(thresholds)
            summary = generate(thresholds)
        except Exception as e:
            messagebox.showerror("Update failed", str(e))
            self.status.set(f"Failed: {e}")
            return

        save_config(thresholds)
        self.saved_thresholds = dict(thresholds)
        for tier in TIER_ORDER:
            self._update_old_label(tier)
        self.refresh_sources()

        ilvls = summary["ilvl_bands"]
        ilvl_str = ", ".join(str(i) for i in ilvls) if ilvls else "none"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.status.set(
            f"Last run {ts}: wrote {len(summary['written'])} filter(s) "
            f"[{summary['rule_count']} rule(s) across {len(ilvls)} ilvl "
            f"band(s) ({ilvl_str}) from {summary['tracked_entries']} "
            f"tracked base(s)]"
        )


def main() -> int:
    if "--headless" in sys.argv[1:]:
        # CI / scripted use: hardcoded defaults, no filter_config.json
        # dependency. Tune by editing DEFAULT_THRESHOLDS at the top of
        # this file.
        try:
            summary = generate(DEFAULT_THRESHOLDS)
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        print(f"Wrote {len(summary['written'])} filter(s):")
        for name in summary['written']:
            print(f"  {name}")
        print(f"({summary['rule_count']} rule(s) across "
              f"{len(summary['ilvl_bands'])} ilvl band(s) "
              f"from {summary['tracked_entries']} tracked base(s))")
        return 0

    # UI path
    if tk is None:
        print(
            "ERROR: tkinter is not available. Install python3-tk "
            "(Linux: `sudo apt install python3-tk`; Windows: tkinter "
            "ships with the official Python installer), or re-run "
            "with --headless to skip the UI.",
            file=sys.stderr,
        )
        return 1
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

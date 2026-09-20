#!/usr/bin/env python3
"""
Emoji Kitchen watch
===================

Google publishes Emoji Kitchen stickers as static PNGs on gstatic.com but offers
no index, feed, or changelog. emojikitchen.dev rescans Google's servers and
commits the result as a 95 MB ``metadata.json``. This script turns that file
into a changelog:

1. Parse ``metadata.json`` into {unordered pair -> latest sticker revision}.
2. Compare against the previous snapshot in ``snapshot/``.
3. Write a newsletter (Markdown + HTML) listing new, removed and redrawn combos.
4. Overwrite the snapshot so the next run diffs against today.

Standard library only. Run:  python watch.py metadata.json
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import os
import sys
from pathlib import Path

PairKey = tuple[str, str]                 # sorted (codepointA, codepointB)
Combo = tuple[str, str, str]              # (batch date, left codepoint, right codepoint)

UPSTREAM = "https://github.com/xsalazar/emoji-kitchen-backend/commits/main/app/metadata.json"
GSTATIC = "https://www.gstatic.com/android/keyboard/emojikitchen/{date}/u{left}/u{left}_u{right}.png"


# --------------------------------------------------------------------------- helpers
def glyph(codepoint: str) -> str:
    """'1f642-200d-2195-fe0f' -> the actual emoji character(s)."""
    return "".join(chr(int(part, 16)) for part in codepoint.split("-"))


def sticker_url(date: str, left: str, right: str) -> str:
    # Google's file names prefix every codepoint component with 'u': 263a-fe0f -> u263a-ufe0f
    u = lambda cp: "-u".join(cp.split("-"))  # noqa: E731
    return GSTATIC.format(date=date, left=u(left), right=u(right))


def pair_key(a: str, b: str) -> PairKey:
    return tuple(sorted((a, b)))  # type: ignore[return-value]


def fmt(n: int) -> str:
    return f"{n:,}"


# --------------------------------------------------------------------------- load / save
def load_metadata(path: Path) -> tuple[dict[str, str], dict[PairKey, Combo]]:
    """Return ({emoji codepoint: name}, {pair: latest revision})."""
    with path.open(encoding="utf-8") as f:
        meta = json.load(f)

    names = {cp: entry["alt"] for cp, entry in meta["data"].items()}
    combos: dict[PairKey, Combo] = {}
    for entry in meta["data"].values():
        for revisions in entry["combinations"].values():
            # Each combo may exist in several dated batches (Google redraws them);
            # exactly one revision per pair is flagged isLatest.
            latest = next((r for r in revisions if r.get("isLatest")), None) or max(revisions, key=lambda r: r["date"])
            key = pair_key(latest["leftEmojiCodepoint"], latest["rightEmojiCodepoint"])
            combos[key] = (latest["date"], latest["leftEmojiCodepoint"], latest["rightEmojiCodepoint"])
    return names, combos


def load_snapshot(directory: Path) -> tuple[dict[str, str], dict[PairKey, Combo], dict] | None:
    combos_file, emoji_file, meta_file = directory / "combos.tsv", directory / "emoji.tsv", directory / "meta.json"
    if not combos_file.exists():
        return None
    combos: dict[PairKey, Combo] = {}
    with combos_file.open(encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        next(reader, None)  # header
        for date, left, right in reader:
            combos[pair_key(left, right)] = (date, left, right)
    names: dict[str, str] = {}
    if emoji_file.exists():
        with emoji_file.open(encoding="utf-8", newline="") as f:
            reader = csv.reader(f, delimiter="\t")
            next(reader, None)
            names = {cp: name for cp, name in reader}
    info = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
    return names, combos, info


def save_snapshot(directory: Path, names: dict[str, str], combos: dict[PairKey, Combo], today: str) -> None:
    """Sorted TSVs: small, and every weekly commit is itself a readable diff."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "combos.tsv").open("w", encoding="utf-8", newline="") as f:
        f.write("date\tleft\tright\n")
        for key in sorted(combos):
            date, left, right = combos[key]
            f.write(f"{date}\t{left}\t{right}\n")
    with (directory / "emoji.tsv").open("w", encoding="utf-8", newline="") as f:
        f.write("codepoint\tname\n")
        for cp in sorted(names):
            f.write(f"{cp}\t{names[cp]}\n")
    (directory / "meta.json").write_text(
        json.dumps({"updated": today, "combos": len(combos), "emoji": len(names)}, indent=2) + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- diff
class Diff:
    def __init__(self, prev_names, prev_combos, cur_names, cur_combos):
        self.names = {**prev_names, **cur_names}  # keep names of emoji that were dropped upstream
        self.added = sorted(k for k in cur_combos.keys() - prev_combos.keys())
        self.removed = sorted(k for k in prev_combos.keys() - cur_combos.keys())
        self.redrawn = sorted(k for k in cur_combos.keys() & prev_combos.keys() if cur_combos[k][0] != prev_combos[k][0])
        self.emoji_added = sorted(cur_names.keys() - prev_names.keys())
        self.emoji_removed = sorted(prev_names.keys() - cur_names.keys())
        self.prev, self.cur = prev_combos, cur_combos

    def any(self) -> bool:
        return bool(self.added or self.removed or self.redrawn or self.emoji_added or self.emoji_removed)

    def name(self, cp: str) -> str:
        return self.names.get(cp) or "U+" + cp.upper().replace("-", " U+")

    def label(self, combo: Combo) -> tuple[str, str]:
        """('🌟 + 🥑', 'glowing star + avocado')"""
        _, left, right = combo
        return f"{glyph(left)} + {glyph(right)}", f"{self.name(left).replace('_', ' ')} + {self.name(right).replace('_', ' ')}"


# --------------------------------------------------------------------------- rendering
def summary_text(d: Diff, since: str | None, today: str) -> str:
    since_txt = f"Since the {since} snapshot" if since else "Compared with the previous snapshot"
    parts = [
        f"{since_txt}, Google's Emoji Kitchen gained **{fmt(len(d.added))}** combos, "
        f"dropped **{fmt(len(d.removed))}**, and redrew **{fmt(len(d.redrawn))}** existing ones. "
        f"It now offers **{fmt(len(d.cur))}** combos across **{fmt(len(set(cp for k in d.cur for cp in k)))}** emoji."
    ]
    if d.emoji_added:
        parts.append("Newly supported emoji: " + ", ".join(f"{glyph(cp)} {d.name(cp)}" for cp in d.emoji_added) + ".")
    if d.emoji_removed:
        parts.append("Emoji no longer supported: " + ", ".join(f"{glyph(cp)} {d.name(cp)}" for cp in d.emoji_removed) + ".")
    batches = sorted({d.cur[k][0] for k in d.added} | {d.cur[k][0] for k in d.redrawn})
    if len(batches) == 1:
        parts.append(f"Sticker batch: {batches[0]}.")
    elif batches:
        parts.append(f"Sticker batches: {len(batches)}, newest {batches[-1]}.")
    return " ".join(parts)


def render_markdown(d: Diff, since: str | None, today: str, max_inline: int | None, thumbs: bool, full_report: str | None) -> str:
    out = [f"# Emoji Kitchen changes — {today}", "", f"**Summary:** {summary_text(d, since, today)}", ""]

    def section(title: str, keys: list[PairKey], source: dict[PairKey, Combo], removed: bool = False, redraw: bool = False):
        out.append(f"## {title} ({fmt(len(keys))})")
        out.append("")
        if not keys:
            out.append("None.")
            out.append("")
            return
        shown = keys if max_inline is None else keys[:max_inline]
        for k in shown:
            date, left, right = source[k]
            glyphs, names = d.label(source[k])
            url = sticker_url(date, left, right)
            if removed:
                out.append(f"- {glyphs} — {names} · last seen in batch {date}")
                continue
            thumb = f'<img src="{url}" width="40" alt=""> ' if thumbs else ""
            batch = f"batch {d.prev[k][0]} → {date}" if redraw else f"batch {date}"
            out.append(f"- {thumb}{glyphs} — {names} · {batch} · [png]({url})")
        if len(shown) < len(keys):
            more = f"… and {fmt(len(keys) - len(shown))} more"
            out.append(f"- {more} — see the full report" + (f" (`{full_report}`)" if full_report else "") + ".")
        out.append("")

    section("New combos", d.added, d.cur)
    section("Removed combos", d.removed, d.prev, removed=True)
    if d.redrawn:
        section("Redrawn combos", d.redrawn, d.cur, redraw=True)
    out.append("---")
    out.append(f"Data: emojikitchen.dev's scan of Google's sticker server ([upstream commits]({UPSTREAM})). "
               "Combos are unordered emoji pairs; 'redrawn' means Google published a newer version of an existing sticker.")
    return "\n".join(out) + "\n"


def render_html(d: Diff, since: str | None, today: str, max_inline: int | None, full_report: str | None) -> str:
    esc = html.escape
    summary = esc(summary_text(d, since, today)).replace("**", "")  # bold markers -> plain
    css = ("body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#222;max-width:720px;margin:0 auto;padding:16px}"
           "h1{font-size:20px}h2{font-size:16px;margin-top:28px;border-bottom:1px solid #ddd;padding-bottom:4px}"
           "table{border-collapse:collapse}td{padding:6px 10px 6px 0;vertical-align:middle;font-size:14px}"
           "td.g{font-size:22px;white-space:nowrap}td.n{color:#555}td.b{color:#888;font-size:12px;white-space:nowrap}"
           "img{width:56px;height:56px;display:block}p.more{color:#555}p.foot{color:#888;font-size:12px;margin-top:32px}")
    out = [f"<!doctype html><html><head><meta charset='utf-8'><title>Emoji Kitchen changes — {today}</title><style>{css}</style></head><body>",
           f"<h1>Emoji Kitchen changes — {today}</h1>", f"<p><b>Summary:</b> {summary}</p>"]

    def section(title: str, keys: list[PairKey], source: dict[PairKey, Combo], removed: bool = False, redraw: bool = False):
        out.append(f"<h2>{title} ({fmt(len(keys))})</h2>")
        if not keys:
            out.append("<p>None.</p>")
            return
        shown = keys if max_inline is None else keys[:max_inline]
        out.append("<table>")
        for k in shown:
            date, left, right = source[k]
            glyphs, names = d.label(source[k])
            url = sticker_url(date, left, right)
            img = "" if removed else f'<a href="{url}"><img src="{url}" alt=""></a>'
            batch = f"last seen in batch {date}" if removed else (f"batch {esc(d.prev[k][0])} → {date}" if redraw else f"batch {date}")
            out.append(f"<tr><td>{img}</td><td class='g'>{esc(glyphs)}</td><td class='n'>{esc(names)}</td><td class='b'>{batch}</td></tr>")
        out.append("</table>")
        if len(shown) < len(keys):
            out.append(f"<p class='more'>… and {fmt(len(keys) - len(shown))} more — the full list is attached"
                       + (f" (<code>{esc(full_report)}</code>)" if full_report else "") + ".</p>")

    section("New combos", d.added, d.cur)
    section("Removed combos", d.removed, d.prev, removed=True)
    if d.redrawn:
        section("Redrawn combos", d.redrawn, d.cur, redraw=True)
    out.append(f"<p class='foot'>Data: emojikitchen.dev's scan of Google's sticker server (<a href='{UPSTREAM}'>upstream commits</a>). "
               "Combos are unordered emoji pairs; “redrawn” means Google published a newer version of an existing sticker.</p>")
    out.append("</body></html>")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- GitHub Actions plumbing
def gh_output(**kv: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for k, v in kv.items():
            f.write(f"{k}={v}\n")


def gh_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(markdown)


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("metadata", type=Path, help="path to the downloaded metadata.json")
    ap.add_argument("--snapshot-dir", type=Path, default=Path("snapshot"))
    ap.add_argument("--reports-dir", type=Path, default=Path("reports"), help="full, uncapped Markdown reports (committed)")
    ap.add_argument("--out-dir", type=Path, default=Path("out"), help="newsletter.md / newsletter.html for this run")
    ap.add_argument("--max-inline", type=int, default=100, help="combos listed per section in the newsletter (rest go to the report)")
    ap.add_argument("--date", default=dt.date.today().isoformat(), help="override today's date (YYYY-MM-DD)")
    args = ap.parse_args()

    today = args.date
    cur_names, cur_combos = load_metadata(args.metadata)
    print(f"Upstream data: {fmt(len(cur_combos))} combos across {len(cur_names)} emoji")

    previous = load_snapshot(args.snapshot_dir)
    if previous is None:
        save_snapshot(args.snapshot_dir, cur_names, cur_combos, today)
        msg = f"Baseline snapshot created: {fmt(len(cur_combos))} combos across {len(cur_names)} emoji. Nothing to compare yet."
        print(msg)
        gh_summary(f"### Emoji Kitchen watch\n\n{msg}\n")
        gh_output(changed="false", subject=f"Emoji Kitchen watch: baseline snapshot ({today})")
        return 0

    prev_names, prev_combos, info = previous
    since = info.get("updated")
    d = Diff(prev_names, prev_combos, cur_names, cur_combos)

    if not d.any():
        msg = f"No Emoji Kitchen changes since {since or 'the last snapshot'} ({fmt(len(cur_combos))} combos)."
        print(msg)
        gh_summary(f"### Emoji Kitchen watch\n\n{msg}\n")
        gh_output(changed="false", subject=f"Emoji Kitchen watch: no changes ({today})")
        return 0

    args.out_dir.mkdir(parents=True, exist_ok=True)
    args.reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.reports_dir / f"{today}.md"
    report_rel = report_path.as_posix()

    report_path.write_text(render_markdown(d, since, today, max_inline=None, thumbs=False, full_report=None), encoding="utf-8")
    newsletter_md = render_markdown(d, since, today, args.max_inline, thumbs=True, full_report=report_rel)
    (args.out_dir / "newsletter.md").write_text(newsletter_md, encoding="utf-8")
    (args.out_dir / "newsletter.html").write_text(render_html(d, since, today, args.max_inline, report_rel), encoding="utf-8")

    save_snapshot(args.snapshot_dir, cur_names, cur_combos, today)

    subject = f"Emoji Kitchen: {fmt(len(d.added))} new, {fmt(len(d.removed))} removed" \
              + (f", {fmt(len(d.redrawn))} redrawn" if d.redrawn else "") + f" ({today})"
    print(subject)
    print(f"Newsletter: {args.out_dir / 'newsletter.md'} · Full report: {report_path}")
    gh_summary(newsletter_md)
    gh_output(changed="true", subject=subject, report=report_rel)
    return 0


if __name__ == "__main__":
    sys.exit(main())

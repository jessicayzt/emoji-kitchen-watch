#!/usr/bin/env python3
"""
Emoji Kitchen watch
===================

Google publishes Emoji Kitchen stickers as static PNGs on gstatic.com and offers
no feed or changelog. It does publish the pair list behind the "emoji kitchen"
widget in Google Search:

    https://www.google.com/logos/fnbx/emoji_kitchen/emoji_kitchen_pairs.<N>.pb

That protobuf lists every supported emoji and every valid pair, but no dates and
no image URLs. Google bumps <N> every few months and leaves old versions online,
so this script probes upward from the last version it saw. It then:

1. Decodes the newest pair list into a set of unordered emoji pairs.
2. Compares it with the previous snapshot in ``snapshot/``.
3. Finds the sticker PNG for each *new* pair by HEAD-probing Google's dated
   folders on gstatic.com, discovering new folders when needed.
4. Writes a newsletter (Markdown + HTML) listing new, removed and - when a
   folder sweep was requested - redrawn combos.
5. Overwrites the snapshot so the next run diffs against today.

Standard library only.
  Normal run:   python watch.py
  Offline:      python watch.py --pairs-file emoji_kitchen_pairs.13.pb --offline
  Redraw check: python watch.py --sweep 20260728
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import http.client
import json
import os
import ssl
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PairKey = tuple[str, str]                 # sorted (codepointA, codepointB)
Combo = tuple[str, str, str]              # (sticker folder YYYYMMDD, or "" if unknown; left codepoint; right codepoint)

PAIRS_URL = "https://www.google.com/logos/fnbx/emoji_kitchen/emoji_kitchen_pairs.{version}.pb"
DEFAULT_VERSION = 13                      # newest version known when this was written (published 2026-09-01)
VERSION_LOOKAHEAD = 3                     # stop probing upward after this many consecutive missing versions
GSTATIC_HOST = "www.gstatic.com"
GSTATIC_PATH = "/android/keyboard/emojikitchen/{date}/u{left}/u{left}_u{right}.png"
USER_AGENT = "Mozilla/5.0 (compatible; emoji-kitchen-watch)"

# Dated sticker folders seen on gstatic.com so far, newest first. Folder names are publish dates.
# The snapshot's own folders are added at run time; this list matters for folders the snapshot has
# never seen and for a fresh baseline. New folders are discovered automatically when a new pair
# cannot be found in any known folder.
KNOWN_FOLDERS = (
    "20260728", "20260429", "20260428", "20260202", "20260128", "20251029", "20250731", "20250728",
    "20250519", "20250501", "20250430", "20250204", "20250130", "20241113", "20241023", "20241021",
    "20241004", "20240715", "20240612", "20240610", "20240530", "20240214", "20240206", "20231128",
    "20231113", "20230821", "20230818", "20230803", "20230426", "20230421", "20230418", "20230405",
    "20230301", "20230221", "20230216", "20230127", "20230126", "20230118", "20221107", "20221101",
    "20220823", "20220815", "20220506", "20220406", "20220203", "20220110", "20211115", "20210831",
    "20210521", "20210218", "20201001",
)


# --------------------------------------------------------------------------- helpers
def glyph(codepoint: str) -> str:
    """'1f642-200d-2195-fe0f' -> the actual emoji character(s)."""
    return "".join(chr(int(part, 16)) for part in codepoint.split("-"))


def codepoints(emoji: str) -> str:
    """The reverse of glyph(): '🙂\\u200d↕️' -> '1f642-200d-2195-fe0f'."""
    return "-".join(f"{ord(c):x}" for c in emoji)


def ufmt(codepoint: str) -> str:
    """Google's file names prefix every codepoint component with 'u': 263a-fe0f -> 263a-ufe0f (caller adds the first u)."""
    return "-u".join(codepoint.split("-"))


def sticker_path(date: str, left: str, right: str) -> str:
    return GSTATIC_PATH.format(date=date, left=ufmt(left), right=ufmt(right))


def sticker_url(date: str, left: str, right: str) -> str:
    """Full image URL, or "" when the sticker's folder is not known yet."""
    return f"https://{GSTATIC_HOST}{sticker_path(date, left, right)}" if date else ""


def pair_key(a: str, b: str) -> PairKey:
    return tuple(sorted((a, b)))  # type: ignore[return-value]


def fmt(n: int) -> str:
    return f"{n:,}"


def emoji_name(codepoint: str) -> str:
    """Best-effort snake_case name from the Unicode database: '1f418' -> 'elephant'."""
    words: list[str] = []
    for part in codepoint.split("-"):
        if part in ("fe0f", "200d"):
            continue
        try:
            words.extend(unicodedata.name(chr(int(part, 16))).lower().split())
        except ValueError:
            words.append("u+" + part)
    return "_".join(words) or "u+" + codepoint


# --------------------------------------------------------------------------- Google's pair list
def http_get(url: str, attempts: int = 3) -> tuple[bytes | None, str | None]:
    """GET url -> (body, Last-Modified). (None, None) on 404. Retries transient failures."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read(), resp.headers.get("Last-Modified")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, None
            if attempt == attempts - 1 or (e.code < 500 and e.code != 429):
                raise
        except (urllib.error.URLError, OSError):
            if attempt == attempts - 1:
                raise
        time.sleep(5 * (attempt + 1))
    return None, None


def fetch_pair_list(start: int) -> tuple[int, bytes, str | None]:
    """Download the newest pair list, probing version numbers upward from `start`.

    Google leaves old versions online, so the newest one that answers 200 is current.
    """
    newest: tuple[int, bytes, str | None] | None = None
    version, misses = start, 0
    while misses < VERSION_LOOKAHEAD:
        body, modified = http_get(PAIRS_URL.format(version=version))
        if body is None:
            misses += 1
        else:
            newest, misses = (version, body, modified), 0
        version += 1
    if newest is None:
        raise SystemExit(f"::error::No pair list found at versions {start}-{version - 1} of {PAIRS_URL}. Did Google move it?")
    return newest


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    n = shift = 0
    while True:
        byte = buf[i]
        i += 1
        n |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return n, i


def _message(buf: bytes) -> dict[int, list]:
    """Decode one protobuf message into {field number: [values]} without a schema.

    Varints come back as int, length-delimited fields as bytes (nested messages are decoded by the caller).
    """
    fields: dict[int, list] = {}
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        wire = key & 7
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 1:
            value, i = buf[i:i + 8], i + 8
        elif wire == 2:
            length, i = _varint(buf, i)
            value, i = buf[i:i + length], i + length
        elif wire == 5:
            value, i = buf[i:i + 4], i + 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire} at byte {i}")
        fields.setdefault(key >> 3, []).append(value)
    return fields


def _ints(values: list) -> list[int]:
    """Repeated uint32 field: packed (bytes) or plain repeated varints (ints) - accept both."""
    out: list[int] = []
    for v in values:
        if isinstance(v, (bytes, bytearray)):
            i = 0
            while i < len(v):
                n, i = _varint(v, i)
                out.append(n)
        else:
            out.append(v)
    return out


def decode_pair_list(data: bytes) -> tuple[list[str], list[tuple[str, int]], set[PairKey]]:
    """Decode Google's emoji_kitchen_pairs.N.pb -> (emoji codepoints in Google's order, categories, pairs).

    Schema (reverse-engineered, proto3):
        message EmojiKitchenData { repeated Emoji emojis = 1; repeated Pairings pairings = 2; repeated Category categories = 3; }
        message Emoji     { string emoji = 1; uint32 id = 2; }
        message Pairings  { uint32 id = 1; repeated uint32 partner_ids = 2 [packed = true]; }
        message Category  { string name = 1; uint32 start_emoji_id = 2; }
    """
    top = _message(data)
    by_id: dict[int, str] = {}
    for m in map(_message, top.get(1, [])):
        by_id[m.get(2, [0])[0]] = codepoints(m[1][0].decode("utf-8"))
    pairs: set[PairKey] = set()
    for m in map(_message, top.get(2, [])):
        try:
            a = by_id[m[1][0]]
            for partner in _ints(m.get(2, [])):
                pairs.add(pair_key(a, by_id[partner]))
        except KeyError as e:
            raise ValueError(f"pair list references unknown emoji id {e}") from None
    categories = [(m[1][0].decode("utf-8"), m.get(2, [0])[0]) for m in map(_message, top.get(3, []))]
    if not by_id or not pairs:
        raise ValueError("pair list decoded to no emoji or no pairs - has Google changed the format?")
    return [by_id[i] for i in sorted(by_id)], categories, pairs


# --------------------------------------------------------------------------- sticker lookup on gstatic.com
class Stickers:
    """HEAD-probes gstatic.com to learn which dated folder holds a pair's sticker.

    Google stores each sticker under one codepoint order (u1f31f_u1f951.png) inside a folder named after
    the publish date; the pair list says nothing about either, so we try both orders in each candidate folder.
    One keep-alive connection per worker thread.
    """

    def __init__(self, workers: int = 16):
        self.workers = workers
        self.requests = 0
        self._lock = threading.Lock()
        self._local = threading.local()
        self._ssl = ssl.create_default_context()

    def exists(self, path: str) -> bool | None:
        """True / False for 200 / 404; None if gstatic kept failing (treated as not found)."""
        for attempt in range(5):
            try:
                conn = getattr(self._local, "conn", None)
                if conn is None:
                    conn = self._local.conn = http.client.HTTPSConnection(GSTATIC_HOST, timeout=20, context=self._ssl)
                conn.request("HEAD", path, headers={"User-Agent": USER_AGENT})
                resp = conn.getresponse()
                resp.read()
                with self._lock:
                    self.requests += 1
                if resp.status == 200:
                    return True
                if resp.status == 404:
                    return False
            except (http.client.HTTPException, OSError):
                pass
            self._local.conn = None            # reconnect after an error or an odd status (429, 5xx) and back off
            time.sleep(0.5 * (attempt + 1))
        return None

    def _exists_many(self, paths: Sequence[str]) -> list[bool | None]:
        with ThreadPoolExecutor(self.workers) as pool:
            return list(pool.map(self.exists, paths))

    @staticmethod
    def _orders(pairs: Iterable[PairKey]) -> list[tuple[PairKey, str, str]]:
        """Both codepoint orders for every pair (one for a self-combo)."""
        jobs = []
        for a, b in pairs:
            jobs.append(((a, b), a, b))
            if a != b:
                jobs.append(((a, b), b, a))
        return jobs

    def in_folder(self, pairs: Iterable[PairKey], folder: str) -> dict[PairKey, Combo]:
        """{pair: (folder, left, right)} for the pairs whose sticker exists in `folder`."""
        jobs = self._orders(pairs)
        results = self._exists_many([sticker_path(folder, left, right) for _, left, right in jobs])
        found: dict[PairKey, Combo] = {}
        for (key, left, right), ok in zip(jobs, results):
            if ok and key not in found:
                found[key] = (folder, left, right)
        return found

    def resolve(self, pairs: Iterable[PairKey], folders: Sequence[str]) -> dict[PairKey, Combo]:
        """Try the folders in order until every pair is found or the folders run out."""
        found: dict[PairKey, Combo] = {}
        pending = list(pairs)
        for folder in folders:
            if not pending:
                break
            found.update(self.in_folder(pending, folder))
            pending = [p for p in pending if p not in found]
        return found

    def discover(self, sample: Sequence[PairKey], after: str | None, today: str, max_days: int = 400) -> list[str]:
        """Find sticker folders newer than `after` (YYYYMMDD) by probing every day-named folder up to `today`
        (ISO date) for a few sample pairs. Returns the folders that exist, newest first."""
        end = dt.date.fromisoformat(today)
        start = end - dt.timedelta(days=max_days)
        if after:
            start = max(start, dt.datetime.strptime(after, "%Y%m%d").date() + dt.timedelta(days=1))
        days = []
        day = start
        while day <= end:
            days.append(day.strftime("%Y%m%d"))
            day += dt.timedelta(days=1)
        jobs = [(d, left, right) for d in days for _, left, right in self._orders(sample)]
        results = self._exists_many([sticker_path(d, left, right) for d, left, right in jobs])
        return sorted({d for (d, _, _), ok in zip(jobs, results) if ok}, reverse=True)


def known_folders(combos: dict[PairKey, Combo], info: dict) -> list[str]:
    """Every sticker folder we know about, newest first."""
    folders = {c[0] for c in combos.values() if c[0]} | set(info.get("folders", [])) | set(KNOWN_FOLDERS)
    return sorted(folders, reverse=True)


# --------------------------------------------------------------------------- snapshot
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


def save_snapshot(directory: Path, names: dict[str, str], combos: dict[PairKey, Combo], info: dict) -> None:
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
    (directory / "meta.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- diff
class Diff:
    def __init__(self, prev_names, prev_combos, cur_names, cur_combos):
        self.names = {**prev_names, **cur_names}  # keep names of emoji that were dropped upstream
        self.added = sorted(k for k in cur_combos.keys() - prev_combos.keys())
        self.removed = sorted(k for k in prev_combos.keys() - cur_combos.keys())
        # A pair whose sticker moved to a newer folder was redrawn. A pair that merely went from
        # "folder unknown" to known is not a change in Google's catalogue, so it is not reported.
        self.redrawn = sorted(k for k in cur_combos.keys() & prev_combos.keys()
                              if prev_combos[k][0] and cur_combos[k][0] != prev_combos[k][0])
        self.emoji_added = sorted(cur_names.keys() - prev_names.keys())
        self.emoji_removed = sorted(prev_names.keys() - cur_names.keys())
        self.prev, self.cur = prev_combos, cur_combos
        self.cur_names = cur_names
        # Run context, filled in by main()
        self.version: int | None = None
        self.modified: str | None = None
        self.discovered: list[str] = []
        self.swept: list[str] = []
        self.unresolved = 0

    def any(self) -> bool:
        return bool(self.added or self.removed or self.redrawn or self.emoji_added or self.emoji_removed)

    def name(self, cp: str) -> str:
        return self.names.get(cp) or "U+" + cp.upper().replace("-", " U+")

    def label(self, combo: Combo) -> tuple[str, str]:
        """('🌟 + 🥑', 'glowing star + avocado')"""
        _, left, right = combo
        return f"{glyph(left)} + {glyph(right)}", f"{self.name(left).replace('_', ' ')} + {self.name(right).replace('_', ' ')}"


# --------------------------------------------------------------------------- rendering
def source_text(d: Diff) -> str:
    if d.version is None:
        return ""
    return f"Google's pair list v{d.version}" + (f" (last modified {d.modified})" if d.modified else "")


def summary_text(d: Diff, since: str | None, today: str) -> str:
    since_txt = f"Since the {since} snapshot" if since else "Compared with the previous snapshot"
    parts = [
        f"{since_txt}, Google's Emoji Kitchen gained **{fmt(len(d.added))}** combos, "
        f"dropped **{fmt(len(d.removed))}**, and redrew **{fmt(len(d.redrawn))}** existing ones. "
        f"It now offers **{fmt(len(d.cur))}** combos across **{fmt(len(d.cur_names))}** emoji."
    ]
    if d.emoji_added:
        parts.append("Newly supported emoji: " + ", ".join(f"{glyph(cp)} {d.name(cp)}" for cp in d.emoji_added) + ".")
    if d.emoji_removed:
        parts.append("Emoji no longer supported: " + ", ".join(f"{glyph(cp)} {d.name(cp)}" for cp in d.emoji_removed) + ".")
    batches = sorted({d.cur[k][0] for k in d.added + d.redrawn if d.cur[k][0]})
    if len(batches) == 1:
        parts.append(f"Sticker batch: {batches[0]}.")
    elif batches:
        parts.append(f"Sticker batches: {len(batches)}, newest {batches[-1]}.")
    if d.discovered:
        parts.append("New sticker folder" + ("s" if len(d.discovered) > 1 else "") + " on gstatic: " + ", ".join(d.discovered)
                     + ". Run the workflow with `sweep` set to " + ",".join(d.discovered)
                     + " to check whether existing combos were redrawn there.")
    if d.swept:
        parts.append("Swept folder" + ("s" if len(d.swept) > 1 else "") + " for redraws: " + ", ".join(d.swept) + ".")
    if d.unresolved:
        parts.append(f"{fmt(d.unresolved)} combo{'s' if d.unresolved != 1 else ''} without a reachable sticker image yet; retried next run.")
    src = source_text(d)
    if src:
        parts.append(f"Source: {src}.")
    return " ".join(parts)


def footer_text(d: Diff) -> str:
    return ("Data: Google's own Emoji Kitchen pair list, the file behind the \"emoji kitchen\" widget in Google Search "
            f"({PAIRS_URL.format(version=d.version if d.version is not None else 'N')}); sticker images are located on gstatic.com. "
            "Combos are unordered emoji pairs; 'redrawn' means a newer dated folder now holds the sticker (found by a folder sweep).")


def render_markdown(d: Diff, since: str | None, today: str, max_inline: int | None, thumbs: bool, full_report: str | None,
                    report_url: str | None = None) -> str:
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
                out.append(f"- {glyphs} — {names} · " + (f"last seen in batch {date}" if date else "sticker folder unknown"))
                continue
            if not url:
                out.append(f"- {glyphs} — {names} · sticker image not found yet")
                continue
            thumb = f'<img src="{url}" width="40" alt=""> ' if thumbs else ""
            batch = f"batch {d.prev[k][0]} → {date}" if redraw else f"batch {date}"
            out.append(f"- {thumb}{glyphs} — {names} · {batch} · [png]({url})")
        if len(shown) < len(keys):
            more = f"… and {fmt(len(keys) - len(shown))} more — see the "
            if report_url:
                more += f"[full report]({report_url})"
            else:
                more += "full report" + (f" (`{full_report}`)" if full_report else "")
            out.append(f"- {more}.")
        out.append("")

    section("New combos", d.added, d.cur)
    section("Removed combos", d.removed, d.prev, removed=True)
    if d.redrawn:
        section("Redrawn combos", d.redrawn, d.cur, redraw=True)
    out.append("---")
    out.append(footer_text(d))
    return "\n".join(out) + "\n"


def render_html(d: Diff, since: str | None, today: str, max_inline: int | None, full_report: str | None,
                report_url: str | None = None) -> str:
    esc = html.escape
    summary = esc(summary_text(d, since, today)).replace("**", "").replace("`", "")  # markdown markers -> plain
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
            img = f'<a href="{url}"><img src="{url}" alt=""></a>' if url and not removed else ""
            if removed:
                batch = f"last seen in batch {date}" if date else "sticker folder unknown"
            elif not url:
                batch = "sticker image not found yet"
            elif redraw:
                batch = f"batch {esc(d.prev[k][0])} → {date}"
            else:
                batch = f"batch {date}"
            out.append(f"<tr><td>{img}</td><td class='g'>{esc(glyphs)}</td><td class='n'>{esc(names)}</td><td class='b'>{batch}</td></tr>")
        out.append("</table>")
        if len(shown) < len(keys):
            rest = f"… and {fmt(len(keys) - len(shown))} more — "
            if report_url:
                rest += f"see the <a href='{esc(report_url)}'>full report</a> (also attached)"
            else:
                rest += "the full list is attached" + (f" (<code>{esc(full_report)}</code>)" if full_report else "")
            out.append(f"<p class='more'>{rest}.</p>")

    section("New combos", d.added, d.cur)
    section("Removed combos", d.removed, d.prev, removed=True)
    if d.redrawn:
        section("Redrawn combos", d.redrawn, d.cur, redraw=True)
    out.append(f"<p class='foot'>{esc(footer_text(d))}</p>")
    out.append("</body></html>")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- GitHub Actions plumbing
def report_link(report_rel: str) -> str | None:
    """URL of the committed report on GitHub when running inside Actions (the commit step pushes it before the issue opens)."""
    repo, ref = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_REF_NAME")
    if not repo or not ref:
        return None
    return f"{os.environ.get('GITHUB_SERVER_URL') or 'https://github.com'}/{repo}/blob/{ref}/{report_rel}"


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
def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pairs-file", type=Path, help="decode this already-downloaded .pb instead of fetching from Google")
    ap.add_argument("--version", type=int, help=f"pair-list version to start probing from (default: the snapshot's, else {DEFAULT_VERSION})")
    ap.add_argument("--offline", action="store_true", help="never touch the network: no download, no sticker lookups (needs --pairs-file)")
    ap.add_argument("--sweep", default="", help="comma-separated sticker folders (YYYYMMDD) to re-check every existing combo against, "
                                                "to find redrawn stickers (two HEAD requests per combo per folder)")
    ap.add_argument("--resolve-limit", type=int, default=500, help="max combos still lacking a sticker folder to retry per run")
    ap.add_argument("--workers", type=int, default=16, help="parallel HEAD requests against gstatic.com")
    ap.add_argument("--snapshot-dir", type=Path, default=Path("snapshot"))
    ap.add_argument("--reports-dir", type=Path, default=Path("reports"), help="full, uncapped Markdown reports (committed)")
    ap.add_argument("--out-dir", type=Path, default=Path("out"), help="newsletter.md / newsletter.html for this run")
    ap.add_argument("--max-inline", type=int, default=100, help="combos listed per section in the newsletter (rest go to the report)")
    ap.add_argument("--report-url", help="link to the committed full report, used by the newsletter's 'and N more' line "
                                         "(default: built from GITHUB_SERVER_URL / GITHUB_REPOSITORY / GITHUB_REF_NAME inside Actions)")
    ap.add_argument("--date", default=dt.date.today().isoformat(), help="override today's date (YYYY-MM-DD)")
    args = ap.parse_args(argv)

    today = args.date
    previous = load_snapshot(args.snapshot_dir)
    prev_names, prev_combos, info = previous or ({}, {}, {})
    since = info.get("updated")

    # 1. Google's pair list
    start_version = args.version or info.get("source_version") or DEFAULT_VERSION
    if args.pairs_file:
        data, version, modified = args.pairs_file.read_bytes(), start_version, None
    elif args.offline:
        ap.error("--offline needs --pairs-file")
    else:
        version, data, modified = fetch_pair_list(start_version)
    emojis, _categories, pairs = decode_pair_list(data)
    print(f"Pair list v{version}: {fmt(len(pairs))} combos across {len(emojis)} emoji" + (f" (Last-Modified {modified})" if modified else ""))
    cur_names = {cp: prev_names.get(cp) or emoji_name(cp) for cp in emojis}

    # 2. Carry forward the sticker location of every pair we already knew; look up the rest on gstatic
    cur_combos: dict[PairKey, Combo] = {}
    new_keys, dateless = [], []
    for key in pairs:
        prev = prev_combos.get(key)
        if prev is None:
            new_keys.append(key)
        else:
            cur_combos[key] = prev
            if not prev[0]:
                dateless.append(key)
    new_keys.sort()
    dateless.sort()
    folders = known_folders(prev_combos, info)
    to_resolve = (new_keys + dateless[:args.resolve_limit]) if previous is not None else []  # a fresh baseline is not probed
    stickers = None if args.offline else Stickers(args.workers)
    discovered: list[str] = []
    found: dict[PairKey, Combo] = {}
    if stickers and to_resolve:
        step = max(1, len(to_resolve) // 3)
        discovered = stickers.discover(to_resolve[::step][:3], folders[0] if folders else None, today)
        if discovered:
            print(f"New sticker folders on gstatic: {', '.join(discovered)}")
        found = stickers.resolve(to_resolve, discovered + [f for f in folders if f not in discovered])
        print(f"Sticker lookup: {fmt(len(found))} of {fmt(len(to_resolve))} combos located ({fmt(stickers.requests)} requests)")
    for key in new_keys:
        cur_combos[key] = found.get(key, ("", key[0], key[1]))
    for key in dateless:
        if key in found:
            cur_combos[key] = found[key]

    # 3. Optional redraw check: does an existing combo have a sticker in a newer folder?
    swept = [f.strip() for f in args.sweep.split(",") if f.strip()]
    if swept and stickers:
        for folder in swept:
            hits = stickers.in_folder([k for k, c in cur_combos.items() if c[0] < folder], folder)
            cur_combos.update(hits)
            print(f"Sweep {folder}: {fmt(len(hits))} stickers found ({fmt(stickers.requests)} requests so far)")

    all_folders = sorted(set(folders) | set(discovered) | set(swept) | {c[0] for c in cur_combos.values() if c[0]}, reverse=True)
    unresolved = sum(1 for c in cur_combos.values() if not c[0])
    meta = {
        "updated": since or today,
        "combos": len(cur_combos),
        "emoji": len(cur_names),
        "source": PAIRS_URL.format(version=version),
        "source_version": version,
        "source_last_modified": modified or info.get("source_last_modified"),
        "folders": all_folders,
        "unresolved": unresolved,
    }

    if previous is None:
        meta["updated"] = today
        save_snapshot(args.snapshot_dir, cur_names, cur_combos, meta)
        msg = (f"Baseline snapshot created from pair list v{version}: {fmt(len(cur_combos))} combos across {len(cur_names)} emoji. "
               "Nothing to compare yet; sticker images are looked up for combos added from now on.")
        print(msg)
        gh_summary(f"### Emoji Kitchen watch\n\n{msg}\n")
        gh_output(changed="false", subject=f"Emoji Kitchen watch: baseline snapshot ({today})")
        return 0

    d = Diff(prev_names, prev_combos, cur_names, cur_combos)
    d.version, d.modified, d.discovered, d.swept, d.unresolved = version, modified, discovered, swept, unresolved

    if not d.any():
        if cur_combos != prev_combos or cur_names != prev_names or any(info.get(k) != v for k, v in meta.items()):
            save_snapshot(args.snapshot_dir, cur_names, cur_combos, meta)   # persist located stickers / version bump
        msg = f"No Emoji Kitchen changes since {since or 'the last snapshot'} ({fmt(len(cur_combos))} combos, {source_text(d)})."
        print(msg)
        gh_summary(f"### Emoji Kitchen watch\n\n{msg}\n")
        gh_output(changed="false", subject=f"Emoji Kitchen watch: no changes ({today})")
        return 0

    args.out_dir.mkdir(parents=True, exist_ok=True)
    args.reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.reports_dir / f"{today}.md"
    report_rel = report_path.as_posix()
    report_url = args.report_url or report_link(report_rel)

    report_path.write_text(render_markdown(d, since, today, max_inline=None, thumbs=False, full_report=None), encoding="utf-8")
    newsletter_md = render_markdown(d, since, today, args.max_inline, thumbs=True, full_report=report_rel, report_url=report_url)
    (args.out_dir / "newsletter.md").write_text(newsletter_md, encoding="utf-8")
    (args.out_dir / "newsletter.html").write_text(render_html(d, since, today, args.max_inline, report_rel, report_url=report_url),
                                                  encoding="utf-8")

    meta["updated"] = today
    save_snapshot(args.snapshot_dir, cur_names, cur_combos, meta)

    subject = f"Emoji Kitchen: {fmt(len(d.added))} new, {fmt(len(d.removed))} removed" \
              + (f", {fmt(len(d.redrawn))} redrawn" if d.redrawn else "") + f" ({today})"
    print(subject)
    print(f"Newsletter: {args.out_dir / 'newsletter.md'} · Full report: {report_path}")
    gh_summary(newsletter_md)
    gh_output(changed="true", subject=subject, report=report_rel)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Offline tests for watch.py.  Run:  python -m unittest discover -s tests -v"""
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import watch  # noqa: E402

ELEPHANT, PINK_HEART, SLOTH, STAR = "1f418", "1fa77", "1f9a5", "1f31f"


# --------------------------------------------------------------------------- protobuf fixture
def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte, n = n & 0x7F, n >> 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def ld(field: int, payload: bytes) -> bytes:      # length-delimited field
    return varint(field << 3 | 2) + varint(len(payload)) + payload


def vi(field: int, n: int) -> bytes:              # varint field
    return varint(field << 3) + varint(n)


def pair_list(emojis, pairs, categories=(), packed=True) -> bytes:
    """Build bytes shaped like Google's emoji_kitchen_pairs.N.pb. Emoji ids are 1-based positions."""
    out = b""
    for i, e in enumerate(emojis, 1):
        out += ld(1, ld(1, e.encode("utf-8")) + vi(2, i))
    by_id: dict[int, list[int]] = {}
    for a, b in pairs:
        by_id.setdefault(a, []).append(b)
    for a, partners in by_id.items():
        if packed:
            out += ld(2, vi(1, a) + ld(2, b"".join(varint(p) for p in partners)))
        else:
            out += ld(2, vi(1, a) + b"".join(vi(2, p) for p in partners))
    for name, start in categories:
        out += ld(3, ld(1, name.encode("utf-8")) + vi(2, start))
    return out


class FakeStickers(watch.Stickers):
    """Stickers whose gstatic is a set of existing paths; counts requests."""

    def __init__(self, existing):
        super().__init__(workers=2)
        self.existing = set(existing)

    def exists(self, path):
        with self._lock:
            self.requests += 1
        return path in self.existing


def path(folder, left, right):
    return watch.sticker_path(folder, left, right)


# --------------------------------------------------------------------------- tests
class DecodeTests(unittest.TestCase):
    def test_decodes_emoji_pairs_and_categories(self):
        data = pair_list(["🐘", "🩷", "🦥"], [(1, 1), (2, 3), (3, 2)], [("Animals", 1), ("Symbols", 2)])
        emojis, categories, pairs = watch.decode_pair_list(data)
        self.assertEqual(emojis, [ELEPHANT, PINK_HEART, SLOTH])
        self.assertEqual(categories, [("Animals", 1), ("Symbols", 2)])
        self.assertEqual(pairs, {(ELEPHANT, ELEPHANT), (SLOTH, PINK_HEART)})  # unordered, duplicates collapse

    def test_accepts_unpacked_repeated_ids(self):
        data = pair_list(["🐘", "🦥"], [(1, 2)], packed=False)
        self.assertEqual(watch.decode_pair_list(data)[2], {(ELEPHANT, SLOTH)})

    def test_multi_codepoint_emoji(self):
        data = pair_list(["🙂‍↔️", "☺️"], [(1, 2)])
        emojis, _, pairs = watch.decode_pair_list(data)
        self.assertEqual(emojis, ["1f642-200d-2194-fe0f", "263a-fe0f"])
        self.assertEqual(pairs, {("1f642-200d-2194-fe0f", "263a-fe0f")})

    def test_rejects_empty_or_inconsistent_data(self):
        with self.assertRaises(ValueError):
            watch.decode_pair_list(b"")
        with self.assertRaises(ValueError):
            watch.decode_pair_list(pair_list(["🐘"], [(1, 7)]))  # partner id 7 does not exist


class HelperTests(unittest.TestCase):
    def test_sticker_url(self):
        self.assertEqual(watch.sticker_url("20260428", PINK_HEART, SLOTH),
                         "https://www.gstatic.com/android/keyboard/emojikitchen/20260428/u1fa77/u1fa77_u1f9a5.png")
        self.assertEqual(watch.sticker_url("20230301", "263a-fe0f", "1f642-200d-2194-fe0f"),
                         "https://www.gstatic.com/android/keyboard/emojikitchen/20230301/u263a-ufe0f/u263a-ufe0f_u1f642-u200d-u2194-ufe0f.png")
        self.assertEqual(watch.sticker_url("", PINK_HEART, SLOTH), "")

    def test_emoji_name(self):
        self.assertEqual(watch.emoji_name(ELEPHANT), "elephant")
        self.assertEqual(watch.emoji_name("263a-fe0f"), "white_smiling_face")
        self.assertEqual(watch.emoji_name("e0000"), "u+e0000")  # unassigned: fall back to the codepoint

    def test_known_folders_merges_snapshot_meta_and_builtin(self):
        folders = watch.known_folders({("a", "b"): ("20990101", "a", "b"), ("c", "d"): ("", "c", "d")}, {"folders": ["20980101"]})
        self.assertEqual(folders[:2], ["20990101", "20980101"])
        self.assertIn("20260728", folders)
        self.assertNotIn("", folders)


class StickerLookupTests(unittest.TestCase):
    def test_resolve_prefers_earlier_folder_and_keeps_google_order(self):
        s = FakeStickers({path("20260428", PINK_HEART, SLOTH), path("20201001", SLOTH, PINK_HEART), path("20260728", ELEPHANT, ELEPHANT)})
        found = s.resolve([(PINK_HEART, SLOTH), (ELEPHANT, ELEPHANT), (STAR, SLOTH)], ["20260728", "20260428", "20201001"])
        self.assertEqual(found[(PINK_HEART, SLOTH)], ("20260428", PINK_HEART, SLOTH))
        self.assertEqual(found[(ELEPHANT, ELEPHANT)], ("20260728", ELEPHANT, ELEPHANT))
        self.assertNotIn((STAR, SLOTH), found)
        # 3 pairs -> 5 paths in the first folder (the self-combo has one order), then 2 pending pairs -> 4 paths, then 1 pair -> 2
        self.assertEqual(s.requests, 5 + 4 + 2)

    def test_in_folder_reports_the_order_that_exists(self):
        s = FakeStickers({path("20250728", SLOTH, PINK_HEART)})
        self.assertEqual(s.in_folder([(PINK_HEART, SLOTH)], "20250728"), {(PINK_HEART, SLOTH): ("20250728", SLOTH, PINK_HEART)})

    def test_discover_finds_day_named_folders_after_the_newest_known_one(self):
        s = FakeStickers({path("20260728", ELEPHANT, ELEPHANT), path("20260101", ELEPHANT, ELEPHANT)})
        self.assertEqual(s.discover([(ELEPHANT, ELEPHANT)], after="20260429", today="2026-11-03"), ["20260728"])
        self.assertEqual(s.requests, (dt_days("2026-04-30", "2026-11-03")))
        self.assertEqual(FakeStickers(set()).discover([(ELEPHANT, ELEPHANT)], after="20261103", today="2026-11-03"), [])


def dt_days(start: str, end: str) -> int:
    import datetime as dt
    return (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days + 1


class DiffTests(unittest.TestCase):
    def test_redrawn_requires_a_previous_folder(self):
        prev = {(STAR, SLOTH): ("20201001", STAR, SLOTH), (PINK_HEART, SLOTH): ("", PINK_HEART, SLOTH), ("a", "b"): ("20201001", "a", "b")}
        cur = {(STAR, SLOTH): ("20260429", STAR, SLOTH), (PINK_HEART, SLOTH): ("20260428", PINK_HEART, SLOTH), ("c", "d"): ("", "c", "d")}
        d = watch.Diff({STAR: "star2"}, prev, {STAR: "star2", ELEPHANT: "elephant"}, cur)
        self.assertEqual(d.redrawn, [(STAR, SLOTH)])          # moved to a newer folder
        self.assertEqual(d.added, [("c", "d")])
        self.assertEqual(d.removed, [("a", "b")])
        self.assertEqual(d.emoji_added, [ELEPHANT])
        self.assertTrue(d.any())


class GroupingTests(unittest.TestCase):
    TULIP, CROISSANT = "1f337", "1f950"

    def test_greedy_groups_and_partner_order(self):
        keys = [(self.TULIP, ELEPHANT), (ELEPHANT, self.CROISSANT), (ELEPHANT, ELEPHANT), (ELEPHANT, SLOTH), (SLOTH, PINK_HEART), (STAR, SLOTH)]
        groups = watch.group_by_emoji(keys)
        self.assertEqual([cp for cp, _ in groups], [ELEPHANT, SLOTH])
        self.assertEqual(groups[0][1], [(ELEPHANT, ELEPHANT), (self.TULIP, ELEPHANT), (ELEPHANT, self.CROISSANT), (ELEPHANT, SLOTH)])  # self first, then by partner
        self.assertEqual(groups[1][1], [(STAR, SLOTH), (SLOTH, PINK_HEART)])          # the shared elephant+sloth pair was taken by the bigger group
        self.assertEqual(sum(len(m) for _, m in groups), len(keys))                  # every pair exactly once

    def test_singletons_are_pooled(self):
        keys = [(STAR, SLOTH), (ELEPHANT, ELEPHANT), (self.TULIP, PINK_HEART), (self.TULIP, SLOTH)]
        groups = watch.group_by_emoji(keys)
        self.assertEqual([cp for cp, _ in groups], [self.TULIP, None])
        self.assertEqual(groups[1][1], [(STAR, SLOTH), (ELEPHANT, ELEPHANT)])   # pooled pairs keep plain sorted order

    def test_layout_spreads_the_newsletter_cap_over_groups(self):
        keys = [(ELEPHANT, f"1f{i:03x}") for i in range(12)] + [(SLOTH, f"1e{i:03x}") for i in range(3)]
        groups, printed, hidden = watch.layout(keys, max_inline=7)
        self.assertEqual([(cp, len(m), len(s)) for cp, m, s in printed], [(ELEPHANT, 12, 5), (SLOTH, 3, 2)])  # 5 per group, then the room left
        self.assertEqual(hidden, 8)
        self.assertEqual(watch.layout(keys, None)[2], 0)

    def test_markdown_groups_with_headings_and_overview(self):
        cur = {(self.TULIP, ELEPHANT): ("20260728", self.TULIP, ELEPHANT), (ELEPHANT, self.CROISSANT): ("20260728", ELEPHANT, self.CROISSANT),
               (STAR, SLOTH): ("20260728", STAR, SLOTH)}
        d = watch.Diff({}, {}, {ELEPHANT: "elephant", self.TULIP: "tulip", self.CROISSANT: "croissant", STAR: "star2", SLOTH: "sloth"}, cur)
        md = watch.render_markdown(d, None, "2026-11-03", max_inline=None, thumbs=False, full_report=None)
        self.assertIn("**By emoji:** 🐘 elephant (2); 1 other combo.", md)
        self.assertIn("### 🐘 elephant (2)\n\n- 🐘 + 🌷 — elephant + tulip · batch 20260728 · [png](https://www.gstatic.com/android/keyboard/emojikitchen/20260728/u1f337/u1f337_u1f418.png)\n"
                      "- 🐘 + 🥐 — elephant + croissant · batch 20260728", md)                   # lead emoji first, URL keeps Google's order
        self.assertIn("### Other combos (1)\n\n- 🌟 + 🦥 — star2 + sloth", md)
        html_out = watch.render_html(d, None, "2026-11-03", max_inline=None, full_report=None)
        self.assertIn("<h3>🐘 elephant (2)</h3>", html_out)
        self.assertIn("<b>By emoji:</b> 🐘 elephant (2); 1 other combo.", html_out)

    def test_truncated_group_says_how_many_more(self):
        cur = {(ELEPHANT, f"1f{i:03x}"): ("20260728", ELEPHANT, f"1f{i:03x}") for i in range(8)}
        cur[(STAR, SLOTH)] = ("20260728", STAR, SLOTH)
        d = watch.Diff({}, {}, {ELEPHANT: "elephant"}, cur)
        md = watch.render_markdown(d, None, "2026-11-03", max_inline=6, thumbs=False, full_report="r.md")
        self.assertIn("- … 3 more with 🐘\n", md)
        self.assertIn("- … and 3 more — see the full report (`r.md`).", md)


class RenderTests(unittest.TestCase):
    def setUp(self):
        cur = {(STAR, SLOTH): ("20260728", STAR, SLOTH), (ELEPHANT, ELEPHANT): ("20260728", ELEPHANT, ELEPHANT)}
        self.d = watch.Diff({}, {}, {}, cur)

    def test_more_line_links_to_the_committed_report(self):
        url = "https://github.com/o/r/blob/main/reports/2026-11-03.md"
        md = watch.render_markdown(self.d, None, "2026-11-03", max_inline=1, thumbs=False, full_report="reports/2026-11-03.md", report_url=url)
        self.assertIn(f"- … and 1 more — see the [full report]({url}).", md)
        html_out = watch.render_html(self.d, None, "2026-11-03", max_inline=1, full_report="reports/2026-11-03.md", report_url=url)
        self.assertIn(f"see the <a href='{url}'>full report</a> (also attached).", html_out)

    def test_more_line_without_a_url_names_the_file(self):
        md = watch.render_markdown(self.d, None, "2026-11-03", max_inline=1, thumbs=False, full_report="reports/2026-11-03.md")
        self.assertIn("- … and 1 more — see the full report (`reports/2026-11-03.md`).", md)
        html_out = watch.render_html(self.d, None, "2026-11-03", max_inline=1, full_report="reports/2026-11-03.md")
        self.assertIn("the full list is attached (<code>reports/2026-11-03.md</code>).", html_out)

    def test_report_link_comes_from_the_actions_environment(self):
        env = {"GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "o/r", "GITHUB_REF_NAME": "main"}
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(watch.report_link("reports/2026-11-03.md"), "https://github.com/o/r/blob/main/reports/2026-11-03.md")
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(watch.report_link("reports/2026-11-03.md"))


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.snap = self.tmp / "snapshot"
        self.snap.mkdir()
        (self.snap / "combos.tsv").write_text("date\tleft\tright\n20201001\t1f31f\t1f9a5\n20241023\t1f44d\t1f937\n", encoding="utf-8")
        (self.snap / "emoji.tsv").write_text("codepoint\tname\n1f31f\tstar2\n1f44d\tthumbsup\n1f937\tshrug\n1f9a5\tsloth\n", encoding="utf-8")
        (self.snap / "meta.json").write_text(json.dumps({"updated": "2026-09-20", "combos": 2, "emoji": 4}), encoding="utf-8")
        self.pb = self.tmp / "pairs.pb"
        # Google now lists: star+sloth (kept), pink heart+sloth (new), elephant+elephant (new); thumbsup+shrug is gone
        self.pb.write_bytes(pair_list(["🌟", "🦥", "🩷", "🐘"], [(1, 2), (3, 2), (4, 4)]))
        self.argv = ["--pairs-file", str(self.pb), "--offline", "--snapshot-dir", str(self.snap),
                     "--reports-dir", str(self.tmp / "reports"), "--out-dir", str(self.tmp / "out"), "--date", "2026-11-03"]

    def test_offline_run_reports_new_removed_and_new_emoji(self):
        self.assertEqual(watch.main(self.argv), 0)
        report = (self.tmp / "reports" / "2026-11-03.md").read_text(encoding="utf-8")
        self.assertIn("gained **2** combos, dropped **1**, and redrew **0**", report)
        # names come from the Unicode database; older Pythons predate U+1FA77, hence the replace
        self.assertIn("Newly supported emoji: 🐘 elephant, 🩷 pink_heart", report.replace("u+1fa77", "pink_heart"))
        self.assertIn("🦥 + 🩷 — sloth + pink heart · sticker image not found yet", report.replace("u+1fa77", "pink heart"))
        self.assertIn("👍 + 🤷 — thumbsup + shrug · last seen in batch 20241023", report)
        self.assertIn("Source: Google's pair list v13.", report)
        combos = (self.snap / "combos.tsv").read_text(encoding="utf-8").splitlines()
        self.assertEqual(combos, ["date\tleft\tright", "20201001\t1f31f\t1f9a5", "\t1f418\t1f418", "\t1f9a5\t1fa77"])  # sorted by pair
        meta = json.loads((self.snap / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual((meta["updated"], meta["combos"], meta["emoji"], meta["source_version"], meta["unresolved"]), ("2026-11-03", 3, 4, 13, 2))
        self.assertIn("20260728", meta["folders"])
        self.assertTrue((self.tmp / "out" / "newsletter.html").read_text(encoding="utf-8").startswith("<!doctype html>"))

    def test_second_run_with_no_changes_sends_nothing(self):
        watch.main(self.argv)
        self.assertEqual(watch.main(self.argv), 0)
        self.assertFalse((self.tmp / "reports" / "2026-11-03.md").read_text(encoding="utf-8") == "")
        self.assertEqual(len(list((self.tmp / "reports").iterdir())), 1)   # no second report

    def test_sticker_lookup_and_sweep(self):
        fake = FakeStickers({path("20260428", PINK_HEART, SLOTH), path("20260728", ELEPHANT, ELEPHANT), path("20260429", STAR, SLOTH)})
        original = watch.Stickers
        watch.Stickers = lambda workers: fake   # noqa: E731
        try:
            argv = [a for a in self.argv if a != "--offline"] + ["--sweep", "20260429"]
            self.assertEqual(watch.main(argv), 0)
        finally:
            watch.Stickers = original
        report = (self.tmp / "reports" / "2026-11-03.md").read_text(encoding="utf-8")
        self.assertIn("gained **2** combos, dropped **1**, and redrew **1**", report)
        self.assertIn("batch 20260428 · [png](https://www.gstatic.com/android/keyboard/emojikitchen/20260428/u1fa77/u1fa77_u1f9a5.png)", report)
        self.assertIn("🌟 + 🦥 — star2 + sloth · batch 20201001 → 20260429", report)
        self.assertIn("Swept folder for redraws: 20260429.", report)
        combos = (self.snap / "combos.tsv").read_text(encoding="utf-8").splitlines()
        # sorted by pair; left/right keep the order Google uses in the file name
        self.assertEqual(combos, ["date\tleft\tright", "20260429\t1f31f\t1f9a5", "20260728\t1f418\t1f418", "20260428\t1fa77\t1f9a5"])

    def test_second_run_with_changes_on_the_same_day_keeps_both_reports(self):
        watch.main(self.argv)
        first = (self.tmp / "reports" / "2026-11-03.md").read_text(encoding="utf-8")
        combos = (self.snap / "combos.tsv").read_text(encoding="utf-8").splitlines()
        (self.snap / "combos.tsv").write_text("\n".join(l for l in combos if "1f418" not in l) + "\n", encoding="utf-8")
        self.assertEqual(watch.main(self.argv), 0)   # the elephant combo looks new again
        self.assertEqual(sorted(p.name for p in (self.tmp / "reports").iterdir()), ["2026-11-03-2.md", "2026-11-03.md"])
        self.assertEqual((self.tmp / "reports" / "2026-11-03.md").read_text(encoding="utf-8"), first)
        self.assertIn("gained **1** combos", (self.tmp / "reports" / "2026-11-03-2.md").read_text(encoding="utf-8"))

    def test_baseline_run_without_snapshot(self):
        for f in self.snap.iterdir():
            f.unlink()
        self.assertEqual(watch.main(self.argv), 0)
        meta = json.loads((self.snap / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual((meta["combos"], meta["emoji"], meta["unresolved"]), (3, 4, 3))
        self.assertFalse((self.tmp / "reports").exists())


if __name__ == "__main__":
    unittest.main()

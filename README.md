# Emoji Kitchen watch

A weekly GitHub Action that emails you when Google adds, removes, or redraws
combos in Gboard's **Emoji Kitchen**.

Google publishes no changelog for Emoji Kitchen. The stickers are static PNGs on
`gstatic.com`, but Google does publish the list of valid pairs that powers the
"emoji kitchen" widget in Google Search:

```
https://www.google.com/logos/fnbx/emoji_kitchen/emoji_kitchen_pairs.<N>.pb
```

It is a small protobuf (about 260 KB) naming every supported emoji and every
valid pair. Google replaces it with a new version number every few months and
leaves the old ones online (v9 June 2025, v10 September 2025, v11 December
2025, v12 March 2026, v13 September 2026). This repo downloads the newest
version every week, diffs it against the previous week's snapshot at the combo
level, looks up the sticker image of every new combo, and sends a newsletter
shaped like this:

```
Summary: Since the 2026-06-05 snapshot, Google's Emoji Kitchen gained 616 combos,
dropped 4, and redrew 42 existing ones. It now offers 147,000 combos across 619
emoji. Newly supported emoji: 🦨 skunk. Sticker batch: 20260128. Source: Google's
pair list v13 (last modified Tue, 01 Sep 2026 19:30:00 GMT).

New combos (616)
  🦨 + 🃏 — skunk + black joker · batch 20260128
  🦨 + 🆒 — skunk + cool · batch 20260128
  …

Removed combos (4)
  👗 + 🌺 — dress + hibiscus · last seen in batch 20251029
  …

Redrawn combos (42)
  🌟 + 🥑 — star2 + avocado · batch 20201001 → 20260429
  …
```

The HTML email shows a thumbnail of every new/redrawn sticker (the images are
served straight from Google). Nothing is sent when nothing changed.

Until November 2026 this repo diffed the `metadata.json` that
[emojikitchen.dev](https://emojikitchen.dev) hand-maintains. That file is
rebuilt by brute-force scanning sticker folders its maintainer knows about, and
it had silently missed three whole batches (July 2025, April 2026, July 2026:
about 4,100 combos and two emoji, 🐘 and 🫏). Google's own pair list has no such
gap, so the first run after switching reports that backlog as new.

## Files

| Path | What it is |
| --- | --- |
| `watch.py` | The script. Standard library only. `python watch.py` |
| `tests/test_watch.py` | Offline tests with a hand-built pair list: `python -m unittest discover -s tests` |
| `.github/workflows/watch.yml` | Weekly schedule + manual trigger; downloads, diffs, commits, notifies |
| `snapshot/combos.tsv` | Last known state: one line per combo (`date  left  right`, ~3 MB, sorted so each weekly commit is a readable diff). `date` is the gstatic folder holding the sticker; empty until it has been located |
| `snapshot/emoji.tsv` | Codepoint → name for every supported emoji (used to name combos that disappear) |
| `snapshot/meta.json` | When the snapshot last changed, its counts, the pair-list version it came from, and every sticker folder seen so far |
| `reports/YYYY-MM-DD.md` | Full, uncapped report for each week that had changes (permanent changelog) |
| `out/` | The newsletter for the current run (Markdown + HTML); not committed |

## Setup

1. Create a repo from these files (private is best; see *Gotchas*).
2. Push. Go to **Actions → Emoji Kitchen watch → Run workflow**. The first run
   only records a baseline snapshot and commits it — no email yet. (A fresh
   baseline stores every combo without a sticker folder; folders are looked up
   for combos added from then on.)
3. Pick how you want to be notified (next section). Both can be on at once.

To test the notification path end to end, delete any one line from
`snapshot/combos.tsv`, commit, and run the workflow again: it will report that
combo as "new" and send the newsletter.

## How the email gets sent

### Option A — SMTP (any mailbox or provider)

The workflow uses [`dawidd6/action-send-mail`](https://github.com/dawidd6/action-send-mail),
which speaks plain SMTP, so it works with anything that gives you SMTP
credentials. The step is skipped automatically until you add the secrets
(**Settings → Secrets and variables → Actions**):

| Secret | Value |
| --- | --- |
| `SMTP_SERVER` | e.g. `smtp.gmail.com` |
| `SMTP_PORT` | optional, default `465` (implicit TLS). Use `587` for STARTTLS |
| `SMTP_USERNAME` | login, usually your address |
| `SMTP_PASSWORD` | password or app password |
| `MAIL_TO` | where to send (comma-separate several) |
| `MAIL_FROM` | optional, defaults to `SMTP_USERNAME` |

Which credentials to use:

- **Gmail / Google Workspace** — turn on 2-Step Verification, then create an
  [App Password](https://myaccount.google.com/apppasswords) and use it as
  `SMTP_PASSWORD`. Your normal password will not work.
- **Fastmail, iCloud, Proton (via Bridge), most others** — same idea: an
  app-specific password on port 465 or 587.
- **Transactional providers** (Brevo, Mailgun, SendGrid, Postmark, Resend, …) —
  all expose an SMTP relay with its own username/API key; their free tiers
  cover a weekly email many times over. This is the cleanest option if you
  don't want a personal mailbox's credentials in GitHub secrets.

The email is the HTML newsletter with inline thumbnails, plus the full Markdown
report attached in case a big batch lands (the inline list is capped at 100
items per section; change `--max-inline` in `watch.py` if you want more).

### Option B — let GitHub send the email (zero configuration)

The workflow also opens a GitHub **issue** containing the newsletter and assigns
it to you. GitHub emails assignees about new issues by default, so you get the
newsletter in your inbox with no SMTP setup at all, and the issues double as a
browsable changelog with the sticker images rendered inline.

- Works out of the box for a repo under your own account.
- For a repo in an organization, set a repository **variable**
  `ISSUE_ASSIGNEE` to your username.
- To turn this off, set the variable `OPEN_ISSUE` to `false`.
- Make sure email is enabled for *Participating and @mentions* in your
  [GitHub notification settings](https://github.com/settings/notifications)
  (it is by default).

### Option C — an email API instead of SMTP

If you'd rather use a provider's HTTP API, replace the SMTP step with a `curl`
call. For example, with a Resend API key in `RESEND_API_KEY` and a verified
sending domain:

```yaml
- name: Email the newsletter (Resend)
  if: steps.diff.outputs.changed == 'true'
  run: |
    jq -n --arg from "kitchen@yourdomain.example" --arg to "$MAIL_TO" \
          --arg subject "${{ steps.diff.outputs.subject }}" \
          --rawfile html out/newsletter.html \
          '{from:$from, to:[$to], subject:$subject, html:$html}' \
    | curl -sS -X POST https://api.resend.com/emails \
        -H "Authorization: Bearer $RESEND_API_KEY" -H "Content-Type: application/json" -d @-
  env:
    RESEND_API_KEY: ${{ secrets.RESEND_API_KEY }}
    MAIL_TO: ${{ secrets.MAIL_TO }}
```

Anything else that accepts a webhook — Slack, Discord, ntfy, a Zapier/Make
flow that forwards to email — can be wired in the same way, using
`out/newsletter.md` as the body.

## How the diff works

- **The pair list.** `watch.py` remembers the version number it last saw
  (`source_version` in `snapshot/meta.json`) and probes upward from it until
  three consecutive versions are missing, then uses the newest one found. The
  file is decoded without any protobuf library; the schema is three repeated
  messages: emoji (string + id), pairings (id + packed partner ids), and
  categories (name + first emoji id).
- A **combo** is an unordered pair of emoji codepoints. **New** = pair present
  now, absent last week. **Removed** = the reverse: Google took it out of the
  list, and in the cases checked so far the PNG disappears too.
- The summary also calls out emoji that gained or lost Emoji Kitchen support
  entirely. New emoji are named from the Unicode database (`🐘` → `elephant`);
  names from the old emojikitchen.dev data are kept for everything else.
- **Sticker images.** The pair list says nothing about where a sticker lives.
  Google stores each one under one codepoint order (`u1f31f_u1f951.png`) in a
  folder named after its publish date, so for every *new* combo the script
  sends HEAD requests for both orders to each known folder, newest first, until
  one answers 200. Folders are remembered in `meta.json`; if a combo is in none
  of them, the script probes day-named folders between the newest known folder
  and today to discover the new one, then tries again. Combos whose image still
  cannot be found are listed without a thumbnail and retried the next week.
- **Redrawn** combos (Google republishing an existing sticker in a newer folder)
  are detected only when you ask for a **sweep**: run the workflow by hand with
  the `sweep` input set to one or more folders (`20260728,20260428`), or locally
  `python watch.py --sweep 20260728`. A sweep makes two HEAD requests per combo
  per folder (about 300,000 for the full catalogue), which is why it is not
  part of the weekly run. The newsletter tells you when a new folder appeared,
  so you know when a sweep is worth running. After switching from emojikitchen.dev
  the folders `20250728`, `20260428` and `20260728` have never been swept.
- Google refreshes the pair list every three to six months, so most weeks the
  run finishes with "No changes" and sends nothing.

## Gotchas

- **Scheduled workflows in public repos are switched off after 60 days without
  repository activity.** Google's data can go quiet for longer than that, so
  keep this repo **private** (the rule doesn't apply there) or expect to
  re-enable the workflow occasionally.
- The pair-list URL is an undocumented static asset of the Google Search
  widget. It has been stable since at least 2023 (an unversioned
  `emoji_kitchen_pairs.pb` from July 2023 is still online), but if Google moves
  or renames it the run fails loudly ("No pair list found") rather than
  silently reporting "no changes". The pair list also trails the image server:
  the April 2026 stickers only appeared in it in September 2026.
- Cron runs in UTC and GitHub may delay it by some minutes on busy days; the
  exact hour doesn't matter here.
- The commit step needs push access to the default branch; if you protect the
  branch, allow the `github-actions[bot]` to bypass or push to a side branch.
- Issue bodies are limited to 65,536 characters, which is why the newsletter is
  capped at 100 combos per section and the full list lives in `reports/`; the
  "and N more" line links to that file on GitHub (the report is committed and
  pushed before the issue is opened).

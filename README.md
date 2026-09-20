# Emoji Kitchen watch

A weekly GitHub Action that emails you when Google adds, removes, or redraws
combos in Gboard's **Emoji Kitchen**.

Google publishes no changelog for Emoji Kitchen. The stickers are static PNGs on
`gstatic.com`, and the only public inventory of them is the `metadata.json`
that [emojikitchen.dev](https://emojikitchen.dev) rescans and commits to
[`xsalazar/emoji-kitchen-backend`](https://github.com/xsalazar/emoji-kitchen-backend).
That file is a 95 MB single-line JSON blob, so its git history tells you *that*
something changed but not *what*. This repo fills the gap: it downloads the
file every week, diffs it against the previous week's snapshot at the combo
level, and sends a newsletter shaped like this:

```
Summary: Since the 2026-06-05 snapshot, Google's Emoji Kitchen gained 616 combos,
dropped 4, and redrew 42 existing ones. It now offers 147,000 combos across 619
emoji. Newly supported emoji: 🦨 skunk. Sticker batch: 20260128.

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

## Files

| Path | What it is |
| --- | --- |
| `watch.py` | The script. Standard library only. `python watch.py metadata.json` |
| `.github/workflows/watch.yml` | Weekly schedule + manual trigger; downloads, diffs, commits, notifies |
| `snapshot/combos.tsv` | Last known state: one line per combo (`date  left  right`, ~3 MB, sorted so each weekly commit is a readable diff) |
| `snapshot/emoji.tsv` | Codepoint → name for every supported emoji (used to name combos that disappear) |
| `snapshot/meta.json` | When the snapshot was taken, and its counts |
| `reports/YYYY-MM-DD.md` | Full, uncapped report for each week that had changes (permanent changelog) |
| `out/` | The newsletter for the current run (Markdown + HTML); not committed |

## Setup

1. Create a repo from these files (private is best; see *Gotchas*).
2. Push. Go to **Actions → Emoji Kitchen watch → Run workflow**. The first run
   only records a baseline snapshot and commits it — no email yet.
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

- A **combo** is an unordered pair of emoji codepoints. Google stores each
  sticker under one ordering (`u1f31f_u1f951.png`), and `metadata.json` lists
  it under both emoji; the script collapses that to one key per pair.
- Google sometimes republishes an existing combo in a newer dated batch.
  `metadata.json` keeps every revision with an `isLatest` flag; the snapshot
  stores only the latest one per pair. A pair whose latest batch date changed
  is reported as **redrawn**; it is *not* counted as new.
- **New** = pair present now, absent last week. **Removed** = the reverse —
  Google deleted the PNGs and emojikitchen.dev pruned them. Pruning of *old*
  revisions of a pair that still exists is ignored as noise.
- The summary also calls out emoji that gained or lost Emoji Kitchen support
  entirely.
- Upstream is refreshed by its maintainer roughly every one to three months,
  so most weeks the run finishes with "No changes" and sends nothing.

## Gotchas

- **Scheduled workflows in public repos are switched off after 60 days without
  repository activity.** Upstream data can go quiet for longer than that, so
  keep this repo **private** (the rule doesn't apply there) or expect to
  re-enable the workflow occasionally.
- Cron runs in UTC and GitHub may delay it by some minutes on busy days; the
  exact hour doesn't matter here.
- The commit step needs push access to the default branch; if you protect the
  branch, allow the `github-actions[bot]` to bypass or push to a side branch.
- Issue bodies are limited to 65,536 characters, which is why the newsletter is
  capped and the full list lives in `reports/`.
- If emojikitchen.dev ever moves or renames `metadata.json`, the download
  step fails loudly rather than silently reporting "no changes".

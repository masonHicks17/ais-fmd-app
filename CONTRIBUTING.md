# Contributing — Fall 2026 treasury team

How we build the AIS Financial Management app together this semester. Read this
once end to end before your first commit. It is short on purpose.

**The model in one line:** you work on your own branch in the team repo, prove
your change in the sandbox, and open a pull request. Your partner reviews it,
and the treasurer approves and merges it. Nobody can push to
`sandbox/mvp-rebuild` or `main` directly — GitHub blocks it — so every change
arrives this way.

---

## 1. Get set up (do this before your first team session)

You need: Git, Python **3.11, 3.12 or 3.13** (CI tests all three), and a
GitHub account. Send the treasurer your GitHub username; you will get an email
invitation to the repo. Accept it before you push anything.

`main` still holds the old app. **All work this semester branches from and
targets `sandbox/mvp-rebuild`.**

### Just want to run it?

```bash
git clone -b sandbox/mvp-rebuild https://github.com/treasuryufais/ais-fmd-app.git
cd ais-fmd-app
```

No Git? Download the ZIP instead:
<https://github.com/treasuryufais/ais-fmd-app/archive/refs/heads/sandbox/mvp-rebuild.zip>

### Going to contribute?

Once you have accepted the invitation, the clone above is all you need. You
push your branches straight to the team repo; there is no fork and only one
remote, `origin`.

Already made a fork earlier in the semester? Point your clone at the team repo
and push your branches there:

```bash
git remote set-url origin https://github.com/treasuryufais/ais-fmd-app.git
git remote remove upstream
git fetch origin
git push -u origin <your-branch>
```

Then delete the fork on GitHub (your fork → Settings → Delete this repository).

### Create a virtual environment and install

Windows (PowerShell or Git Bash):

```bash
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

macOS / Linux:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Install **only** `requirements.txt`. Never `pip install supabase` or `anthropic`
into this environment — their absence is one of the three things that make the
sandbox physically unable to touch the real database or spend API money.

### Prove your setup works

```bash
# Windows: .venv\Scripts\python   macOS: .venv/bin/python
.venv\Scripts\python -m pytest
.venv\Scripts\python -m streamlit run app.py
```

Tests should finish green (a few are skipped on purpose). The app opens at
<http://localhost:8501> with a **SANDBOX** banner across the top and seeds
itself with several semesters of fake data. If you see that banner, you are set up.

---

## 2. The sandbox is where you prove things

Everything you build runs against a local SQLite file full of generated data.
It has no network access to our database, and it cannot reach an AI service.
That is what makes it safe for everyone to experiment freely.

- **Switch roles** from the sidebar to see a page as a Member, Officer (VP),
  Treasurer or Admin. Test your feature as every role that can reach it.
- **Reset & reseed data** in the sidebar puts the database back to a known state.
  Run it before recording a demo, so what you show is reproducible.
- The seed data has planted problems (an unbalanced statement, uncategorized
  rows, duplicate-looking purchases) so diagnostic pages have something to find.

---

## 3. Data rules — non-negotiable

This repository is **public**. Treat everything you push as published forever.

1. **No real financial data, anywhere in Git.** No bank statements, Venmo
   exports, membership rosters, member names, emails, UFIDs or card numbers —
   not in code, tests, fixtures, screenshots, PR descriptions or issues. (The
   one exception is `ais_fmd/config/card_roster.json`, which maps the last four
   digits of org cards to committees. The treasurer maintains it; nobody else
   adds to it.)
2. **No real data on your machine for this project.** You do not need it.
   If your feature needs to be checked against real statements, write it so it
   runs on the sandbox, and ask the treasurer to run it on the real data.
3. **No keys or secrets.** If you ever see a Supabase URL, key or password,
   tell the treasurer and do not copy it anywhere. `tests/test_no_secrets.py`
   fails the build if one reaches the working tree.
4. **Do not paste real data into AI tools.** Sandbox data is fine.

---

## 4. Rules about money

Some code decides *which committee a dollar is booked to*. Changing that moves
real money between budgets, so those decisions belong to the treasurer, not to
a pull request.

- `tests/test_treasury_decisions.py` holds one test per treasury ruling. **Never
  edit or delete one of those tests to make your change pass.** If your change
  breaks one, stop and ask.
- `ais_fmd/config/categories.py`, `ais_fmd/config/card_roster.json` and
  `ais_fmd/config/spend_policy.json` are
  treasurer-owned. Propose changes in your PR description; do not make them.
- Never add `"membership"` to `MEMO_COMMITTEE_KEYWORDS`. It appears in dues memos
  and would misfile dues income as a Membership expense.

---

## 5. Day-to-day workflow

### Start a piece of work

```bash
git fetch origin
git checkout -b <team>/<short-description> origin/sandbox/mvp-rebuild   # e.g. vp/committee-home
```

Team prefixes: `vp/`, `cat/`, `model/`, `platform/`, `qa/`, `close/`, `ux/`, `ai/`.

Everyone's branches live in the same repo, so **only push to branches you
created**, and never delete someone else's. GitHub deletes a branch
automatically once its pull request merges.

**One branch per person per slice of work.** Pairs split a feature into pieces
and each person opens their own pull requests. Small PRs get reviewed in a day;
2,000-line PRs sit for a week. Aim for under ~400 changed lines.

### Stay current

At least twice a week, bring in what has been merged since you branched:

```bash
git fetch origin
git merge origin/sandbox/mvp-rebuild
```

Fix any conflicts, re-run the tests, commit.

### Before you open a pull request

- [ ] `pytest` is green locally.
- [ ] New logic has new tests. Business logic lives in `ais_fmd/domain/` and is
      tested without Streamlit; pages are tested headlessly — copy the patterns
      in `tests/test_views.py`.
- [ ] You **restarted Streamlit** and checked the page by hand, as each role that
      can reach it (see the traps below — the app does not reload package code).
- [ ] You captured a screenshot or short recording from the sandbox.

### Open the pull request

```bash
git push -u origin <team>/<short-description>
```

- On GitHub, click **Compare & pull request**. Set the base to
  **`sandbox/mvp-rebuild`** (not `main`).
- Fill in the template — it asks how you proved the change in the sandbox.
- Ask **your partner** to review it first. The treasurer is added as a
  reviewer automatically (`.github/CODEOWNERS`).
- The **Merge** button stays grey until the tests are green **and** the
  treasurer approves. A partner's approval alone does not unlock it — that is
  intended, not a problem with your PR.
- Pushing new commits after an approval clears it, so the final version is the
  one that gets approved.
- The treasurer merges with **Squash and merge**, so your branch becomes one
  commit on `sandbox/mvp-rebuild`. Then start your next slice from a fresh
  branch (see "Start a piece of work").

### Shared files — coordinate before editing

These are touched by several teams. Keep edits small and mention them in your PR:

| File | Why it collides |
| --- | --- |
| `app.py` (`PAGES` list) | Every new page is registered here |
| `ais_fmd/ui/shell.py` | Shared page components — add, don't rewrite |
| `ais_fmd/data/backend.py`, `sqlite_backend.py`, `supabase_backend.py` | Any new data method must be added to all three |
| `ais_fmd/data/repositories.py` | The single caching layer |

---

## 6. Traps that have bitten this project before

Full list in `HANDOFF.md` §9. The ones you will hit in week one:

1. **Streamlit does not hot-reload package code.** Editing anything under
   `ais_fmd/` needs a server restart (Ctrl+C, run again). Editing only a page
   file does not. This will make you think your fix did not work.
2. **`$` renders as math.** Two dollar signs in one `st.markdown` string turn
   the text between them into an equation. Use `shell.say(...)` or
   `shell.notify(...)` for any text containing money.
3. **Views must use absolute imports** — `from ais_fmd import auth`, never
   `from . import ...`.
4. **A pandas NaN is truthy.** `value or ""` does not replace it. Use `pd.isna()`.
5. **`domain/` never imports Streamlit.** A test enforces it. Keep calculations
   there and layout in `views/`.

---

## 7. Using AI coding tools

Allowed and encouraged — much of this codebase was built that way. Two rules:
you must be able to explain every line in your PR when asked, and you never
give an AI tool real data. Point your tool at `HANDOFF.md` §9 and §11 before it
edits anything; it will save you both an afternoon.

---

## 8. Where to learn the codebase

In this order: `README.md` (safety model) → `HANDOFF.md` §3 (architecture) and
§9 (traps) → `ais_fmd/config/categories.py` (the committee vocabulary) → the
module your team owns → its tests. Your team brief in
`docs/fall-2026-projects.md` lists the exact files.

Stuck for more than 30 minutes? Ask in the team chat with what you tried. That
is expected, not a failure.

---

## 9. For the treasurer: repo settings that enforce this

Contributors don't need this section. It records how GitHub is configured, so
the next treasurer can check or rebuild it.

- **Collaborators** (Settings → Collaborators): each contributor has **Write**,
  never Admin. Remove them at the end of the semester.
- **Ruleset** (Settings → Rules → Rulesets), active on `sandbox/mvp-rebuild`
  and `main`:
  - require a pull request, 1 approval, **review from Code Owners**, dismiss
    stale approvals on new commits, all conversations resolved
  - require status checks `test (3.11)`, `test (3.12)`, `test (3.13)`
  - block force pushes and deletions
  - bypass list: the treasurer only, for emergencies
- **Pull requests** (Settings → General): squash merging only;
  "Automatically delete head branches" on.
- **CODEOWNERS** (`.github/CODEOWNERS`): replace the username when the
  treasurer changes.

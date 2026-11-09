# Simplification punch list — from the Oct 31 meeting

Goal: fewer tabs, bigger and more obvious controls, less text. Things you use
every day are one click away; things you do once a semester can take two or three.

Ground rules from the meeting:
- **Don't touch the VP pages** (Mark and the treasurer are still working on them).
- **Hold off on Transactions** (both the page and the Review Queue). Teammates have unpushed changes there.
  Anything below marked ⏸ waits until those land.

## Proposed navigation

| Now (11 tabs) | After |
|---|---|
| Home, Dashboard | **Home**: one page |
| Transactions (+ Review Queue) | **Transactions**: statement upload → review queue → table |
| Dues (+ Roster), Planner | **Semester Setup**: Dues · Roster · Budgeting, as sub-tabs |
| Treasury, Reconciliation | **Treasury**: Budgets · Terms · Reconciliation · Export |
| Assistant (+ Help), Alerts & Data Quality, Settings, My Committee | Not discussed. Proposal: fold Alerts into Home, put Settings under Treasury, keep Assistant and My Committee as they are |

## Punch list

### A. Navigation and consolidation
1. **Merge Home and Dashboard** into a single Home page. Remove the Dashboard tab. *(Home.py, Dashboard.py, app.py)*
2. **Create a Semester Setup tab**, named for turnover / start of semester, holding Dues, Roster and Budgeting as sub-tabs.
   Retire the separate Dues and Planner tabs. *(DuesHub.py, Planner.py, app.py)*
3. **Rename "Scenario Planner" to "Budgeting"** everywhere, so it isn't confused with the planning that happens in Treasury. *(Planner.py, labels)*
4. **Move Reconciliation into Treasury** as a sub-tab. Keep what it does: re-upload a statement at term end to confirm no transactions are missing. *(Reconciliation.py, Treasury.py)*
5. **Remove "Upload statement" from Treasury.** It moves to Transactions in item 13 ⏸. Treasury keeps Budgets, Terms and Export, the Supabase backend tables. *(Treasury.py:69)*
6. **Remove "Demo".** The treasurer says they've asked for this twice. ❓ *Confirm what "Demo" refers to. The only match in the code is the sandbox "Demo sign-in" picker in `auth.py`.*
7. **Decide what to do with the remaining tabs** (Alerts & Data Quality, Settings, Assistant/Help) using the proposal in the table above.

### B. Home (the merged dashboard)
8. **Lead with spend-pace alerts.** Flag any committee or budget line burning faster than the semester clock, meeting food first.
   The treasurer: "that's all I care about on the dashboard." Keep the semester-progress (0–100%) gauge; they liked it.
9. **Redesign "Where the money went".** The waterfall chart confused everyone. Use a clearer income-vs-spend view,
   **show sponsorship income on its own**, and **label every bar**. *(ui/charts.py `waterfall`/`flow_waterfall`)*
10. **Show committees against their budget, not net income.** Committees don't bring in money, so show a budget-vs-spent view. *(committee view)*
11. ✅ **Fix the orange that renders as white** and is hard to read. *Done Oct 31: on the committee view, over-budget terms are now
    orange in "This term vs. past terms" and "Across semesters", and the near-white budget bars are now a visible grey.
    (ui/charts.py `trend_bars`, ui/committee_charts.py `same_point_bars`)*

### C. Dues and Roster
12. **Remove the "Expected members" slider** from Dues. It was supposed to be gone already, and the roster is now the source of truth. Then tidy the
    Dues and Roster split, which "is kind of a mess". *(Dues.py:62–90)*

### D. Transactions ⏸ (wait for teammates' changes)
13. **Put the whole workflow in one place**: Wells Fargo statement upload → AI categorization → Review Queue → table.
14. **Clean up the description column.** Strip "Purchase authorized on", transaction and handshake numbers, phone numbers and
    the account name. Leave the merchant, plus a separate authorized-on date.
15. **Add a Cardholder column** that maps the card's last 4 digits to a person (Maria, Nolan, Connell, …).
16. **(Stretch) Derive the store location** from the store number, e.g. "1560", to flag purchases outside Gainesville (trips, the Springs).
17. **Have the Review Queue show the AI's reasoning** whenever the AI made or passed on a decision. Partly built, so check it.

### E. App-wide polish
18. **Cut and restructure text.** Too much prose and too much scrolling, worst on the committee views. Turn it into
    visuals, short captions or expanders.
19. **Make buttons bigger and more prominent** across the app.
20. **Phone install.** Mobile already looks better than desktop. Add a web-app manifest and icon so "Add to Home Screen" in
    Safari installs it like a native app.

### F. Not for this pass (teammates or later)
- **Wrong bank balance (−$22k).** Last semester isn't categorized yet, and about $10k cash plus incoming sponsorship should be close. The treasurer is investigating.
- **Event-level categorization.** Match transactions to events using an event schedule (Google Sheets or Calendar) with confidence
  rules and Haiku. Owned by the treasurer. This also feeds Budgeting's "what did past events cost".
- **Fun stats**, e.g. how much membership spends on drinks. Depends on the categorization above.

## Suggested order
11 → 12 → 6 → 1 → 8–10 → 2–3 → 4–5 → 7 → 19 → 18 → 20, then section D once Transactions is clear.

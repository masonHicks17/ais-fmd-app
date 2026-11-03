# Questions for treasury

**Status: answered 2026-08-24.** Four of five resolved; 5a still open.

The questions are kept below as they were asked, because each answer only means
something next to the question it answers. What was decided:

| # | Question | Answer | Where it landed |
| --- | --- | --- | --- |
| 1 | Dues rates per term | Last term \$35 / \$52.50. **This term (Fall 2026) \$50 / \$65.** Formal payments will land near those amounts; real dues are an exact figure | `dues.CONFIRMED_DUES_RATES`, applied by `scripts/apply_treasury_rates.py` |
| 2 | Who holds the seven cards | Cards move with new VPs; numbers to follow | `quality.check_card_roster_era` — no roster guessed, see below |
| 3 | Which committee a reimbursement hits | **The committee it repaid.** "If someone spends money on a personal card and gets reimbursed then it was a committee expenditure" | `predicates.rule_reimbursement` replaces `rule_refund` |
| 4 | Venmo dues arriving net of fees | Venmo is **deprecated** going forward; count historical rows net of fees | `dues.ACCEPT_VENMO_NET_OF_FEES = True` |
| 5a | Disputed purpose mappings | **Still open** — "I'm not sure, keep it as an open question" | unchanged; `DISPUTED_PURPOSE_MAPPINGS` still reports it |
| 5b | Are memos enough to book a transfer | **Yes.** "Absolutely look at the memos they will clarify it well" — and a dues keyword **in addition** to the amount rule | `predicates.MEMO_COMMITTEE_KEYWORDS`, plus `rule_dues_memo` |

Each of these is asserted in `tests/test_treasury_decisions.py`, one test per
ruling, so reversing a decision fails the test that names it.

### Two things the answers did not settle

**The seven cards were not guessed.** Treasury said to use a best guess, and
there is no honest one available. The only current-era evidence about those
cards is the app's own `budget_category`, which the categorizer wrote itself —
inferring a roster from it would train the categorizer on its own output. The
2022-23 human labels are a different cohort with different card numbers. So the
roster is unchanged and a new check reports the real risk instead: the four
*documented* cards were confirmed for the 2024-2026 cohort, and Fall 2026 began
a new one. Card 8408 matters most, because `rule_consulting` treats it as a
certainty that outranks even a human's merchant mapping.

**"Last term" was read as Spring 2026.** This changes no numbers — \$35/\$52.50
was already the assumed default for every term — so the only consequence of
being wrong is which row shows as confirmed on the Data Quality page. Earlier
terms are deliberately still unconfirmed; Spring 2025 in particular had both
rate pairs circulating and nobody has said which counted.

---

Five questions. Each one is currently blocking something specific, and each is
answerable from records or memory without touching the app. They are ordered by
what they unblock, not by how hard they are.

Numbers below come from the 892 real Wells Fargo transactions covering
2024-07-09 to 2026-07-08. Reproduce any of them with:

```bash
.venv/Scripts/python scripts/verify_dues_schedule.py
```

---

## 1. What did each term actually charge for dues? (most urgent)

**Why it matters.** Dues are matched on an *exact* amount. If a term charged a
rate the app doesn't know, dues for that term stop being recognised — silently.
No error appears; the payments just pile up in the review queue and dues income
looks like it collapsed.

The app currently assumes **\$35.00 / \$52.50** for every term, which are the
Fall 2024 rates. The VP Treasury Handbook shows the rate changing nearly every
term (\$20/\$40 → \$25/\$40 → \$30/\$50 → \$35/\$52.50).

**What the data shows.** There are **59 incoming Zelle/Venmo payments totalling
\$1,870** that sit at documented handbook rates and are currently uncategorized:

| Term | \$20 | \$25 | \$30 | \$50 |
| --- | --- | --- | --- | --- |
| Fall 2024 | 3 | 2 | 2 | — |
| Spring 2025 | 4 | 1 | **27** | **9** |
| Fall 2025 | — | 3 | — | 2 |
| Spring 2026 | 2 | — | 4 | — |

Spring 2025 stands out: 27 payments of \$30 and 9 of \$50, which is exactly the
handbook's "\$30 / \$50" pair. They read like dues — individual people paying by
Zelle — but we are not going to book \$1,870 of income on a pattern match.

**The question:** for each term below, what were the two dues rates?

| Term | Rates charged |
| --- | --- |
| Summer 2024 | |
| Fall 2024 | |
| Spring 2025 | |
| Summer 2025 | |
| Fall 2025 | |
| Spring 2026 | |
| Summer 2026 | |
| **Fall 2026** (starts 2026-08-15) | |

Fall 2026 is the one that matters going forward — it starts now, and if the rate
changed, dues stop categorizing on day one.

**A complication worth flagging.** Spring 2025 appears to have had **both** rate
pairs in circulation: 54 payments of \$35 and 4 of \$52.50 *alongside* 27 of
\$30 and 9 of \$50. We tested recording Spring 2025 as "\$30 / \$50" and the
number of recognised dues payments went **down**, from 245 to 223 — it picked up
the \$30s and lost the \$35s.

So this is not a matter of swapping one pair for another. Either the term
genuinely accepted several amounts (early-bird vs. late, new vs. returning,
semester vs. full-year), or some of those payments are not dues at all.

**Please answer explicitly:** for each term, list *every* amount that counted as
dues, not just the headline rate. The app accepts any number of amounts per
term, so "30, 35, 50 and 52.50 were all valid in Spring 2025" is a perfectly
usable answer — and guessing at this would move real income between committees.

---

## 2. Who holds these seven cards?

Four cards have a documented owner. Seven appear in real statements with none:

**0153, 7757, 9309, 3444, 7193, 1113, 5535**

Every purchase on an unknown card loses its strongest clue about which committee
it belongs to, so those rows land in the review queue by default. History
suggests 0153 is mostly meeting food, but that is inferred from the app's own
past guesses, not from anything anyone confirmed — so it is not usable as
evidence.

For reference, the four that are known: 8408 (Consulting VP), 8313 and 5718
(Membership VPs), 3568 (President).

**The question:** for each of the seven, who held it and which committee's
spending was it for? "Retired, no idea" is a useful answer too — it tells us to
stop trying.

---

## 3. When someone is reimbursed, which committee should it hit?

**Why it matters.** This is the single largest source of disagreement between
the app and the historical ledgers — about half of all measured mismatches.

The app books every outgoing Venmo/Zelle to **"Refunded"**, a ledger account
that sits outside budget-vs-actual. The 2023 treasurer instead booked a
reimbursement to **the committee whose expense it repaid**, so it counted
against that committee's budget. **18 labeled rows disagree** on exactly this.

These are different claims about what a budget means. If the 2023 approach was
right, committee budgets today understate what committees actually spent,
because reimbursed spending is excluded.

**The question:** when a member fronts money for a committee and gets paid back,
should that repayment count against that committee's budget, or sit outside it?

---

## 4. Should dues paid over Venmo count, given Venmo takes a cut?

Venmo deducts a fee, so dues paid that way arrive short. The historical amounts
on record are **\$24.43, \$29.34 and \$39.14** against gross rates of \$25, \$30
and \$40 — roughly 2.2% light.

Because the app matches an exact amount, these currently don't register as dues
at all. No Venmo data has been imported yet, so nothing is affected today, but
it will be on the first Venmo import.

The app can accept an amount slightly below the expected rate on Venmo rows
only. It is **switched off** right now, because booking income at an amount
nobody explicitly authorised is a treasurer's decision.

**The question:** should a Venmo payment that arrives ~2% short of the dues rate
count as dues paid in full? And is the member considered paid up, or do they owe
the difference?

---

## 5. Two smaller mapping questions

**5a. Two purpose mappings that contradict the reference table.**

| Purpose | App books it to | Reference table says | Affected |
| --- | --- | --- | --- |
| Professional Development | 7 — Consulting | 10 is Prof. Development | 7 tx, \$1,391.98 |
| Food & Drink | 5 — Membership | not documented anywhere | 82 tx, \$7,808.28 |

The original behaviour was kept so historical figures wouldn't shift. Food &
Drink is the larger exposure by a wide margin. Are both correct as they stand?

**5b. Incoming payments with a memo.** **124 payments totalling \$18,802** are
members sending money in by Zelle or Venmo and are currently uncategorized. The
app deliberately refuses to learn a rule from a person's name — a rule keyed on
one member would mis-categorise everything they ever pay — but the memo line
often says what the money was for:

| Memo mentions | Rows | Total |
| --- | --- | --- |
| merch / crewneck / hoodie / t-shirt | 13 | \$580 |
| road trip / St. Augustine | 13 | \$1,030 |
| semi / formal / ticket | 5 | \$345 |
| headshot | 6 | \$35 |

**The question:** should a memo saying "merch" or "road trip" be enough to book
the payment to that committee automatically? And for headshots — Professional
Development or Membership?

Note this only accounts for about \$2,000 of the \$18,800. The rest carries no
memo at all, and no rule will ever recover those; they need a human or a
different source of truth.

---

## What happens with the answers

1 and 2 are entered as data and take effect immediately — no code change. 3, 4,
and 5 are one-line configuration changes each, but each one moves real money
between committees, which is why none of them has been decided by inference.

---

# Round 2 — from the real Fall 2026 statement, 2026-09-08

Found by running `Checking (4).csv` (171 rows, 2026-07-01 .. 2026-09-04) through
the full pipeline. Nothing here was decided by inference; each item moves real
money or changes a real total, so each needs an answer.

## 6. Two $500 Zelles are counted as income, and probably should not be

```
2026-07-01   +500.00   ZELLE FROM [TREASURER] ... VENMO REIMBURSEMENT
2026-07-01   +500.00   ZELLE FROM [TREASURER] ... VENMO TRANSFER
```

Both memos name Venmo, and both come from the treasurer rather than a member.
That reads as the organisation's own money moving from the Venmo account into
checking — not new revenue.

**Why it matters:** $1,000 is **11% of this statement's $8,895 of incoming
money**. Booked as income it overstates revenue by that much, and because the
Venmo side is a separate account, the same dollars can be counted twice.

**The question:** are these internal transfers (committee 3, Transfers) or
genuine income? If transfers, is there a memo convention we can rely on going
forward, now that Venmo is being retired?

## 7. There is a dues rate the schedule does not know about

Confirmed Fall 2026 rates are **$50 / $65**. The statement also contains:

| Amount | Payments | Total | What the memos say |
| --- | --- | --- | --- |
| $35.00 | 9 | $315 | `MEMBERSHIP DUES`, `DUES`, **`AIS EXEC MEMBERSHIP FEE`** |
| $20.00 | 1 | $20 | `MEMBERSHIP DUES  [MEMBER]` |
| $15.00 | 2 | $30 | `[MEMBER] AIS DUES`, (no memo) |

`AIS EXEC MEMBERSHIP FEE` is the telling one — $35 looks like a deliberate exec
or officer rate, not a mistake. Two of the nine carry no memo at all and are
only recognised as dues because the others establish the amount.

**The question:** is $35 an official Fall 2026 rate? If so it should go in
`terms.dues_rates` alongside 50 and 65, at which point all nine categorise
deterministically. And are $20 / $15 partial payments, hardship rates, or
something else?

## 8. Ten cards now have no recorded holder — and one of them decides five rows

**None of the three cards in this statement is in the roster**, so the card tier
contributed *nothing* to it. Every card-based decision was made on merchant name,
amount and weekday alone.

| Card | Rows | What it bought |
| --- | --- | --- |
| **0594** | 5 | Sam's Club x3, Wal-Mart, Dick's Sporting Goods — all bulk goods |
| 3526 | 2 | The Swamp \$307.75, Macdintons \$298.96 — large weekend bills |
| 3466 | 1 | US Mobile \$96.00 |

Plus seven still unresolved from the previous cohort: 0153, 7757, 9309, 3444,
7193, 1113, 5535.

**0594 is the highest-value single answer available.** Treasury's read is that
its purchases are Membership, not Meeting Food. Confirmed as Membership, five
rows resolve deterministically and stop reaching a human at all — verified by
adding it to the roster and re-running: rows needing review went **14 to 8**.

**The question:** who holds 0594, 3526 and 3466? Add them to
`ais_fmd/config/card_roster.json` — a plain text file, no code change, no deploy.

## 9. Still open from round 1

7.1 disputed purpose mappings, and whether "headshot" is Professional
Development or Membership. Unchanged.

---

**6, 7 and 8 are all data, not code.** Each is entered in a file or a form and
takes effect on the next run.

---

# Round 3 — rulings, 2026-09-23

| # | Ruling | Where it landed |
| --- | --- | --- |
| 8 | **Answered.** From the 09/21/26 E-Board notes: Consulting **4831**, Membership **0594** and **7101**, President **3526**. **3466** (US Mobile) is the treasurer's card: Treasury. Marketing and Prof Dev have no card. | `config/card_roster.json`, era `2026-2027` |
| 10 | **The card is the most highly valued evidence.** A purchase on a confirmed card is that card's committee, ahead of every other rule. This reverses the 2026-08-24 reading that meeting-food timing beats the card, and the 2026-09-08 middle ground where a card only contested it. | `CardRegistry.certain`, first step of `pipeline.categorize_records` |
| 11 | **No hard-coded cards; new cards replace old ones.** "card 8408 is Consulting" was the previous cohort's card. Every card is now a roster entry scoped to its officer cohort's dates, so 8408 still categorizes 2024-2026 statements and decides nothing from 2026-08-01. | `rule_consulting` and `rule_membership_card` removed; `_eras` in `card_roster.json` |
| 12 | **Merchant rules are out of categorization.** Too many merchants serve more than one committee — Consulting food and Meeting Food especially — for a merchant's history to predict the next purchase. | merchant tier removed from the pipeline; Review Queue no longer offers "Remember this merchant" |

Asserted in `tests/test_treasury_decisions.py` (the Fall 2026 cards, 8408 not
carrying over, a Membership card at a Tuesday food run staying Membership, the
treasurer's card) and `tests/test_scoring.py` (the card outweighing a full
meeting-food reading).

---

# Round 4 — rulings, 2026-10-01

| # | Ruling | Where it landed |
| --- | --- | --- |
| 7 | **$35 is an official exec dues rate**, at least for Fall 2026. All nine $35 payments in `Checking (4).csv` now book as dues. | `dues.CONFIRMED_DUES_RATES["Fall 2026"]`; run `scripts/apply_treasury_rates.py --apply` against each database |
| 6 | **The two $500 Venmo Zelles are not income.** They were the chapter's existing money moved between accounts. A Zelle whose memo says "Venmo" is booked to Transfers (3), and Transfers are left out of the income and expense totals. | `rule_internal_transfer`; `budgets._totals_for`, `budgets.categorize_flow`, the assistant's income answer |
| 10 (revised) | **The card is the default, not a verdict.** It is "taken unless all of the other classifications point towards it not being the case", because members sometimes buy for another committee (meeting food especially) with the card they have on hand. A confirmed card stands unless *every* other signal agrees on one other committee with nothing opposing it. Today that means a catering-sized ($75+) food purchase on a Tue/Wed. Partial dissent is noted in the reason but does not move the row. | `pipeline._overrules_the_card`, `scoring.CARD_OVERRULE_EVIDENCE` |

A known consequence, pinned in `test_consulting_catering_on_a_meeting_day_reads_as_meeting_food`: a Consulting card buying $210 of Chipotle on a Tuesday now reads as Meeting Food.

### Round 4, continued (2026-10-01)

| # | Ruling | Where it landed |
| --- | --- | --- |
| 13 | **Dues windows start the day after the previous term ends.** Nobody pays dues for a semester that is over. Fall 2026 dues arrived from 08/12 while the term starts 08/15. This applies to dues matching and the dues pages only. Spending keeps the real term dates. | `predicates._reach_back`, `terms.dues_terms` |
| 14 | **A dues amount is dues at 99%, unless the memo says otherwise.** A payment with no memo or a name-only memo still books. A memo naming something else (ticket, pay back, reimbursement, gala…) sends the row to the model with that reason, then to a human. | `rule_dues`, `DUES_CONFLICT_KEYWORDS` |
| 15 | **Consulting cards are never auto-overruled.** A full meeting-food reading on a Consulting card goes to the model and then a human, with Consulting suggested first. The rule is keyed on the committee, so it carries to next year's card. | `scoring.REVIEW_ON_CARD_CONFLICT` |
| 16 | **Push contested rows to the model, not straight to a human.** The model now sees every committee and the reason a row was flagged. It only books at 75%+ confidence. Below that, its suggestion goes to the Review Queue. | `llm._SYSTEM_PROMPT`, `pipeline.categorize_records` |
| 17 | **Card 9570 is the VP of Operations.** She buys the meeting food, plus the occasional thing for her own committee (the $66.67 Chili's was a rewards dinner). Committee 6, formerly Corporate Relations and now defunct, is renamed **Operations**. Her card defaults to Operations. A catering-sized meeting-day food order on it goes to Meeting Food, and smaller meeting-day food goes to the model and then review. | `config/card_roster.json`, `categories.py`, migration 006, `scoring.REVIEW_MEETING_DAY_FOOD_ON` |
| 18 | **Outgoing reimbursement memos that name their committee are booked.** "MEMBERSHIP REIMBURSEMENT" → Membership and "MEETING FOOD REIMBURSEMENT" → Meeting Food. This only applies to outgoing transfers, because on incoming payments "membership" means dues. | `OUTGOING_MEMO_COMMITTEE_KEYWORDS` |
| 19 | **An outgoing memo that says "consulting" is Consulting**, whatever else it says ("CONSULTING MEETING FOOD REIMBURSEMENT" included). It's checked before every other memo keyword. Outgoing only, because the one incoming example is a hoodie payment. This moved $911 of Spring 2026 "Consulting food" reimbursements out of Meeting Food, and resolved 7 more Consulting reimbursements that were in review. | `predicates.OUTGOING_FIRST_MEMO_KEYWORDS` |

---

# Round 5 — rulings, 2026-11-03

From the review of `Checking (2).csv` (08/10 to 11/02/2026).

| # | Ruling | Where it landed |
| --- | --- | --- |
| 17 (revised) | **Card 9570 defaults to Meeting Food.** "Moe's is absolutely meeting food and that card should default to meeting food with sole exceptions of socials we give them." The only socials a bank line can show are bars and liquor stores, so those book to Operations automatically. Any other Operations social, like the $66.67 Chili's rewards dinner, is left for the spend review and the treasurer to move. This replaces Round 4 #17. | `config/card_roster.json` (`committee: 8`, `socials_to: 6`), `CardRegistry.default_for`; `REVIEW_MEETING_DAY_FOOD_ON` removed |
| 20 | **Monday is a meeting-food day**, alongside Tuesday and Wednesday. | `predicates.MEETING_WEEKDAYS` (scoring and bulk import it) |
| 15 (revised) | **Food on a Consulting card goes to Consulting.** "Consulting card purchases that are similar to meeting food go to consulting, they also cater." Even a full meeting-food reading leaves the card standing, instead of going to the model and review. This replaces Round 4 #15. | `scoring.CARD_NEVER_OVERRULED` |
| 21 | **Start-of-term President charges were Membership.** In Fall 2026, MacDinton's ($298.96, 08/15) and The Swamp ($307.75, 08/21) were Membership socials paid on the President's card. The $21.00 Salty Dog charge (09/23) is the President's own discretionary spending. | A booking change in the app. The spend review's "Start-of-term cover" section already describes this |
| 22 | **Merch memo words:** "crew neck", "sweater" and "shirt" are added. "Moe's" is added to the food merchants. | `MEMO_COMMITTEE_KEYWORDS`, `FOOD_MERCHANT_KEYWORDS` |

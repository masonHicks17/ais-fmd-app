"""
Module M21 -- the membership roster, and dues reconciliation against it.

The app could already report who *did* pay, recovered from transfer
descriptions. It had nothing to compare that against, so the question a
treasurer is actually asked -- "who still owes?" -- was unanswerable except by
typing a headcount into `dues.outstanding` and subtracting.

Everything here is pure. No I/O, no Streamlit.

WHY NAME MATCHING NEEDS ITS OWN MODULE
--------------------------------------
Three properties of the real Fall 2026 statement make the naive `payer == member`
comparison wrong often enough to be useless:

1. **Wells Fargo emits names in both orders.** The same file carries
   "ZELLE FROM TESSA MORROW" and "ZELLE FROM PARK OWEN" -- first-last and
   last-first, with nothing in the row to say which. Any comparison has to try
   both, which is what `name_variants` is for.

2. **People pay for each other, and say so in the memo.** "MARCUS HOLLOWAY ...
   RILEY BRENNAN DUES" is Marcus paying Riley's dues; crediting Marcus
   twice and leaving Riley unpaid is wrong on both counts. Parents pay for
   students ("BETH SULLIVAN ... KATE SULLIVAN DUES"), and one person may cover
   several members.

3. **Memos are noisy.** "MIRA SOLANKI MS ISOM DATA SCIENCE FALL 2026" and
   "AIS DUES FOR GABRIELA MONTOYA" carry a name inside prose.

The approach that survives all three is to search the memo **for roster members**
rather than to parse names out of it generically. The roster is a closed list, so
"does any member's name appear in this memo" is answerable exactly, where "what
name is in this string" is not. Beneficiary beats payer whenever both are found,
because a memo naming someone else is an explicit instruction about who the
payment is for.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
No fuzzy/edit-distance matching. A near-miss on a member name silently credits
the wrong person's dues, and a treasurer chasing someone who already paid --
or, worse, not chasing someone who has not -- is a worse outcome than a row
landing in "unmatched" for a human to resolve in seconds. Unmatched is a
first-class result here, not a failure.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, replace

import pandas as pd

from .dues import extract_payer

# Tokens that are never part of a person's name, stripped before matching so
# "AIS DUES FOR GABRIELA MONTOYA" and "GABRIELA MONTOYA" compare equal.
_NOISE_WORDS = frozenset(
    {
        "ais", "uf", "dues", "due", "payment", "payments", "pay", "paid", "fee",
        "fees", "membership", "member", "memberships", "registration", "entry",
        "entrance", "enrollment", "club", "chapter", "semester", "fall", "spring",
        "summer", "exec", "for", "the", "and", "of", "on", "behalf", "my", "his",
        "her", "their", "ref", "zelle", "venmo", "from", "to", "transfer",
        "reimbursement", "msba", "isom", "ms", "data", "science", "mgt",
    }
)

# Suffixes and honorifics that appear inconsistently between a bank record and a
# membership sheet, so they must not decide a match.
_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v", "md", "phd"})
_TITLES = frozenset({"mr", "mrs", "ms", "miss", "dr"})

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_NON_NAME_RE = re.compile(r"[^a-z\s]")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_name(value: object) -> str:
    """
    A name reduced to lowercase alphabetic tokens, sorted.

    Sorting is what makes "Park Owen" and "Owen Park" the same key, which is
    required because Wells Fargo emits both orders in one file with nothing to
    distinguish them. It costs the ability to tell apart two members whose names
    are anagrams of each other at the token level -- "Owen Park" vs "Park
    John" as two different people -- which is not a real case.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value)
    # "Doe, Jane" -> "Jane Doe"; the comma is the only reliable order marker.
    if "," in text:
        head, _, tail = text.partition(",")
        text = f"{tail} {head}"
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _YEAR_RE.sub(" ", text.lower())
    text = _NON_NAME_RE.sub(" ", text)
    tokens = [
        token
        for token in _WHITESPACE_RE.sub(" ", text).strip().split(" ")
        if token
        and token not in _NOISE_WORDS
        and token not in _SUFFIXES
        and token not in _TITLES
        and len(token) > 1  # drops middle initials, which appear inconsistently
    ]
    return " ".join(sorted(set(tokens)))


def match_key(value: object) -> str:
    """The stored form of a normalised name. One place, so the DB agrees with the code."""
    return normalize_name(value)


@dataclass(frozen=True)
class Member:
    """One person on the roster."""

    full_name: str
    match_key: str
    email: str = ""
    ufid: str = ""
    committee_id: int | None = None
    notes: str = ""
    # Additional normalised names the same person is known by. The Fall 2026
    # membership form carries a Preferred Name for 34 of 152 members, and the
    # bank data uses both forms freely: the roster says "Katherine Sullivan"
    # while the memo on her payment says "KATE SULLIVAN". Matching on the legal
    # name alone would leave a fifth of the roster looking unpaid.
    alt_keys: tuple[str, ...] = ()
    preferred_name: str = ""
    # True when the member ticked "I certify that I have completed the dues
    # transaction" on the form. A claim, not a receipt -- see `disputed`.
    claims_paid: bool = False

    @property
    def tokens(self) -> frozenset[str]:
        return frozenset(self.match_key.split()) if self.match_key else frozenset()

    @property
    def key_sets(self) -> tuple[frozenset[str], ...]:
        """Token sets for every name this person answers to, longest first."""
        keys = [self.match_key, *self.alt_keys]
        sets = {frozenset(k.split()) for k in keys if k}
        return tuple(sorted(sets, key=lambda s: -len(s)))

    @property
    def display_name(self) -> str:
        if self.preferred_name and self.preferred_name.lower() not in self.full_name.lower():
            return f"{self.full_name} ({self.preferred_name})"
        return self.full_name


def member_from_name(name: object, **extra) -> Member | None:
    """Build a Member, or None when the name normalises to nothing."""
    key = match_key(name)
    if not key:
        return None
    text = str(name).strip()
    return Member(full_name=text, match_key=key, **extra)


# Columns a membership export might use for each field, in preference order.
# "preferred name" is deliberately NOT here, though it is the most tempting
# entry in the list. On the Fall 2026 membership form it matched first, and
# because only 65 of 152 people filled it in the roster came out as 58
# nicknames -- "Vik", "RJ", "Nic" -- with no surnames to match on and
# two-thirds of the membership silently missing. A preferred name is an alias
# for a person, never the identity of one.
_NAME_COLUMNS = (
    "full name", "full_name", "name", "member", "member name", "student name",
    "first and last name",
)
_FIRST_COLUMNS = ("first name", "first_name", "first", "given name")
_LAST_COLUMNS = ("last name", "last_name", "last", "surname", "family name")
_EMAIL_COLUMNS = (
    "uf email", "email", "email address", "e-mail", "ufl email", "school email",
    "personal email",
)
_UFID_COLUMNS = ("ufid", "uf id", "student id", "id number")
_PREFERRED_COLUMNS = ("preferred name", "preferred", "nickname", "goes by")

# Google Forms names a column after the whole question, so these are matched as
# prefixes rather than exact labels. The Fall 2026 form's column reads
# "I certify that I have completed the dues transaction".
_CLAIM_COLUMN_PREFIXES = ("i certify", "have you paid", "did you pay", "dues paid")
_CLAIM_TRUE_TOKENS = ("yes", "true", "paid", "y")


def _pick(columns: dict[str, str], candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if candidate in columns:
            return columns[candidate]
    return None


@dataclass
class RosterImport:
    """The outcome of reading an uploaded roster."""

    members: list[Member] = field(default_factory=list)
    skipped_rows: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    name_column: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.members)

    @property
    def duplicates(self) -> int:
        keys = [m.match_key for m in self.members]
        return len(keys) - len(set(keys))


def parse_roster(df: pd.DataFrame) -> RosterImport:
    """
    Read a membership sheet into members.

    Accepts either a single name column or separate first/last columns, because
    both shapes turn up depending on where the sheet was exported from. Column
    matching is by label, case-insensitively -- never by position, which is the
    mistake that made the original Wells Fargo parser import 892 rows as $0.00.
    """
    result = RosterImport()
    if df is None or df.empty:
        return RosterImport(errors=["The file contains no rows."])

    columns = {str(c).strip().lower(): c for c in df.columns}
    name_col = _pick(columns, _NAME_COLUMNS)
    first_col = _pick(columns, _FIRST_COLUMNS)
    last_col = _pick(columns, _LAST_COLUMNS)

    if name_col is None and not (first_col and last_col):
        return RosterImport(
            errors=[
                "No name column found. Expected one of "
                f"{', '.join(_NAME_COLUMNS[:4])}, or separate first/last name columns. "
                f"Found: {', '.join(str(c) for c in df.columns)}"
            ]
        )

    email_col = _pick(columns, _EMAIL_COLUMNS)
    ufid_col = _pick(columns, _UFID_COLUMNS)
    preferred_col = _pick(columns, _PREFERRED_COLUMNS)
    claim_col = next(
        (
            original
            for lowered, original in columns.items()
            if lowered.startswith(_CLAIM_COLUMN_PREFIXES)
        ),
        None,
    )
    result.name_column = str(name_col or f"{first_col} + {last_col}")

    seen: set[str] = set()
    for row in df.to_dict("records"):
        last = str(row.get(last_col) or "").strip() if last_col else ""
        if name_col is not None:
            raw_name = row.get(name_col)
        else:
            first = str(row.get(first_col) or "").strip()
            raw_name = f"{first} {last}".strip()

        preferred = str(row.get(preferred_col) or "").strip() if preferred_col else ""
        if preferred.lower() in {"nan", "none"}:
            preferred = ""

        # The preferred name may be a bare first name ("Kate") or an entire name
        # ("Caleb Foster"), depending on how the person filled the form in.
        # Appending the surname covers the first case and is harmless in the
        # second, because `normalize_name` de-duplicates tokens.
        alt_keys: list[str] = []
        if preferred:
            for candidate in (f"{preferred} {last}".strip(), preferred):
                key = match_key(candidate)
                if key:
                    alt_keys.append(key)

        claims_paid = False
        if claim_col is not None:
            answer = str(row.get(claim_col) or "").strip().lower()
            claims_paid = answer.startswith(_CLAIM_TRUE_TOKENS)

        member = member_from_name(
            raw_name,
            email=str(row.get(email_col) or "").strip() if email_col else "",
            ufid=str(row.get(ufid_col) or "").strip() if ufid_col else "",
            preferred_name=preferred,
            claims_paid=claims_paid,
        )
        if member is None:
            result.skipped_rows += 1
            continue
        if member.match_key in seen:
            result.skipped_rows += 1
            continue
        seen.add(member.match_key)
        # Never let an alias equal the primary key, or specificity ranking sees
        # the same name twice.
        member = replace(
            member,
            alt_keys=tuple(dict.fromkeys(k for k in alt_keys if k != member.match_key)),
        )
        result.members.append(member)

    if result.skipped_rows:
        result.warnings.append(
            f"{result.skipped_rows} row(s) skipped: blank or duplicate names."
        )
    if not result.members:
        result.errors.append("No usable names found in the file.")
    return result


# --- Matching ----------------------------------------------------------------

# The memo extractor lives with the categorizer, which reads memos too.
from .categorize.predicates import extract_memo  # noqa: E402  (re-exported)


@dataclass(frozen=True)
class PaymentMatch:
    """One dues payment, resolved (or not) to a roster member."""

    index: int
    amount: float
    transaction_date: object
    details: str
    payer_name: str
    member: Member | None
    matched_on: str  # "payer" | "memo" | "" when unmatched
    on_behalf_of: bool = False

    @property
    def matched(self) -> bool:
        return self.member is not None


# Shorter than this, a prefix is not evidence: "li" would claim "Liang",
# "Lillian" and "Liu" alike.
_MIN_PREFIX = 3


def _prefix_hit(token: str, candidates: set[str]) -> bool:
    """Is `token` equal to, or a plausible shortening of, any candidate token?"""
    for other in candidates:
        if token == other:
            return True
        if len(token) >= _MIN_PREFIX and len(other) >= _MIN_PREFIX:
            if token.startswith(other) or other.startswith(token):
                return True
    return False


def _matched_alias_tokens(member: Member, text_tokens: set[str]) -> frozenset[str] | None:
    """
    The specific tokens in `text_tokens` that satisfy `member`, if any key set does.

    Used to turn a confirmed suggestion into a precise alias: not the whole memo
    (which may carry unrelated words) and not the member's own spelling (which
    is exactly what did not match), but the payer's actual spelling of just the
    name -- "zachariah calloway", not "calloway zachariah dues fall 2026".
    """
    for keys in member.key_sets:
        if keys and all(_prefix_hit(token, text_tokens) for token in keys):
            chosen: set[str] = set()
            for token in keys:
                candidates = [t for t in text_tokens if _prefix_hit(token, {t})]
                if not candidates:
                    return None
                exact = [t for t in candidates if t == token]
                chosen.add(exact[0] if exact else min(candidates, key=len))
            return frozenset(chosen)
    return None


def suggested_alias(payment: PaymentMatch, member: Member) -> str:
    """
    The alias to record if a treasurer confirms `payment` belongs to `member`.

    Recomputes independently of `Reconciliation.suggestions()` so a caller only
    needs the one payment/member pair being confirmed, not the whole
    reconciliation. Falls back to the payer's own normalised name if the
    token-level pick fails for any reason -- still a reasonable alias, just a
    less surgical one.
    """
    text = extract_memo(payment.details)
    if payment.member is None:
        text = f"{payment.payer_name} {text}"
    tokens = set(normalize_name(text).split())
    chosen = _matched_alias_tokens(member, tokens)
    if chosen:
        return " ".join(sorted(chosen))
    return normalize_name(payment.payer_name)


def _scored_hits(text: str, members: list[Member]) -> list[tuple[int, Member]]:
    """
    Every roster member whose whole name appears in `text`, most specific first.

    Requires every token of one of the member's names to be present. A single
    shared token is not a match -- with a roster of hundreds, one common surname
    would otherwise claim payments belonging to someone else.

    The score is the length of the *matched* name, not of the legal one, so a
    member found by their preferred name is ranked on what actually matched.
    """
    tokens = set(normalize_name(text).split())
    if not tokens:
        return []
    seen: set[str] = set()
    hits: list[tuple[int, Member]] = []
    for member in members:
        if member.match_key in seen:
            continue
        matched = [len(s) for s in member.key_sets if s and s <= tokens]
        if matched:
            seen.add(member.match_key)
            hits.append((max(matched), member))
    hits.sort(key=lambda pair: (-pair[0], pair[1].match_key))
    return hits


def _best_member(hits: list[tuple[int, Member]]) -> Member | None:
    """
    The one member a list of scored hits unambiguously points at.

    The longest matched name wins: "Kate Sullivan" beating "Kate" is right,
    because the more specific name is the more likely referent. Equal-length
    ties are refused rather than broken arbitrarily.
    """
    if not hits:
        return None
    if len(hits) > 1 and hits[0][0] == hits[1][0]:
        return None
    return hits[0][1]


def _find_member_in_text(text: str, members: list[Member]) -> Member | None:
    """The roster member `text` names, if exactly one does."""
    return _best_member(_scored_hits(text, members))


def match_payments(
    dues_rows: pd.DataFrame,
    members: list[Member],
) -> list[PaymentMatch]:
    """
    Attribute each dues payment to a roster member.

    Order is the whole rule: **the memo outranks the payer.** A memo naming a
    different member is an explicit statement about who the payment covers, and
    the real statement is full of them -- one member paid three times, once for
    themselves and twice for other people. Crediting the payer in that case
    double-counts one member and leaves two unpaid.
    """
    matches: list[PaymentMatch] = []
    if dues_rows is None or dues_rows.empty:
        return matches

    for position, row in enumerate(dues_rows.to_dict("records")):
        details = str(row.get("details") or "")
        payer = extract_payer(details) or ""

        # Search the memo only, never the whole row. The row text begins with
        # the payer's own name, so searching all of it would credit the payer
        # for every payment and "on behalf of" could never be detected.
        #
        # An earlier attempt subtracted the payer's tokens from the row instead.
        # That broke the commonest on-behalf case there is: a parent paying for
        # a student shares the surname, so "BETH SULLIVAN ... KATE SULLIVAN"
        # lost "sullivan" and Kate stopped matching. Slicing by position in the
        # record is exact where token subtraction is not.
        memo_hits = _scored_hits(extract_memo(details), members)
        beneficiary = _best_member(memo_hits)

        if beneficiary is not None:
            member, matched_on = beneficiary, "memo"
            # A display hint, not an accounting decision: the credit is the same
            # either way. It can misfire when the bank name and the roster name
            # are different forms of one person ("Nikitha" vs "Nikki"), which is
            # why nothing downstream depends on it.
            on_behalf = normalize_name(payer) != beneficiary.match_key
        elif len(memo_hits) > 1:
            # The memo names several members -- one transfer covering a friend
            # as well as the sender. Deciding how much belongs to whom is a
            # judgement, so it must NOT quietly fall back to crediting the
            # payer: that credits one member, leaves the other showing unpaid,
            # and sends a treasurer chasing somebody who has already paid.
            member, matched_on, on_behalf = None, "names several members", False
        else:
            member = _find_member_in_text(payer, members)
            matched_on, on_behalf = ("payer" if member else ""), False

        try:
            amount = float(row.get("amount") or 0.0)
        except (TypeError, ValueError):
            amount = 0.0

        matches.append(
            PaymentMatch(
                index=position,
                amount=amount,
                transaction_date=row.get("transaction_date"),
                details=details,
                payer_name=payer,
                member=member,
                matched_on=matched_on,
                on_behalf_of=on_behalf,
            )
        )
    return matches


@dataclass
class Reconciliation:
    """Roster against payments: who paid, who has not, and what did not match."""

    matches: list[PaymentMatch] = field(default_factory=list)
    members: list[Member] = field(default_factory=list)

    @property
    def paid_keys(self) -> set[str]:
        return {m.member.match_key for m in self.matches if m.member is not None}

    @property
    def unpaid(self) -> list[Member]:
        paid = self.paid_keys
        return [m for m in self.members if m.match_key not in paid]

    @property
    def unmatched(self) -> list[PaymentMatch]:
        """Payments that name nobody on the roster. Money in, person unknown."""
        return [m for m in self.matches if m.member is None]

    @property
    def on_behalf(self) -> list[PaymentMatch]:
        return [m for m in self.matches if m.on_behalf_of]

    @property
    def disputed(self) -> list[Member]:
        """
        Members who certified on the form that they paid, with no payment found.

        The most useful list the reconciliation produces, because every entry is
        a specific discrepancy with a named person attached rather than a
        statistic. Each is one of: paid by a method the bank export does not
        cover (cash, or the Venmo account being retired), paid under a name the
        matcher could not connect, or did not actually pay. All three need a
        human, and all three are invisible without the form.
        """
        paid = self.paid_keys
        return [m for m in self.members if m.claims_paid and m.match_key not in paid]

    def suggestions(self) -> list[tuple[PaymentMatch, Member]]:
        """
        Unmatched payments paired with the unpaid member they probably belong to.

        Strictly a proposal. Nothing here is credited automatically, and that is
        the point: exact matching refuses "ZACHARIAH CALLOWAY" against a roster
        reading "Zach Calloway", and refusing is right, because a matcher loose
        enough to accept it is also loose enough to credit the wrong person's
        dues. What a human can do in one glance is confirm it.

        The rule is prefix-per-token: every token of the member's name must be a
        prefix of, or equal to, some token in the payment. That accepts
        "Zach"/"Zachariah" and "Whitfield"/"Whitfields" -- shortened forms and stray plurals,
        which is what the real near-misses were -- while rejecting "Victor Huang"
        against "Yi Ming Huang", where only the surname agrees.
        """
        pairs: list[tuple[PaymentMatch, Member]] = []
        unpaid = self.unpaid
        if not unpaid:
            return pairs

        # Searched from the member's side, over *every* payment rather than only
        # the unmatched ones. A near-miss in a memo does not leave the payment
        # unmatched -- it falls through to crediting the payer, which is how
        # "MARCUS HOLLOWAY ... DANA WHITFIELDS DUES" ended up as a second credit for
        # Marcus while Dana showed unpaid. Looking only at unmatched payments
        # would never find her.
        for member in unpaid:
            candidates: list[PaymentMatch] = []
            for payment in self.matches:
                text = extract_memo(payment.details)
                if payment.member is None:
                    # Nobody is claiming this one, so the sender's name is fair
                    # evidence too.
                    text = f"{payment.payer_name} {text}"
                tokens = set(normalize_name(text).split())
                if not tokens:
                    continue
                if any(
                    keys and all(_prefix_hit(token, tokens) for token in keys)
                    for keys in member.key_sets
                ):
                    candidates.append(payment)
            if len(candidates) == 1:
                pairs.append((candidates[0], member))
        return pairs

    def totals_by_member(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for match in self.matches:
            if match.member is None:
                continue
            key = match.member.match_key
            totals[key] = totals.get(key, 0.0) + match.amount
        return totals

    def duplicates(self) -> list[tuple[Member, int, float]]:
        """
        Members credited by more than one payment.

        Usually an instalment or a correction, occasionally a genuine double
        charge someone should be refunded for. Either way a treasurer wants to
        see it rather than have the total quietly absorb it.
        """
        counts: dict[str, list[PaymentMatch]] = {}
        for match in self.matches:
            if match.member is not None:
                counts.setdefault(match.member.match_key, []).append(match)
        by_key = {m.match_key: m for m in self.members}
        return [
            (by_key[key], len(group), sum(p.amount for p in group))
            for key, group in counts.items()
            if len(group) > 1 and key in by_key
        ]

    def summary(self) -> dict:
        totals = self.totals_by_member()
        expected = len(self.members)
        paid = len(totals)
        return {
            "expected": expected,
            "paid": paid,
            "unpaid": expected - paid,
            "rate": (paid / expected * 100) if expected else 0.0,
            "collected": sum(totals.values()),
            "unmatched_payments": len(self.unmatched),
            "unmatched_amount": sum(m.amount for m in self.unmatched),
            "on_behalf": len(self.on_behalf),
            "disputed": len(self.disputed),
        }


def _text(value: object) -> str:
    """
    A DB column value as a string, NULL-safe.

    `value or ""` does not fall back on a pandas NaN -- NaN is truthy, so it
    returns the NaN, and a caller wrapping that in `str()` gets the literal
    string "nan" rendered on the page instead of a blank. Every optional TEXT
    column read from SQLite needs this, not just `value or ""`.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value)


def members_from_rows(stored: pd.DataFrame) -> list[Member]:
    """Members from stored roster rows (`repositories.load_members`)."""
    members = []
    for row in stored.to_dict("records"):
        alt_keys = tuple(
            part.strip() for part in _text(row.get("alt_keys")).split(",") if part.strip()
        )
        members.append(
            Member(
                full_name=_text(row.get("full_name")),
                match_key=_text(row.get("match_key")),
                alt_keys=alt_keys,
                preferred_name=_text(row.get("preferred_name")),
                claims_paid=bool(row.get("claims_paid") or 0),
                email=_text(row.get("email")),
                ufid=_text(row.get("ufid")),
            )
        )
    return members


def semester_dues_rows(transactions: pd.DataFrame, terms: pd.DataFrame, semester: str) -> pd.DataFrame:
    """The dues payments for `semester`, including those paid before it began."""
    from .dues import dues_transactions, schedule_from_terms
    from .terms import attach_semester, dues_terms

    rows = dues_transactions(transactions, schedule_from_terms(terms))
    if rows.empty:
        return rows
    # `dues_terms`, not the plain term dates: dues for a term start arriving
    # before its first day (Fall 2026's on 08/12, term start 08/15). Tagging by
    # the plain dates dropped those payments and showed their payers as unpaid,
    # while the Dues page's Collected figure -- which uses `dues_terms` -- counted
    # them. Found 2026-11-08.
    tagged = attach_semester(rows, dues_terms(terms))
    return tagged[tagged["Semester"].astype(str) == str(semester)]


def reconcile(dues_rows: pd.DataFrame, members: list[Member]) -> Reconciliation:
    """Match every dues payment to the roster and report both directions."""
    return Reconciliation(
        matches=match_payments(dues_rows, members),
        members=list(members),
    )


def _member_frame(members: list[Member]) -> pd.DataFrame:
    columns = ["Member", "Email", "UFID", "Says they paid"]
    if not members:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(
        [
            {
                "Member": m.display_name,
                "Email": m.email,
                "UFID": m.ufid,
                "Says they paid": "yes" if m.claims_paid else "",
            }
            for m in members
        ],
        columns=columns,
    )


def unpaid_frame(result: Reconciliation) -> pd.DataFrame:
    """The chase list, ready to display or export."""
    return _member_frame(result.unpaid)


def disputed_frame(result: Reconciliation) -> pd.DataFrame:
    """Members who say they paid but whose payment cannot be found."""
    return _member_frame(result.disputed)


def matched_frame(result: Reconciliation) -> pd.DataFrame:
    """Every payment with the member it was credited to and why."""
    columns = ["Date", "Amount", "Paid by", "Credited to", "Matched on", "On behalf of"]
    if not result.matches:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(
        [
            {
                "Date": m.transaction_date,
                "Amount": m.amount,
                "Paid by": m.payer_name,
                "Credited to": m.member.full_name if m.member else "",
                "Matched on": m.matched_on or "no match",
                "On behalf of": "yes" if m.on_behalf_of else "",
            }
            for m in result.matches
        ],
        columns=columns,
    )

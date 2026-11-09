"""
Production backend.

Not exercised by the sandbox -- the `supabase` package is deliberately absent
from the sandbox venv, and importing this module in sandbox mode raises. It is
included so the fixes have a production implementation to migrate to, and so the
corrected patterns are written down somewhere concrete.

FINDING F1, and what actually happened to it. The original cached one Supabase
client with `@st.cache_resource`, which caches per *process*, not per session.
Every visitor shared one client object, `sign_in_with_password` wrote the auth
session onto it, and `sign_out` revoked it for everybody. Any RLS policy keyed
on auth.uid() was evaluated against whoever logged in most recently.

This module used to answer that with a `_user` property that carried a
per-session access token. **That property was never wired up** -- `get_backend()`
constructs `SupabaseBackend()` with no token, and nothing ever passed one -- so
it has been removed rather than left standing as a fix that was never connected.

The architecture it belonged to was also abandoned deliberately, not by
accident: the app authenticates people with Streamlit's own OIDC login and talks
to Supabase with its own keys regardless of who is using it, so no individual
ever opens a Postgres session and `auth.uid()` is always NULL. See the long note
on `profiles` in migrations/002_deferred_features.sql. Authorization is
application-level, in `auth.require(...)`, on every page.

So the database protects itself the only way this architecture allows: every
read and write goes through the service (secret) key, and migration
008_lock_down.sql enables RLS with no permissive policy and revokes the RPCs
from anon/authenticated. The service key bypasses RLS by design, so it is the
only way in. The publishable key is no longer used -- after 008 it can read
nothing, and a read through it would silently return zero rows rather than
fail, which is why no code path is left that could fall back to it.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from .. import settings
from ..domain import flags as flags_domain
from .backend import Backend, TransactionChange, UpdateResult, UploadReceipt, normalize_email

PAGE_SIZE = 1000


def _require_production() -> None:
    settings.assert_external_call_allowed("Supabase connection")


def _now() -> str:
    """
    The current time, as an ISO 8601 string PostgREST will parse into `timestamptz`.

    NOT the literal string `"now()"` -- a JSON/REST write is data, not SQL, so
    `"now()"` would be inserted as an attempt to parse the four characters
    "now()" as a timestamp and rejected by Postgres, not evaluated as the SQL
    function. Columns with a `DEFAULT now()` (most of them, on INSERT) don't
    need this; it's only for UPDATE statements that have to set a timestamp
    column explicitly, the way `sqlite_backend.py` uses `CURRENT_TIMESTAMP` in
    an `UPDATE ... SET` clause.
    """
    return datetime.now(timezone.utc).isoformat()


class SupabaseBackend(Backend):
    name = "supabase"

    def __init__(self) -> None:
        _require_production()
        try:
            from supabase import create_client
        except ImportError as exc:  # pragma: no cover - depends on install
            raise RuntimeError(
                "The 'supabase' package is not installed. The sandbox venv omits "
                "it on purpose; install it only in a production environment."
            ) from exc

        self._create_client = create_client

        # Accepts either shape, because both are documented somewhere and the
        # two used to disagree. `os.environ` works on a host where you can set
        # real environment variables; a `[supabase]` section in secrets.toml is
        # the only channel Streamlit Community Cloud offers -- and Streamlit
        # never copies a *section* into os.environ, only top-level scalars, so
        # the previous `os.environ["SUPABASE_URL"]` could not be satisfied by
        # the secrets file the project's own example told people to write.
        #
        # New-format key names (publishable/secret) are preferred and the legacy
        # anon/service_role names still work, so this reads whichever is present.
        section = settings.secret_section("supabase")

        def _config(*names: str, required: bool = False) -> str:
            for name in names:
                value = settings._setting(name) or str(section.get(name, "") or "")
                if value.strip():
                    return value.strip()
            if required:
                raise RuntimeError(
                    f"Supabase is not configured: set one of {', '.join(names)} "
                    f"as an environment variable or under [supabase] in "
                    f".streamlit/secrets.toml."
                )
            return ""

        self._url = _config("SUPABASE_URL", "url", required=True)
        self._service_key = _config(
            "SUPABASE_SERVICE_KEY", "secret_key", "service_key", required=True
        )
        self._admin_client = None

    # --- clients -------------------------------------------------------------
    #
    # Built once per backend instance and reused. This was previously a bare
    # `@property` calling `create_client()` on *every* access, so a page doing
    # ten reads opened ten HTTP clients and ten TLS handshakes. The backend
    # object itself is cached per process by `repositories.backend()`, which is
    # safe precisely because it holds no user identity.

    @property
    def _admin(self):
        """Service-key client. Bypasses RLS -- server-side only, never exposed."""
        if self._admin_client is None:
            self._admin_client = self._create_client(self._url, self._service_key)
        return self._admin_client

    # --- reads ---------------------------------------------------------------

    def _select_all(self, table: str) -> pd.DataFrame:
        """
        Paginated read.

        The original loop issued one extra request whenever the row count was an
        exact multiple of the page size; stopping on a short page avoids that.
        """
        client = self._admin
        rows: list[dict] = []
        offset = 0
        while True:
            response = (
                client.table(table)
                .select("*")
                .range(offset, offset + PAGE_SIZE - 1)
                .execute()
            )
            batch = response.data or []
            rows.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        return pd.DataFrame(rows)

    def fetch_committees(self) -> pd.DataFrame:
        return self._select_all("committees")

    def fetch_terms(self) -> pd.DataFrame:
        return self._select_all("terms")

    def fetch_budgets(self) -> pd.DataFrame:
        return self._select_all("committeebudgets")

    def fetch_transactions(self) -> pd.DataFrame:
        frame = self._select_all("transactions")
        if not frame.empty:
            frame["transaction_date"] = pd.to_datetime(frame["transaction_date"], errors="coerce")
            frame["budget_category"] = frame["budget_category"].astype("Int64")
        return frame

    def fetch_uploaded_files(self) -> pd.DataFrame:
        return self._select_all("uploaded_files")

    def fetch_merchants(self) -> pd.DataFrame:
        return self._select_all("merchants")

    def fetch_audit(self, limit: int = 500) -> pd.DataFrame:
        response = (
            self._admin.table("transaction_audit")
            .select("*")
            .order("audit_id", desc=True)
            .limit(limit)
            .execute()
        )
        return pd.DataFrame(response.data or [])

    def fetch_statement_balances(self) -> pd.DataFrame:
        return self._select_all("statement_balances")

    def fetch_labeled_examples(self) -> pd.DataFrame:
        return self._select_all("labeled_examples")

    def fetch_reimbursements(self) -> pd.DataFrame:
        """
        Pending and approved first, then newest first within each -- matching
        `sqlite_backend.py`.

        Sorted client-side because PostgREST's `order()` takes a column name,
        not an expression; SQLite's `CASE status WHEN 'pending' THEN 0 ...` has
        no equivalent over the query-builder API. Fine at this table's size —
        a chapter's outstanding reimbursements, not a transaction ledger.
        """
        response = (
            self._admin.table("reimbursements")
            .select("*")
            .order("submitted_at", desc=True)
            .execute()
        )
        frame = pd.DataFrame(response.data or [])
        if frame.empty:
            return frame
        priority = {"pending": 0, "approved": 1}
        frame["_priority"] = frame["status"].map(lambda s: priority.get(s, 2))
        return frame.sort_values(["_priority"], kind="stable").drop(columns="_priority")

    def fetch_receipts(self) -> pd.DataFrame:
        response = (
            self._admin.table("receipts")
            .select("*")
            .order("uploaded_at", desc=True)
            .execute()
        )
        return pd.DataFrame(response.data or [])

    def fetch_members(self, term_id: str | None = None) -> pd.DataFrame:
        query = self._admin.table("members").select("*")
        if term_id:
            query = query.eq("term_id", term_id).order("full_name")
        else:
            query = query.order("term_id").order("full_name")
        return pd.DataFrame((query.execute().data or []))

    def fetch_profiles(self) -> pd.DataFrame:
        response = (
            self._admin.table("profiles")
            .select("*")
            .order("committee_id")
            .order("display_name")
            .execute()
        )
        return pd.DataFrame(response.data or [])

    def fetch_profile(self, email: str) -> dict | None:
        """
        One person's access record, or None on a miss.

        Matched with `eq` on the normalized (lowercase) email, which is also how
        `upsert_profile` stores it. This used to be `ilike`, but in a LIKE
        pattern `_` and `%` are wildcards and `_` is common in real addresses --
        `a_b@x.edu` matched `a.b@x.edu`, so a lookup that decides who gets in as
        which committee could land on a different person's row (and, with
        `maybe_single`, error out when it matched two).
        """
        key = normalize_email(email)
        if not key:
            return None
        response = (
            self._admin.table("profiles")
            .select("*")
            .eq("email", key)
            .maybe_single()
            .execute()
        )
        return response.data if response is not None else None

    # --- writes --------------------------------------------------------------

    def _audit(
        self,
        *,
        transaction_id: int | None,
        action: str,
        actor: str,
        field: str | None = None,
        old_value: str | None = None,
        new_value: str | None = None,
    ) -> None:
        """
        Best-effort audit row.

        Not atomic with the write it describes -- a genuinely atomic pairing
        needs a Postgres function, the way `insert_transactions` and
        `update_transactions` already use `import_statement` and
        `apply_transaction_edits` for exactly that reason (FINDING F4/F6). The
        methods below don't get that treatment because none of the *existing*
        simple writes in this file do either (`insert_term`, `upsert_merchants`,
        `record_statement_balance` write no audit row at all) -- this at least
        adds one, on the same non-atomic footing the rest of the file already
        accepts. Failure here must not fail the caller's real write, so it is
        swallowed rather than propagated.
        """
        try:
            self._admin.table("transaction_audit").insert(
                {
                    "transaction_id": transaction_id,
                    "action": action,
                    "field": field,
                    "old_value": old_value,
                    "new_value": new_value,
                    "actor": actor,
                }
            ).execute()
        except Exception:  # noqa: BLE001 - the write already succeeded; don't undo it
            pass

    def create_reimbursement(self, request: dict, actor: str) -> UpdateResult:
        result = UpdateResult()
        try:
            response = (
                self._admin.table("reimbursements")
                .insert(
                    {
                        "requester": request["requester"],
                        "committee_id": request.get("committee_id"),
                        "amount": float(request["amount"]),
                        "description": request.get("description"),
                        "incurred_on": request.get("incurred_on"),
                        "status": "pending",
                        "receipt_id": request.get("receipt_id"),
                    }
                )
                .execute()
            )
            request_id = (response.data or [{}])[0].get("request_id")
            self._audit(
                transaction_id=None,
                action="reimbursement_request",
                actor=actor,
                field=f"request {request_id}",
                new_value=f"{request['requester']} ${float(request['amount']):.2f}",
            )
            result.updated = 1
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def decide_reimbursement(
        self, request_id: int, status: str, actor: str, note: str = ""
    ) -> UpdateResult:
        result = UpdateResult()
        if status not in {"approved", "rejected", "paid", "pending"}:
            result.error = f"Unknown status {status!r}."
            return result
        try:
            existing = (
                self._admin.table("reimbursements")
                .select("status")
                .eq("request_id", request_id)
                .maybe_single()
                .execute()
            )
            if existing is None or existing.data is None:
                result.error = f"Request {request_id} does not exist."
                return result
            if existing.data.get("status") == status:
                result.unchanged = 1
                return result

            self._admin.table("reimbursements").update(
                {
                    "status": status,
                    "decided_at": _now(),
                    "decided_by": actor,
                    "decision_note": note or None,
                }
            ).eq("request_id", request_id).execute()
            self._audit(
                transaction_id=None,
                action="reimbursement_decision",
                actor=actor,
                field=f"request {request_id}",
                old_value=existing.data.get("status"),
                new_value=status,
            )
            result.updated = 1
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def link_reimbursement_to_transaction(
        self, request_id: int, transaction_id: int, actor: str
    ) -> UpdateResult:
        result = UpdateResult()
        try:
            self._admin.table("reimbursements").update(
                {
                    "matched_transaction_id": transaction_id,
                    "status": "paid",
                    "decided_at": _now(),
                    "decided_by": actor,
                }
            ).eq("request_id", request_id).execute()
            self._audit(
                transaction_id=transaction_id,
                action="reimbursement_matched",
                actor=actor,
                field=f"request {request_id}",
                new_value=str(transaction_id),
            )
            result.updated = 1
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def store_receipt(self, receipt: dict, actor: str) -> tuple[int | None, UpdateResult]:
        """Record receipt metadata. The file itself is written by domain.receipts."""
        result = UpdateResult()
        try:
            response = (
                self._admin.table("receipts")
                .insert(
                    {
                        "file_name": receipt["file_name"],
                        "stored_path": receipt["stored_path"],
                        "content_type": receipt.get("content_type"),
                        "byte_size": receipt.get("byte_size"),
                        "uploaded_by": actor,
                        "transaction_id": receipt.get("transaction_id"),
                        "request_id": receipt.get("request_id"),
                        "vendor": receipt.get("vendor"),
                        "amount": receipt.get("amount"),
                        "receipt_date": receipt.get("receipt_date"),
                    }
                )
                .execute()
            )
            receipt_id = (response.data or [{}])[0].get("receipt_id")
            self._audit(
                transaction_id=receipt.get("transaction_id"),
                action="receipt_uploaded",
                actor=actor,
                field=receipt["file_name"],
            )
            result.updated = 1
            return receipt_id, result
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
            return None, result

    def set_term_lock(self, term_id: str, locked: bool, actor: str) -> UpdateResult:
        result = UpdateResult()
        try:
            existing = (
                self._admin.table("terms")
                .select("Semester,locked")
                .eq("TermID", term_id)
                .maybe_single()
                .execute()
            )
            if existing is None or existing.data is None:
                result.error = f"Term {term_id} does not exist."
                return result
            if bool(existing.data.get("locked")) == locked:
                result.unchanged = 1
                return result

            self._admin.table("terms").update(
                {"locked": locked, "locked_at": _now(), "locked_by": actor}
            ).eq("TermID", term_id).execute()
            self._audit(
                transaction_id=None,
                action="lock" if locked else "unlock",
                actor=actor,
                field=term_id,
                old_value="locked" if existing.data.get("locked") else "open",
                new_value="locked" if locked else "open",
            )
            result.updated = 1
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def set_term_dues_rates(
        self, term_id: str, rates: str, verified: bool, actor: str
    ) -> UpdateResult:
        result = UpdateResult()
        try:
            existing = (
                self._admin.table("terms")
                .select("dues_rates,dues_rates_verified")
                .eq("TermID", term_id)
                .maybe_single()
                .execute()
            )
            if existing is None or existing.data is None:
                result.error = f"Term {term_id} does not exist."
                return result
            previous = existing.data.get("dues_rates")
            was_verified = bool(existing.data.get("dues_rates_verified"))
            if (previous or "") == (rates or "") and was_verified == verified:
                result.unchanged = 1
                return result

            self._admin.table("terms").update(
                {"dues_rates": rates, "dues_rates_verified": verified}
            ).eq("TermID", term_id).execute()
            self._audit(
                transaction_id=None,
                action="dues_rates_set",
                actor=actor,
                field=term_id,
                old_value=previous,
                new_value=rates,
            )
            result.updated = 1
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    # --- Sponsorship (M20). Needs migrations/012_sponsorship.sql. ------------

    def set_term_sponsorship_goal(
        self, term_id: str, goal: float | None, actor: str
    ) -> UpdateResult:
        """Same contract as `SqliteBackend.set_term_sponsorship_goal`: None clears it."""
        result = UpdateResult()
        try:
            existing = (
                self._admin.table("terms")
                .select("sponsorship_goal")
                .eq("TermID", term_id)
                .maybe_single()
                .execute()
            )
            if existing is None or existing.data is None:
                result.error = f"Term {term_id} does not exist."
                return result
            previous = existing.data.get("sponsorship_goal")
            if (previous is None and goal is None) or (
                previous is not None and goal is not None and float(previous) == float(goal)
            ):
                result.unchanged = 1
                return result

            self._admin.table("terms").update({"sponsorship_goal": goal}).eq("TermID", term_id).execute()
            self._audit(
                transaction_id=None,
                action="sponsorship_goal",
                actor=actor,
                field=term_id,
                old_value="(none)" if previous is None else f"${float(previous):,.2f}",
                new_value="(none)" if goal is None else f"${goal:,.2f}",
            )
            result.updated = 1
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    # --- Dues price dates. Needs migrations/015_dues_schedule.sql. ----------

    def set_term_dues_schedule(
        self,
        term_id: str,
        early_price: float | None,
        late_price: float | None,
        increase_date: str | None,
        late_cutoff: str | None,
        actor: str,
    ) -> UpdateResult:
        """Same contract as `SqliteBackend.set_term_dues_schedule`."""
        fields = ("dues_early_price", "dues_late_price", "dues_increase_date", "dues_late_cutoff")
        new = (early_price, late_price, increase_date, late_cutoff)
        result = UpdateResult()
        try:
            existing = (
                self._admin.table("terms").select(",".join(fields)).eq("TermID", term_id)
                .maybe_single().execute()
            )
            if existing is None or existing.data is None:
                result.error = f"Term {term_id} does not exist."
                return result
            old = tuple(existing.data.get(field) for field in fields)
            if old == new:
                result.unchanged = 1
                return result
            self._admin.table("terms").update(dict(zip(fields, new))).eq("TermID", term_id).execute()
            self._audit(
                transaction_id=None,
                action="dues_schedule",
                actor=actor,
                field=term_id,
                old_value=str(old),
                new_value=str(new),
            )
            result.updated = 1
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def set_transaction_sponsor_name(
        self, transaction_id: int, sponsor_name: str | None, actor: str
    ) -> UpdateResult:
        """
        Same contract as `SqliteBackend.set_transaction_sponsor_name`, including
        the closed-term refusal: `locked_term_for` (migration 003) is the same
        check `apply_transaction_edits` uses, so this is not a side door.
        """
        result = UpdateResult()
        try:
            existing = (
                self._admin.table("transactions")
                .select("sponsor_name,transaction_date")
                .eq("transactionid", int(transaction_id))
                .maybe_single()
                .execute()
            )
            if existing is None or existing.data is None:
                result.failed.append(transaction_id)
                result.error = f"Transaction {transaction_id} does not exist."
                return result

            closed = self._admin.rpc(
                "locked_term_for", {"p_date": existing.data.get("transaction_date")}
            ).execute().data
            if closed:
                result.failed.append(transaction_id)
                result.error = (
                    f"This transaction falls in {closed}, which is closed. "
                    f"Reopen the term before editing it."
                )
                return result

            previous = existing.data.get("sponsor_name")
            cleaned = (sponsor_name or "").strip() or None
            if (previous or None) == cleaned:
                result.unchanged = 1
                return result

            self._admin.table("transactions").update({"sponsor_name": cleaned}).eq(
                "transactionid", int(transaction_id)
            ).execute()
            self._audit(
                transaction_id=int(transaction_id),
                action="update",
                actor=actor,
                field="sponsor_name",
                old_value=previous,
                new_value=cleaned,
            )
            result.updated = 1
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def insert_labeled_examples(self, examples: list[dict], actor: str) -> UpdateResult:
        """
        Record human labels. Idempotent on `natural_key` via `ignore_duplicates`
        (Postgres `ON CONFLICT (natural_key) DO NOTHING`), matching
        `sqlite_backend.py` so re-importing the same ledger adds nothing rather
        than inflating the training set with duplicates.

        One batched upsert rather than one request per row -- the SQLite version
        loops because a local transaction makes that free; over HTTP it would
        not be.
        """
        result = UpdateResult()
        if not examples:
            return result
        try:
            rows = [
                {
                    "source": example.get("source", "review"),
                    "source_ref": example.get("source_ref"),
                    "era": example.get("era"),
                    "transaction_id": example.get("transaction_id"),
                    "transaction_date": example.get("transaction_date"),
                    "amount": example.get("amount"),
                    "details": example.get("details"),
                    "account": example.get("account"),
                    "committee_id": int(example["committee_id"]),
                    "purpose": example.get("purpose"),
                    "model_committee": example.get("model_committee"),
                    "model_confidence": example.get("model_confidence"),
                    "model_source": example.get("model_source"),
                    "sample_weight": example.get("sample_weight"),
                    "labeled_by": example.get("labeled_by", actor),
                    "natural_key": example.get("natural_key"),
                }
                for example in examples
            ]
            self._admin.table("labeled_examples").upsert(
                rows, on_conflict="natural_key", ignore_duplicates=True
            ).execute()
            # PostgREST's `ignore_duplicates` returns only the rows it actually
            # inserted, so this is the real count -- unlike the SQLite version's
            # per-row `cursor.rowcount` loop, there's no cheap way to also report
            # how many were skipped without a second query, and nothing reads
            # `unchanged` for this call today.
            result.updated = len(rows)
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def replace_members(
        self, term_id: str, members: list[dict], source_file: str, actor: str
    ) -> UpdateResult:
        """
        Replace one term's roster wholesale -- delete then insert, matching
        `sqlite_backend.py`. See that method's docstring for why replace and not
        merge: a membership export is a snapshot of who is on the roster *now*.

        Not atomic across the two requests the way the SQLite version's single
        transaction is. A partial failure here can leave the term's roster
        empty rather than reverted -- acceptable for now because a roster
        upload is a low-frequency, treasurer-supervised action with an
        immediately visible result (the Roster page would show 0 members), not
        a background process that could fail silently.
        """
        result = UpdateResult()
        if not term_id:
            result.error = "A term is required to store a roster."
            return result
        try:
            self._admin.table("members").delete().eq("term_id", term_id).execute()
            if members:
                rows = [
                    {
                        "term_id": term_id,
                        "full_name": member.get("full_name"),
                        "match_key": member.get("match_key"),
                        "alt_keys": ",".join(member.get("alt_keys") or ()) or None,
                        "preferred_name": member.get("preferred_name") or None,
                        "claims_paid": bool(member.get("claims_paid")),
                        "email": member.get("email") or None,
                        "ufid": member.get("ufid") or None,
                        "committee_id": member.get("committee_id"),
                        "notes": member.get("notes") or None,
                        "source_file": source_file,
                        "uploaded_by": actor,
                    }
                    for member in members
                ]
                self._admin.table("members").insert(rows).execute()
            result.updated = len(members)
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def add_member_alias(
        self, term_id: str, match_key: str, alias_key: str, actor: str
    ) -> UpdateResult:
        """Teach the roster a second name for one member. See sqlite_backend.py."""
        result = UpdateResult()
        if not (term_id and match_key and alias_key):
            result.error = "Term, member and alias are all required."
            return result
        try:
            existing = (
                self._admin.table("members")
                .select("alt_keys")
                .eq("term_id", term_id)
                .eq("match_key", match_key)
                .maybe_single()
                .execute()
            )
            if existing is None or existing.data is None:
                result.error = f"No member {match_key!r} on the {term_id} roster."
                return result
            keys = {
                part.strip()
                for part in str(existing.data.get("alt_keys") or "").split(",")
                if part.strip()
            }
            if alias_key == match_key or alias_key in keys:
                result.unchanged = 1
                return result
            keys.add(alias_key)
            self._admin.table("members").update(
                {"alt_keys": ",".join(sorted(keys))}
            ).eq("term_id", term_id).eq("match_key", match_key).execute()
            result.updated = 1
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def upsert_profile(
        self,
        email: str,
        role: str,
        committee_id: int | None,
        display_name: str,
        actor: str,
    ) -> UpdateResult:
        result = UpdateResult()
        email = normalize_email(email)
        if not email:
            result.error = "An email is required."
            return result
        if role not in {"member", "officer", "treasurer", "president", "admin"}:
            result.error = f"Unknown role {role!r}."
            return result
        try:
            self._admin.table("profiles").upsert(
                {
                    "email": email,
                    "display_name": display_name or None,
                    "role": role,
                    "committee_id": committee_id,
                    "created_by": actor,
                },
                on_conflict="email",
            ).execute()
            result.updated = 1
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def remove_profile(self, email: str) -> UpdateResult:
        result = UpdateResult()
        key = normalize_email(email)
        if not key:
            result.error = "An email is required."
            return result
        try:
            # Exact match, never a pattern: a wildcard here would delete every
            # profile the pattern happens to match, not just the one chosen.
            response = (
                self._admin.table("profiles").delete().eq("email", key).execute()
            )
            result.updated = len(response.data or [])
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    # --- Model runs and spend reviews -----------------------------------------
    #
    # Needs migrations/013_model_runs.sql. Until it is run, `fetch_llm_runs`
    # raises, `metering.open_run` reports the ledger unreadable, and no model
    # call is made -- the feature is off, not unmetered.

    def fetch_llm_runs(self) -> pd.DataFrame:
        data = self._admin.table("llm_runs").select("*").order("run_id", desc=True).execute().data
        return pd.DataFrame(data or [])

    def start_llm_run(
        self,
        *,
        purpose: str,
        model: str,
        actor: str,
        rows_planned: int,
        calls_planned: int,
        reserved_usd: float,
        stale_before: str,
        started_at: str,
    ) -> tuple[int | None, UpdateResult]:
        result = UpdateResult()
        try:
            self._admin.table("llm_runs").update(
                {"status": "abandoned", "error": "never finished; its reservation still counts"}
            ).eq("status", "running").lt("started_at", stale_before).execute()
            response = self._admin.table("llm_runs").insert(
                {
                    "purpose": purpose,
                    "model": model,
                    "status": "running",
                    "started_by": actor,
                    "started_at": started_at,
                    "rows_planned": int(rows_planned),
                    "calls_planned": int(calls_planned),
                    "reserved_usd": float(reserved_usd),
                }
            ).execute()
        except Exception as exc:  # noqa: BLE001
            # 23505: the one-running-run index refused a second run.
            if "23505" in str(exc) or "duplicate key" in str(exc).lower():
                result.error = "Another model run is in progress. Try again in a few minutes."
            else:
                result.error = f"{type(exc).__name__}: {exc}"
            return None, result
        rows = response.data or []
        if not rows:
            result.error = "The run could not be recorded, so no call is made."
            return None, result
        result.updated = 1
        return int(rows[0]["run_id"]), result

    def finish_llm_run(
        self,
        *,
        run_id: int,
        status: str,
        calls_made: int,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
        cost_usd: float,
        error: str | None,
    ) -> UpdateResult:
        result = UpdateResult()
        try:
            self._admin.table("llm_runs").update(
                {
                    "status": status,
                    "finished_at": _now(),
                    "calls_made": int(calls_made),
                    "input_tokens": int(input_tokens),
                    "output_tokens": int(output_tokens),
                    "cache_read_tokens": int(cache_read_tokens),
                    "cache_write_tokens": int(cache_write_tokens),
                    "cost_usd": float(cost_usd),
                    "error": error,
                }
            ).eq("run_id", int(run_id)).execute()
            result.updated = 1
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def fetch_spend_reviews(self) -> pd.DataFrame:
        frame = pd.DataFrame(self._select_all("spend_reviews"))
        if frame.empty:
            return frame
        frame["_rank"] = frame["status"].map(lambda status: 0 if status == "open" else 1)
        return (
            frame.sort_values(["_rank", "review_id"], ascending=[True, False], kind="stable")
            .drop(columns="_rank")
            .reset_index(drop=True)
        )

    def insert_spend_reviews(self, reviews: list[dict], actor: str) -> UpdateResult:
        result = UpdateResult()
        if not reviews:
            return result
        try:
            response = (
                self._admin.table("spend_reviews")
                .upsert(
                    reviews,
                    on_conflict="transaction_id,booked_committee_id",
                    ignore_duplicates=True,
                )
                .execute()
            )
            result.updated = len(response.data or [])
            result.unchanged = len(reviews) - result.updated
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def resolve_spend_review(
        self, review_id: int, status: str, actor: str, note: str = ""
    ) -> UpdateResult:
        result = UpdateResult()
        if status not in ("resolved", "dismissed"):
            result.error = f"Unknown status {status!r}."
            return result
        try:
            response = (
                self._admin.table("spend_reviews")
                .update(
                    {
                        "status": status,
                        "resolved_at": _now(),
                        "resolved_by": actor,
                        "resolution_note": (note or "").strip() or None,
                    }
                )
                .eq("review_id", int(review_id))
                .eq("status", "open")
                .execute()
            )
            if response.data:
                result.updated = 1
            else:
                result.unchanged = 1
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    # --- "This isn't ours" flags ---------------------------------------------
    #
    # Needs migrations/010_transaction_flags.sql, which has NOT been run against
    # any database. Like the other Supabase methods added for the VP portal, this
    # has never executed against a real Postgres instance. Reads go through the
    # secret-key client like every other read since 008_lock_down.sql.

    def fetch_flags(self, committee_ids: tuple[int, ...] | None = None) -> pd.DataFrame:
        query = self._admin.table("transaction_flags").select("*")
        if committee_ids is not None:
            ids = [int(committee_id) for committee_id in committee_ids]
            if not ids:
                return pd.DataFrame()
            query = query.in_("booked_to_committee_id", ids)
        frame = pd.DataFrame(query.order("flag_id", desc=True).execute().data or [])
        if frame.empty:
            return frame
        # Open first, then newest -- matching sqlite_backend.py. Sorted here
        # because PostgREST orders by column, not by a CASE expression.
        frame["_rank"] = frame["status"].map(lambda status: 0 if status == flags_domain.OPEN else 1)
        return frame.sort_values("_rank", kind="stable").drop(columns="_rank")

    def create_flag(
        self,
        transaction_id: int,
        allowed_committee_ids: tuple[int, ...],
        note: str,
        actor: str,
    ) -> UpdateResult:
        result = UpdateResult()
        cleaned, problem = flags_domain.clean_note(note)
        if problem:
            result.error = problem
            return result

        allowed = {int(committee_id) for committee_id in allowed_committee_ids}
        try:
            charge = (
                self._admin.table("transactions")
                .select("budget_category")
                .eq("transactionid", int(transaction_id))
                .maybe_single()
                .execute()
            )
            if charge is None or charge.data is None:
                result.error = f"Charge {transaction_id} does not exist."
                return result
            booked_to = charge.data.get("budget_category")
            if booked_to is None or int(booked_to) not in allowed:
                result.error = "That charge is not booked to your committee, so you cannot flag it."
                return result

            existing = (
                self._admin.table("transaction_flags")
                .select("flag_id")
                .eq("transaction_id", int(transaction_id))
                .eq("status", flags_domain.OPEN)
                .execute()
            )
            if existing.data:
                result.unchanged = 1
                return result

            self._admin.table("transaction_flags").insert(
                {
                    "transaction_id": int(transaction_id),
                    "booked_to_committee_id": int(booked_to),
                    "flagged_by": actor,
                    "note": cleaned,
                    "status": flags_domain.OPEN,
                }
            ).execute()
            self._audit(
                transaction_id=int(transaction_id),
                action="flag",
                actor=actor,
                field="not our charge",
                new_value=cleaned[:120],
            )
            result.updated = 1
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def resolve_flag(self, flag_id: int, status: str, actor: str, note: str = "") -> UpdateResult:
        result = UpdateResult()
        if status not in flags_domain.CLOSING_STATUSES:
            result.error = f"Unknown status {status!r}."
            return result
        try:
            existing = (
                self._admin.table("transaction_flags")
                .select("status,transaction_id")
                .eq("flag_id", int(flag_id))
                .maybe_single()
                .execute()
            )
            if existing is None or existing.data is None:
                result.error = f"Flag {flag_id} does not exist."
                return result
            if existing.data.get("status") != flags_domain.OPEN:
                result.unchanged = 1
                return result

            self._admin.table("transaction_flags").update(
                {
                    "status": status,
                    "resolved_at": _now(),
                    "resolved_by": actor,
                    "resolution_note": (note or "").strip() or None,
                }
            ).eq("flag_id", int(flag_id)).eq("status", flags_domain.OPEN).execute()
            self._audit(
                transaction_id=int(existing.data["transaction_id"]),
                action="flag_resolved",
                actor=actor,
                field=f"flag {flag_id}",
                old_value=flags_domain.OPEN,
                new_value=status,
            )
            result.updated = 1
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    # --- Committee plans and budget requests (migration 014) -------------------

    def _scoped(self, table: str, committee_ids: tuple[int, ...] | None, semester: str | None):
        query = self._admin.table(table).select("*")
        if committee_ids is not None:
            query = query.in_("committee_id", [int(i) for i in committee_ids])
        if semester is not None:
            query = query.eq("semester", semester)
        return query

    def fetch_plan_lines(
        self, committee_ids: tuple[int, ...] | None = None, semester: str | None = None
    ) -> pd.DataFrame:
        if committee_ids is not None and not committee_ids:
            return pd.DataFrame()
        frame = pd.DataFrame(
            self._scoped("committee_plan_lines", committee_ids, semester).execute().data or []
        )
        if frame.empty:
            return frame
        # Biggest planned amount first -- matching sqlite_backend.py.
        frame["amount"] = pd.to_numeric(frame["amount"], errors="coerce")
        return frame.sort_values(
            ["committee_id", "amount", "purpose"], ascending=[True, False, True], kind="stable"
        ).reset_index(drop=True)

    def replace_plan_lines(
        self,
        committee_id: int,
        semester: str,
        lines: list[dict],
        allowed_committee_ids: tuple[int, ...],
        actor: str,
    ) -> UpdateResult:
        """
        Delete the old plan, then insert the new one. Not atomic: if the insert
        fails the VP is told and the plan is empty until they save again. The
        unique (committee, semester, purpose) key would refuse an insert-first
        order, which would briefly need two copies of a line.
        """
        result = UpdateResult()
        if int(committee_id) not in {int(i) for i in allowed_committee_ids}:
            result.error = "You can only plan for your own committee."
            return result
        try:
            old = (
                self._admin.table("committee_plan_lines").select("line_id")
                .eq("committee_id", int(committee_id)).eq("semester", semester).execute()
            )
            old_ids = [row["line_id"] for row in (old.data or [])]
            if old_ids:
                self._admin.table("committee_plan_lines").delete().in_("line_id", old_ids).execute()
            if lines:
                self._admin.table("committee_plan_lines").insert([
                    {
                        "committee_id": int(committee_id),
                        "semester": semester,
                        "purpose": line["purpose"],
                        "amount": float(line["amount"]),
                        "note": line.get("note"),
                        "updated_by": actor,
                    }
                    for line in lines
                ]).execute()
            self._audit(
                transaction_id=None,
                action="plan",
                actor=actor,
                field=f"committee {int(committee_id)} / {semester}",
                old_value=f"{len(old_ids)} line(s)",
                new_value=f"{len(lines)} line(s)",
            )
            result.updated = len(lines) or 1
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def fetch_budget_requests(
        self, committee_ids: tuple[int, ...] | None = None, semester: str | None = None
    ) -> pd.DataFrame:
        if committee_ids is not None and not committee_ids:
            return pd.DataFrame()
        frame = pd.DataFrame(
            self._scoped("budget_requests", committee_ids, semester).execute().data or []
        )
        if frame.empty:
            return frame
        return frame.sort_values(["semester", "committee_id"], kind="stable").reset_index(drop=True)

    def save_budget_request(
        self,
        committee_id: int,
        semester: str,
        amount: float,
        reason: str,
        allowed_committee_ids: tuple[int, ...],
        actor: str,
    ) -> UpdateResult:
        result = UpdateResult()
        if int(committee_id) not in {int(i) for i in allowed_committee_ids}:
            result.error = "You can only send a request for your own committee."
            return result
        try:
            self._admin.table("budget_requests").upsert(
                {
                    "committee_id": int(committee_id),
                    "semester": semester,
                    "amount": float(amount),
                    "reason": reason,
                    "requested_by": actor,
                    "requested_at": datetime.now(timezone.utc).isoformat(),
                },
                on_conflict="committee_id,semester",
            ).execute()
            self._audit(
                transaction_id=None,
                action="budget request",
                actor=actor,
                field=f"committee {int(committee_id)} / {semester}",
                new_value=str(float(amount)),
            )
            result.updated = 1
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    # --- writes (pre-existing) -------------------------------------------------

    def insert_transactions(
        self, records: list[dict], file_name: str, actor: str
    ) -> UploadReceipt:
        """
        FINDING F4. Both writes happen inside one Postgres function, so the
        statement marker and the rows commit together or not at all. The RPC is
        defined in schema_postgres.sql as `import_statement`.
        """
        receipt = UploadReceipt(file_name=file_name)
        try:
            response = self._admin.rpc(
                "import_statement",
                {
                    "p_records": records,
                    "p_file_name": file_name,
                    "p_actor": actor,
                },
            ).execute()
            payload = (response.data or [{}])[0]
            receipt.inserted = int(payload.get("inserted", 0))
            receipt.duplicates_skipped = int(payload.get("skipped", 0))
            receipt.committed = True
        except Exception as exc:  # noqa: BLE001
            receipt.error = f"{type(exc).__name__}: {exc}"
        return receipt

    def update_transactions(
        self, changes: list[TransactionChange], actor: str
    ) -> UpdateResult:
        """
        FINDING F6. One batched RPC rather than one HTTP request per row.

        M10. `blocked` / `blocked_terms` come from migration 003, which taught
        the RPC to honour a closed term -- until then locking was enforced only
        in `sqlite_backend`, so closing a term did nothing in production while
        the Treasury page said otherwise. `.get(..., 0)` rather than `[...]` so
        a database still on the two-column version of the function degrades to
        the old behaviour instead of raising.
        """
        result = UpdateResult()
        if not changes:
            return result
        try:
            payload = [
                {
                    "transactionid": change.transaction_id,
                    "purpose": change.purpose,
                    "budget_category": change.budget_category,
                }
                for change in changes
            ]
            response = self._admin.rpc(
                "apply_transaction_edits", {"p_changes": payload, "p_actor": actor}
            ).execute()
            data = (response.data or [{}])[0]
            result.updated = int(data.get("updated", 0))
            result.unchanged = int(data.get("unchanged", 0))

            blocked = int(data.get("blocked", 0) or 0)
            if blocked:
                terms = str(data.get("blocked_terms") or "a closed term")
                result.error = (
                    f"{blocked} transaction(s) fall in {terms}, which is closed. "
                    f"Reopen the term before editing them."
                )
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def upsert_budgets(
        self, term_id: str, allocations: dict[int, float], actor: str
    ) -> UpdateResult:
        """
        The original deleted every budget for the term and re-inserted, which
        destroys history and leaves a window where the term has no budget at all.
        An upsert keyed on (termid, committeeid) has neither problem.
        """
        result = UpdateResult()
        try:
            rows = [
                {"termid": term_id, "committeeid": committee_id, "budget_amount": float(amount)}
                for committee_id, amount in allocations.items()
            ]
            self._admin.table("committeebudgets").upsert(
                rows, on_conflict="termid,committeeid"
            ).execute()
            result.updated = len(rows)
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def insert_term(
        self, term_id: str, semester: str, start_date: str, end_date: str, actor: str
    ) -> UpdateResult:
        result = UpdateResult()
        try:
            self._admin.table("terms").insert(
                {
                    "TermID": term_id,
                    "Semester": semester,
                    "start_date": start_date,
                    "end_date": end_date,
                }
            ).execute()
            result.updated = 1
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def upsert_merchants(self, rules: list[dict], actor: str) -> UpdateResult:
        result = UpdateResult()
        if not rules:
            return result
        try:
            self._admin.table("merchants").upsert(
                rules, on_conflict="merchant_key"
            ).execute()
            result.updated = len(rules)
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def record_statement_balance(self, balance: dict, actor: str) -> UpdateResult:
        result = UpdateResult()
        try:
            self._admin.table("statement_balances").upsert(
                balance, on_conflict="account,period_start,period_end"
            ).execute()
            result.updated = 1
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        return result

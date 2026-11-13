"""
Transactions hub: the ledger and the Review Queue under one nav entry. The
Import view left on 2026-11-13: transactions arrive by the automatic sync, and
an uploaded statement is checked on Treasury -> Reconcile.

CONSOLIDATION (Pebble). The merged pages are unchanged files, run one at a time
by shell.hub; the switcher renders beside each page's title. Remove this file
and restore the original entries in app.py to undo the merge.
"""

from __future__ import annotations

from ais_fmd import auth
from ais_fmd.ui import shell

auth.current_user()

shell.hub(
    "transactions_hub",
    [
        ("Transactions", "Transactions.py", auth.Role.MEMBER),
        ("Review Queue", "ReviewQueue.py", auth.Role.TREASURER),
    ],
)

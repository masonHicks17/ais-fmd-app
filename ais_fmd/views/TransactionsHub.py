"""
Transactions hub: statement import, the ledger and the Review Queue under one
nav entry. Statement upload moved here from Treasury (Import).

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
        # Treasurers land on Import: upload, Haiku, import and the spend
        # review run top to bottom there. Members never see it.
        ("Import", "ImportStatement.py", auth.Role.TREASURER),
        ("Transactions", "Transactions.py", auth.Role.MEMBER),
        ("Review Queue", "ReviewQueue.py", auth.Role.TREASURER),
    ],
)

"""
Transactions hub: the ledger, the Review Queue and statement upload under one
nav entry. Upload statement moved here from Treasury on 2026-11-08.

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
        ("Upload statement", "UploadStatement.py", auth.Role.TREASURER),
    ],
)

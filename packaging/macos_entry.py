"""PyInstaller entry point for A.R.S.app.

Lives under ``packaging/`` — not ``apps/desktop/ars_desktop/`` — on purpose: this is a
packaging concern (what PyInstaller's dependency-graph walk starts from), not app
source. It does exactly what ``python -m ars_desktop`` does; PyInstaller just needs a
real script file rather than a ``-m`` target.
"""

from __future__ import annotations

from ars_desktop.__main__ import main

if __name__ == "__main__":
    main()

"""Entry point for ``python -m c2hlsc_agent`` (the form used throughout the docs).

Equivalent to the installed ``c2hlsc-agent`` console script.
"""

from __future__ import annotations

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())

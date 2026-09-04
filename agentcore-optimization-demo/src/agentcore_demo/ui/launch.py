"""Console entry point for the Streamlit front end (``demo-ui``).

Thin wrapper that shells out to ``streamlit run <app.py>`` so users can launch the UI
with a single command instead of remembering the module path. Extra CLI args are passed
straight through to Streamlit (e.g. ``demo-ui --server.port 8502``).
"""

from __future__ import annotations

import sys
from pathlib import Path

APP = Path(__file__).with_name("app.py")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        from streamlit.web import cli as st_cli
    except ModuleNotFoundError:
        sys.stderr.write(
            "Streamlit is not installed. Install the optional UI extra:\n"
            '    pip install -e ".[ui]"\n'
        )
        return 1

    # streamlit's CLI reads sys.argv, so build the argv it expects.
    sys.argv = ["streamlit", "run", str(APP), *argv]
    return int(st_cli.main() or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

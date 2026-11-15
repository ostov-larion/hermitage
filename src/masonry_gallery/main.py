"""Точка входа: `ermitage [КАТАЛОГ]`."""
from __future__ import annotations

import sys

from .application import ErmitageApplication


def main(argv: list[str] | None = None) -> int:
    app = ErmitageApplication()
    return app.run(argv if argv is not None else sys.argv)


if __name__ == "__main__":
    sys.exit(main())

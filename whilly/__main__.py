"""Allow running whilly as a module: ``python -m whilly``."""

import sys

from whilly.cli import main

if __name__ == "__main__":
    sys.exit(main())

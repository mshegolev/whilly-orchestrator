"""Allow running ralph as a module: ``python -m ralph``."""

import sys

from ralph.cli import main

if __name__ == "__main__":
    sys.exit(main())

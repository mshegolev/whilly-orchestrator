#!/usr/bin/env python3
"""Development entry point — run Whilly directly from a source checkout.

Equivalent to ``python -m whilly`` or the ``whilly`` console script
created by ``pip install``. Provided so you can ``./whilly.py`` from
a cloned repo without installing anything.
"""

import sys

if __name__ == "__main__":
    from whilly.cli import main

    sys.exit(main())

"""Entry point for launchd and the README commands: python bump.py <command>.

The code lives in the wallabump package; `python -m wallabump` does the same.
"""

import sys

from wallabump.__main__ import main

if __name__ == "__main__":
    sys.exit(main())

"""Run from source for development; farm users install the standalone release.

The packaged application has its own native-only bootstrap and bundled Python.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from proxy import main

if __name__ == '__main__':
    main()

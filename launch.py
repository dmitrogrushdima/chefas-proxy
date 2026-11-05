"""Development launcher. Distributed builds have their own native-only launcher."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from proxy import main

if __name__ == '__main__':
    main()

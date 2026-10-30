"""The `vorpalfusion` command: the sidecar (live.py) from this folder, its modules beside it."""
import sys
from pathlib import Path


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from live import main as run

    run()


if __name__ == "__main__":
    main()

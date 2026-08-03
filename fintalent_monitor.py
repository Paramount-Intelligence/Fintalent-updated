"""Backward-compatible shim — production entrypoint is monitor.py """
import sys
from monitor import main

if __name__ == "__main__":
    raise SystemExit(main())

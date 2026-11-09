"""PyInstaller entry point for the windowed app (EspSync.exe)."""

import sys

from espsync.gui.app import main

if __name__ == "__main__":
    sys.exit(main())

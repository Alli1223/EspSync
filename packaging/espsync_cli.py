"""PyInstaller entry point for the console tool (espsync-cli.exe)."""

import sys

from espsync.cli import main

if __name__ == "__main__":
    sys.exit(main())

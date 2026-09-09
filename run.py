"""Entry point for the desktop app (and the PyInstaller build)."""
import sys

if __name__ == "__main__":
    if len(sys.argv) > 1:
        from trader.cli import main as cli_main
        cli_main(sys.argv[1:])
    else:
        from trader.gui.app import main
        sys.exit(main())

"""Public command entry point; implementation lives in app.cli."""

from app.cli.change_order import main

if __name__ == "__main__":
    raise SystemExit(main())

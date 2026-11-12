"""Public command entry point; implementation lives in app.cli."""

from app.cli.batch_orders import main


if __name__ == "__main__":
    raise SystemExit(main())

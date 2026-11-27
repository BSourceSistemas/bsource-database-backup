"""Compatibility entry point. Prefer the `backup` console command."""

from app.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

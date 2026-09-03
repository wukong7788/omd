"""OMD command line entry point."""

from __future__ import annotations

import argparse
import sys

from .core.errors import OhMyDataError
from .providers.sec.cli import add_nport_commands, run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omd")
    commands = parser.add_subparsers(dest="command", required=True)
    add_nport_commands(commands)
    args = parser.parse_args(argv)
    try:
        if args.command == "sec":
            return run(args)
    except OhMyDataError:
        print("SEC operation failed", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, RuntimeError, KeyError, TypeError):
        print("SEC operation failed", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

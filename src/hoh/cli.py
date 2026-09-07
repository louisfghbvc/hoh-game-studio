from __future__ import annotations

import argparse
from collections.abc import Sequence

from hoh.config import handle_doctor, handle_init


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hoh")
    commands = parser.add_subparsers(dest="command")

    init_parser = commands.add_parser("init")
    init_parser.add_argument("--project", default=".")
    init_parser.add_argument("--adapter", required=True)
    init_parser.add_argument("--model", required=True)
    init_parser.add_argument("--reasoning-effort", required=True)
    init_parser.set_defaults(handler=handle_init)

    doctor_parser = commands.add_parser("doctor")
    doctor_parser.add_argument("--project", default=".")
    doctor_parser.set_defaults(handler=handle_doctor)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code)
    handler = getattr(arguments, "handler", None)
    if handler is None:
        parser.print_usage(__import__("sys").stderr)
        return 2
    return handler(arguments)


def entrypoint() -> None:
    raise SystemExit(main())

"""dh command line."""
from __future__ import annotations

import argparse

COMMANDS = ["start", "stop", "status", "deals", "show", "verify", "replay", "eval", "playbook", "books", "round"]


def _stub(args: argparse.Namespace) -> int:
    print("not implemented")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dh", description="Deal Hunter")
    sub = p.add_subparsers(dest="command", metavar="command")
    for name in COMMANDS:
        sp = sub.add_parser(name, help=f"{name} (stub)")
        sp.set_defaults(func=_stub)
    from . import probe

    sp = sub.add_parser("probe", help="run one source adapter search live and print the listings")
    probe.add_arguments(sp)
    sp.set_defaults(func=probe.run)
    return p


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

"""pell CLI — v0.

Usage:
    pell build <file.pell>            -> emits to stdout
    pell build <file.pell> -o <out>   -> writes to file
    pell build <dir>                  -> compiles every .pell file in the dir
    pell parse <file.pell>            -> prints AST
    pell tokens <file.pell>           -> prints token stream
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .emitter import emit
from .lexer import tokenize
from .parser import parse, ParseError
from .lexer import LexError


def cmd_build(args: argparse.Namespace) -> int:
    inputs = _collect_inputs(args.input)
    if not inputs:
        print(f"pell: no .pell files found in {args.input!r}", file=sys.stderr)
        return 2
    if args.output and len(inputs) > 1:
        print("pell: -o requires a single input file (or a directory output)", file=sys.stderr)
        return 2
    failures = 0
    for src_path in inputs:
        try:
            src = src_path.read_text()
            module = parse(src, str(src_path))
            sql = emit(module)
        except (LexError, ParseError) as e:
            print(f"pell: {e}", file=sys.stderr)
            failures += 1
            continue
        # destination
        if args.output:
            out_path = Path(args.output)
        elif args.dir_output:
            out_path = Path(args.dir_output) / (src_path.stem + ".sql")
        else:
            out_path = None
        if out_path is None:
            print(sql)
        else:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(sql)
            print(f"  → {out_path}")
    return 1 if failures else 0


def cmd_parse(args: argparse.Namespace) -> int:
    path = Path(args.input)
    src = path.read_text()
    try:
        m = parse(src, str(path))
    except (LexError, ParseError) as e:
        print(f"pell: {e}", file=sys.stderr)
        return 1
    _dump_ast(m)
    return 0


def cmd_tokens(args: argparse.Namespace) -> int:
    path = Path(args.input)
    src = path.read_text()
    try:
        for t in tokenize(src, str(path)):
            print(t)
    except LexError as e:
        print(f"pell: {e}", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------


def _collect_inputs(input_arg: str) -> list[Path]:
    p = Path(input_arg)
    if p.is_dir():
        return sorted(p.glob("*.pell"))
    if p.is_file():
        return [p]
    return []


def _dump_ast(node, depth: int = 0) -> None:
    """Crude AST pretty-printer."""
    from dataclasses import is_dataclass, fields
    pad = "  " * depth
    if is_dataclass(node):
        cls = type(node).__name__
        print(f"{pad}{cls}")
        for f in fields(node):
            if f.name == "loc":
                continue
            v = getattr(node, f.name)
            print(f"{pad}  {f.name}:", end="")
            if isinstance(v, list):
                print()
                for item in v:
                    _dump_ast(item, depth + 2)
            elif is_dataclass(v):
                print()
                _dump_ast(v, depth + 2)
            else:
                print(f" {v!r}")
    else:
        print(f"{pad}{node!r}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pell", description=f"pell compiler v{__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="compile .pell to PL/SQL")
    b.add_argument("input", help="path to a .pell file or a directory of them")
    b.add_argument("-o", "--output", help="output .sql file (single input only)")
    b.add_argument("-d", "--dir-output", help="output directory (when compiling a dir)")
    b.set_defaults(func=cmd_build)

    pa = sub.add_parser("parse", help="print AST for a .pell file")
    pa.add_argument("input")
    pa.set_defaults(func=cmd_parse)

    t = sub.add_parser("tokens", help="print token stream for a .pell file")
    t.add_argument("input")
    t.set_defaults(func=cmd_tokens)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

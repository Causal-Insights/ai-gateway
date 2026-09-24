#!/usr/bin/env python3
"""Install/refresh the canonical skill and verify byte-for-byte correspondence."""
import argparse
import os
from pathlib import Path
import shutil
import sys

SOURCE = Path(__file__).resolve().parents[1] / "skills/price-verification"


def files(root):
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--destination", type=Path, default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "skills/price-verification")
    args = parser.parse_args()
    target = args.destination
    if SOURCE.resolve() == target.resolve() or target.is_symlink():
        parser.error("Destination must be a separate, non-symlink skill directory")
    expected = files(SOURCE)
    if not args.check:
        if target.exists() and any(p.is_symlink() for p in target.rglob("*")):
            parser.error("Destination contains symlinks; inspect it before refreshing")
        target.mkdir(parents=True, exist_ok=True)
        for relative in expected:
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SOURCE / relative, destination)
        for relative in files(target).keys() - expected.keys():
            (target / relative).unlink()
    if not target.exists() or files(target) != expected:
        print("Price Verification personal copy differs from the canonical source", file=sys.stderr)
        return 1
    print(f"Price Verification verified: {len(expected)} matching files at {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

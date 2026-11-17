"""Copy only Lox agent/command files into a chosen checkout; never global config."""

import argparse
import shutil
from datetime import UTC, datetime
from pathlib import Path


def main():
    source = Path(__file__).parent / "opencode"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=source.parents[2])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    destination = args.target.resolve() / ".opencode"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    for file in sorted(source.glob("*/*.md")):
        target = destination / file.relative_to(source)
        if target.exists() and target.read_bytes() == file.read_bytes():
            print(f"Unchanged: {target}")
            continue
        print(f"Install: {target}")
        if args.dry_run:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.copy2(target, target.with_name(target.name + f".backup-{stamp}"))
        shutil.copy2(file, target)


if __name__ == "__main__":
    main()

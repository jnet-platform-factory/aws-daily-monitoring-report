#!/usr/bin/env python3
"""Assert the built artifact can actually start.

The failure this exists for: `sam package --template-file template.yaml` -- the
SOURCE template -- after `sam build` has produced a built one under
.aws-sam/build/. Packaging the source template uploads app/ verbatim, without
the dependencies `sam build` installed, and every other gate still passes. The
only thing that notices is a Lambda at cold start, after the version is
immutable:

    Runtime.ImportModuleError: Unable to import module 'src.handler'

This artifact has no third-party dependencies today, so it would dodge that
exact bug. The gate stays anyway: it also checks the handler module itself is
in the build, and it starts checking dependencies the day one is added to
app/requirements.txt.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / ".aws-sam" / "build"

# Distribution name in requirements.txt -> the package directory pip installs,
# where the two differ.
IMPORT_NAMES = {}


def required() -> list[str]:
    text = (ROOT / "app" / "requirements.txt").read_text()
    names = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        dist = re.split(r"[=<>!~\[]", line, 1)[0].strip()
        names.append(IMPORT_NAMES.get(dist, dist.replace("-", "_")))
    return names


def main() -> int:
    if not BUILD.is_dir():
        print(f"check-build: {BUILD} does not exist — run `sam build` first")
        return 2

    funcs = [d for d in BUILD.iterdir() if d.is_dir() and (d / "src").is_dir()]
    if not funcs:
        print(f"check-build: no built function directories under {BUILD}")
        return 2

    wanted = required()
    problems = []
    for func in sorted(funcs):
        missing = [n for n in wanted if not (func / n).exists()]
        if not (func / "src" / "handler.py").is_file():
            missing.append("src/handler.py")
        status = "ok" if not missing else "MISSING " + ", ".join(missing)
        print(f"  {func.name}: {status}")
        if missing:
            problems.append(func.name)

    if problems:
        print(
            "\ncheck-build: the built artifact is incomplete. Packaging this publishes "
            "a function that cannot start.\n"
            "Package the BUILT template (.aws-sam/build/template.yaml), not the source one."
        )
        return 1

    print(f"check-build: all {len(funcs)} function(s) are complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())

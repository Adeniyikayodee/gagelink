"""Build the .mcpb bundle: the whole server as one file a client installs by opening it.

The other install paths ask for something first. `pip install gagelink` asks for a Python
environment, and the copy-paste configuration asks somebody to find a JSON file and edit
it without breaking the servers already in it. A bundle asks for neither, which matters
because the step somebody abandons is the step before they have seen the thing work.

The manifest is generated from the running package rather than written beside it. The tool
and prompt lists in a bundle are what a client shows before anything is installed, and a
copy of them maintained by hand is a copy that goes stale in the direction of promising
tools that are not there.

    python scripts/build_bundle.py dist/gagelink.mcpb

The result is a zip with manifest.json at its root, the wheel's dependencies vendored under
lib/, and a launcher that runs the same console script `gagelink-mcp` runs. Every
dependency here is pure Python, so one bundle serves every platform; a compiled one would
need building per platform and this deliberately has none.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import gagelink  # noqa: E402
from gagelink.catalogue import PROMPTS  # noqa: E402
from gagelink.server import TOOLS  # noqa: E402

#: What a client runs. It imports rather than re-implements, so the bundle and the console
#: script cannot start the server two different ways.
LAUNCHER = '''"""Entry point for the bundled server. The console script by another name."""

import os
import sys

# Checked before the imports because the manifest asks for python3 by name and a host that
# has one older than this runs it anyway. Without this the failure is an ImportError from
# inside a dependency, several frames deep, naming a symbol from the typing module. That
# tells somebody installing a bundle nothing they can act on.
if sys.version_info < (3, 10):
    running = ".".join(str(n) for n in sys.version_info[:3])
    sys.exit(
        f"gagelink needs Python 3.10 or later and this is {running} at "
        f"{sys.executable}. Point the bundle at a newer interpreter, or install the "
        f"package with `uvx --from gagelink gagelink-mcp`, which fetches its own."
    )

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from gagelink.server import main

if __name__ == "__main__":
    raise SystemExit(main())
'''


def manifest() -> dict:
    """The bundle manifest, with every list read from the package it describes."""
    return {
        "manifest_version": "0.2",
        "name": "gagelink",
        "display_name": "GageLink",
        "version": gagelink.__version__,
        "description": (
            "Hydrology data for AI agents, with the reference frames kept attached."
        ),
        "long_description": (
            "River levels, streamflow, flood forecasts, drainage basins and satellite "
            "water levels from USGS, NOAA, France's Hub'Eau, the UK Environment Agency "
            "and SWOT. Every value arrives carrying its unit, the datum it was measured "
            "from, its timezone, and whether the record is provisional or approved.\n\n"
            "A river stage is measured upward from the gage's own zero and a surveyed "
            "levee crest from a national datum. Both are lengths in feet, so subtracting "
            "one from the other produces a number that reads as freeboard and that a "
            "units library will pass. GageLink refuses that subtraction and returns the "
            "offset that makes it well defined, with how well that offset is known."
        ),
        "author": {"name": "Kayode Adeniyi"},
        "homepage": "https://adeniyikayodee.github.io/gagelink/",
        "documentation": "https://github.com/Adeniyikayodee/gagelink#readme",
        "support": "https://github.com/Adeniyikayodee/gagelink/issues",
        "repository": {
            "type": "git",
            "url": "https://github.com/Adeniyikayodee/gagelink",
        },
        "license": "MIT",
        "keywords": [
            "hydrology",
            "water",
            "streamflow",
            "river",
            "flood",
            "usgs",
            "noaa",
            "swot",
        ],
        "server": {
            "type": "python",
            "entry_point": "server/main.py",
            "mcp_config": {
                "command": "python3",
                "args": ["${__dirname}/server/main.py"],
                # Empty rather than absent when nothing was configured. The server treats
                # an empty key as no key and falls back to the 50-an-hour allowance, which
                # is the behaviour the manifest promises by leaving this optional.
                "env": {"GAGELINK_API_KEY": "${user_config.api_key}"},
            },
        },
        "tools": [
            {"name": t["name"], "description": t["description"].split(".")[0] + "."}
            for t in TOOLS
        ],
        "prompts": [
            {
                "name": p["name"],
                "description": p["description"],
                "arguments": [a["name"] for a in p["arguments"]],
            }
            for p in PROMPTS
        ],
        "tools_generated": False,
        "user_config": {
            "api_key": {
                "type": "string",
                "title": "USGS API key",
                "description": (
                    "Optional. Free from https://api.waterdata.usgs.gov/signup. Raises "
                    "the allowance from 50 requests an hour to 1000. The server runs "
                    "without one."
                ),
                "sensitive": True,
                "required": False,
            }
        },
        "compatibility": {
            "platforms": ["darwin", "win32", "linux"],
            "runtimes": {"python": ">=3.10"},
        },
    }


def build(destination: Path) -> Path:
    """Assemble the bundle and write it, replacing anything already at that path."""
    staging = destination.parent / f"{destination.stem}.staging"
    if staging.exists():
        shutil.rmtree(staging)
    (staging / "server" / "lib").mkdir(parents=True)

    # Installed from the source tree rather than from PyPI, so a bundle built at a tag
    # carries that tag's code rather than whatever the index is serving.
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--quiet",
            "--target",
            str(staging / "server" / "lib"),
            str(ROOT),
        ],
        check=True,
    )

    (staging / "server" / "main.py").write_text(LAUNCHER)
    (staging / "manifest.json").write_text(json.dumps(manifest(), indent=2) + "\n")
    shutil.copy(ROOT / "README.md", staging / "README.md")
    shutil.copy(ROOT / "LICENSE", staging / "LICENSE")

    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as bundle:
        # manifest.json first, so a reader that streams the archive has the manifest before
        # it has the megabytes of dependency it describes.
        bundle.write(staging / "manifest.json", "manifest.json")
        for path in sorted(staging.rglob("*")):
            if path.is_file() and path.name != "manifest.json":
                bundle.write(path, str(path.relative_to(staging)))

    shutil.rmtree(staging)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "destination",
        nargs="?",
        default=str(ROOT / "dist" / f"gagelink-{gagelink.__version__}.mcpb"),
        help="Where to write the bundle",
    )
    args = parser.parse_args(argv)
    built = build(Path(args.destination))
    size = built.stat().st_size
    print(f"{built} ({size // 1024} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

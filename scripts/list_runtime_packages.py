"""Emit the installed runtime distributions as an auditable requirements file."""

from __future__ import annotations

import importlib.metadata


def main() -> None:
    packages = sorted(
        {
            (distribution.metadata["Name"].lower().replace("_", "-"), distribution.version)
            for distribution in importlib.metadata.distributions()
            if distribution.metadata["Name"]
        }
    )
    for name, version in packages:
        if name != "atlas-dd":
            print(f"{name}=={version}")


if __name__ == "__main__":
    main()

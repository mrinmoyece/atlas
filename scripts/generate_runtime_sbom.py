"""Emit a deterministic CycloneDX SBOM for the current Python environment."""

from __future__ import annotations

import importlib.metadata
import json
import urllib.parse
import uuid


def main() -> None:
    """Write installed distributions, including Atlas, as CycloneDX JSON."""
    packages = sorted(
        {
            (distribution.metadata["Name"].lower().replace("_", "-"), distribution.version)
            for distribution in importlib.metadata.distributions()
            if distribution.metadata["Name"]
        }
    )
    components = [
        {
            "type": "library",
            "bom-ref": f"pkg:pypi/{urllib.parse.quote(name)}@{urllib.parse.quote(version)}",
            "name": name,
            "version": version,
            "purl": f"pkg:pypi/{urllib.parse.quote(name)}@{urllib.parse.quote(version)}",
        }
        for name, version in packages
        if name != "atlas-dd"
    ]
    fingerprint = "\n".join(f"{name}=={version}" for name, version in packages)
    document = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, fingerprint)}",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "bom-ref": "pkg:pypi/atlas-dd@0.1.0",
                "name": "atlas-dd",
                "version": "0.1.0",
                "purl": "pkg:pypi/atlas-dd@0.1.0",
            }
        },
        "components": components,
    }
    print(json.dumps(document, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

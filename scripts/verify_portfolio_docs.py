"""Validate local Markdown links, diagram sources and real screenshot manifests."""

import hashlib
import json
import re
import struct
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    markdown = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    links = 0
    errors = []
    for path in markdown:
        for raw in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", path.read_text()):
            target = raw.strip().strip("<>")
            if target.startswith(("https:", "http:", "mailto:", "#")):
                continue
            target = unquote(target.split("#")[0])
            if not target:
                continue
            links += 1
            resolved = (path.parent / target).resolve()
            if not resolved.is_relative_to(ROOT) or not resolved.exists():
                errors.append({"file": str(path.relative_to(ROOT)), "target": target})
    diagrams = sorted((ROOT / "docs/diagrams").glob("*.mmd"))
    assert len(diagrams) == 8
    diagram_doc = (ROOT / "docs/ARCHITECTURE_DIAGRAMS.md").read_text()
    for path in diagrams:
        assert path.read_text() in diagram_doc, "Embedded diagram differs from source"
    folder = ROOT / "docs/assets/screenshots"
    manifest = json.loads((folder / "manifest.json").read_text())
    assert len(manifest["screenshots"]) == 13
    assert len(list(folder.glob("*.png"))) == 13
    for item in manifest["screenshots"]:
        data = (folder / item["file"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == item["sha256"]
        assert len(data) == item["bytes"]
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        assert struct.unpack(">II", data[16:24]) == (item["width"], item["height"])
    description = (
        (ROOT / "docs/LINKEDIN_PROJECT.md")
        .read_text()
        .split("## Description (under 2,000 characters)\n\n")[1]
        .split("\n## Five skills")[0]
    )
    assert len(description) < 2000
    result = {
        "markdown_files": len(markdown),
        "local_links_checked": links,
        "broken_local_links": errors,
        "diagram_sources_match_embedded": len(diagrams),
        "screenshots_verified_sha256_dimensions_size": 13,
        "linkedin_description_characters": len(description),
        "scope": "Local links and artifact consistency only; external links and GitHub rendering not verified",
    }
    (ROOT / "docs/evidence/phase13a-doc-checks.json").write_text(
        json.dumps(result, indent=2) + "\n"
    )
    print(json.dumps(result, indent=2))
    if errors:
        raise SystemExit("Broken local links must be corrected")


if __name__ == "__main__":
    main()

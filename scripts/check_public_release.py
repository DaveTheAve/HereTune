from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_VERSION = "0.13.0"

FORBIDDEN_TRACKED_NAMES = {
    "config.toml",
    "config.recommended.toml",
    "config.unlimited.toml",
    "BRANCH_MERGE_NOTES.md",
    "UI_MIGRATION_NOTES.md",
    "VALIDATION_REPORT.md",
    "favicon_source.png",
    "requirements.txt",
    "requirements-dev.txt",
}
FORBIDDEN_TEXT = [
    r"192\.168\.0\.13",
    "Sex" + "LikeReal",
    r"E:\\\\balancer",
    r"C:\\\\Users\\\\DevDa",
]
TEXT_SUFFIXES = {".py", ".js", ".css", ".html", ".md", ".toml", ".txt", ".bat", ".yml", ".yaml"}

EXPECTED_ROOT_MARKDOWN = {
    "AGENTS.md",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "COMMERCIAL_USE.md",
    "CONTRIBUTING.md",
    "CONTRIBUTOR_LICENSE_AGREEMENT.md",
    "README.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
}
EXPECTED_DOC_MARKDOWN = {
    "CONFIGURATION.md",
    "TROUBLESHOOTING.md",
    "HERESPHERE_API_QUICK_REFERENCE.md",
    "HERESPHERE_API_REFERENCE.md",
    "HERESPHERE_MODDING_AND_CUSTOMIZATION_GUIDE.md",
}


def fail(message: str) -> None:
    print(f"PUBLIC RELEASE CHECK FAILED: {message}", file=sys.stderr)
    raise SystemExit(1)


def _version_checks() -> None:
    package_text = (ROOT / "heretune/__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', package_text, re.M)
    package_version = match.group(1) if match else None
    with (ROOT / "pyproject.toml").open("rb") as handle:
        project_version = tomllib.load(handle)["project"]["version"]
    player = (ROOT / "heretune/static/player.js").read_text(encoding="utf-8")
    index = (ROOT / "heretune/static/index.html").read_text(encoding="utf-8")
    player_html = (ROOT / "heretune/static/player.html").read_text(encoding="utf-8")
    issue = (ROOT / ".github/ISSUE_TEMPLATE/bug_report.yml").read_text(encoding="utf-8")

    if package_version != PUBLIC_VERSION or project_version != PUBLIC_VERSION:
        fail(f"version mismatch: package={package_version!r}, project={project_version!r}, expected={PUBLIC_VERSION!r}")
    expected_fragments = {
        "player.js": f"const PLAYER_BUILD = '{PUBLIC_VERSION}';",
        "index.html styles": f"styles.css?v={PUBLIC_VERSION}",
        "index.html app": f"app.js?v={PUBLIC_VERSION}",
        "player.html css": f"player.css?v={PUBLIC_VERSION}",
        "player.html js": f"player.js?v={PUBLIC_VERSION}",
        "bug form": f'placeholder: "{PUBLIC_VERSION}"',
    }
    texts = {
        "player.js": player,
        "index.html styles": index,
        "index.html app": index,
        "player.html css": player_html,
        "player.html js": player_html,
        "bug form": issue,
    }
    for name, fragment in expected_fragments.items():
        if fragment not in texts[name]:
            fail(f"public version/cache-buster mismatch in {name}: expected {fragment!r}")

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    release_headings = re.findall(r"^#{1,3}\s+v(\d+\.\d+\.\d+)\b", changelog, re.M | re.I)
    if release_headings != [PUBLIC_VERSION]:
        fail(f"CHANGELOG must contain exactly one public release heading for v{PUBLIC_VERSION}; found {release_headings}")


def _markdown_checks() -> None:
    root_md = {path.name for path in ROOT.glob("*.md")}
    docs_md = {path.name for path in (ROOT / "docs").glob("*.md")}
    if root_md != EXPECTED_ROOT_MARKDOWN:
        fail(f"unexpected root Markdown set: {sorted(root_md)}")
    if docs_md != EXPECTED_DOC_MARKDOWN:
        fail(f"unexpected docs Markdown set: {sorted(docs_md)}")
    if (ROOT / "docs/development").exists():
        fail("docs/development is not part of the public documentation set")

    markdown_files = [*ROOT.glob("*.md"), *ROOT.joinpath("docs").glob("*.md")]
    link_pattern = re.compile(r"\]\(([^)]+\.md(?:#[^)]+)?)\)", re.I)
    for markdown in markdown_files:
        text = markdown.read_text(encoding="utf-8", errors="ignore")
        for match in link_pattern.finditer(text):
            target = match.group(1).split("#", 1)[0]
            if "://" in target:
                continue
            resolved = markdown.parent / target
            if not resolved.exists():
                fail(f"broken local Markdown link in {markdown.relative_to(ROOT)}: {target}")


def main() -> None:
    for name in FORBIDDEN_TRACKED_NAMES:
        if (ROOT / name).exists():
            fail(f"remove internal/private repository file: {name}")

    configs = sorted(path.name for path in ROOT.glob("config*.toml"))
    if configs != ["config.example.toml"]:
        fail(f"expected exactly config.example.toml; found {configs}")

    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if any(part in {".git", ".venv", "venv", "data", "build", "dist", "dist-release", "__pycache__", ".pytest_cache"} for part in rel.parts):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name != "LICENSE":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in FORBIDDEN_TEXT:
            if re.search(pattern, text, re.I):
                fail(f"private/environment-specific text matched {pattern!r} in {rel}")

    required = [
        "LICENSE", "COMMERCIAL_USE.md", "README.md", "CHANGELOG.md", "SECURITY.md", "CONTRIBUTING.md",
        "CONTRIBUTOR_LICENSE_AGREEMENT.md", "CODE_OF_CONDUCT.md", "AGENTS.md", "THIRD_PARTY_NOTICES.md",
        "docs/CONFIGURATION.md", "docs/TROUBLESHOOTING.md",
        "docs/HERESPHERE_API_QUICK_REFERENCE.md", "docs/HERESPHERE_API_REFERENCE.md",
        "docs/HERESPHERE_MODDING_AND_CUSTOMIZATION_GUIDE.md",
        "docs/images/dashboard.png", "docs/images/native-image-grade.png", "docs/images/scene-grade.png",
        "docs/images/analysis-evidence.png", "docs/images/vr-verification.png",
        ".github/workflows/ci.yml", ".github/workflows/codeql.yml", ".github/workflows/release.yml",
        "heretune/static/favicon.ico", "heretune/static/favicon.svg", "heretune/static/favicon-180.png",
    ]
    missing = [name for name in required if not (ROOT / name).exists()]
    if missing:
        fail(f"missing public-release files: {missing}")

    _markdown_checks()
    _version_checks()

    example = (ROOT / "config.example.toml").read_text(encoding="utf-8")
    if not re.search(r'^roots\s*=\s*\[\s*\]\s*$', example, re.M):
        fail("config.example.toml must ship with an empty library roots list")
    if re.search(r'^root\s*=', example, re.M):
        fail("public config must not expose the removed single-root key")

    # Private-development HereTune version history belongs nowhere in first-public
    # user/runtime surfaces. Preserved HereSphere reference docs are excluded here
    # because their own upstream release numbers are part of the technical record.
    history_targets = [
        ROOT / "README.md", ROOT / "CHANGELOG.md", ROOT / "AGENTS.md", ROOT / "config.example.toml",
        *ROOT.joinpath("heretune").rglob("*.py"), *ROOT.joinpath("heretune/static").glob("*.js"),
        *ROOT.joinpath("heretune/static").glob("*.css"), *ROOT.joinpath("heretune/static").glob("*.html"),
    ]
    private_version = re.compile(r"\bv0\.(?:[0-9]+)(?:\.[0-9]+)?\b", re.I)
    for path in history_targets:
        text = path.read_text(encoding="utf-8", errors="ignore")
        matches = sorted(set(private_version.findall(text)))
        allowed = {f"v{PUBLIC_VERSION}"}
        unexpected = [item for item in matches if item not in allowed]
        if unexpected:
            fail(f"non-public version history remains in {path.relative_to(ROOT)}: {unexpected}")

    print("Public release hygiene: PASS")


if __name__ == "__main__":
    main()

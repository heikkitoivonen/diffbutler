"""Build the public one-page website from the repository README."""

import re
import shutil
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from markdown_it import MarkdownIt

REPOSITORY = "https://github.com/heikkitoivonen/diffbutler"


def render_readme(markdown: str) -> str:
    """Keep README content, with section anchors and repository-relative links."""
    parser = MarkdownIt("js-default")
    tokens = parser.parse(markdown)
    anchors: set[str] = set()
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            heading = tokens[index + 1]
            title = "".join(child.content for child in heading.children or [])
            slug = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
            anchor = slug
            suffix = 1
            while anchor in anchors:
                anchor = f"{slug}-{suffix}"
                suffix += 1
            anchors.add(anchor)
            token.attrSet("id", anchor)
        for child in token.children or []:
            href = child.attrGet("href")
            if (
                isinstance(href, str)
                and href
                and not href.startswith("#")
                and not urlsplit(href).scheme
            ):
                child.attrSet("href", urljoin(f"{REPOSITORY}/blob/main/", href))
    return parser.renderer.render(tokens, parser.options, {})


def build_site(root: Path, output: Path) -> None:
    """Write only the public page and its assets to the deployment directory."""
    content = render_readme((root / "README.md").read_text(encoding="utf-8"))
    template = (root / "website" / "index.html").read_text(encoding="utf-8")
    output.mkdir(parents=True, exist_ok=True)
    (output / "index.html").write_text(
        template.replace("<!-- README -->", content), encoding="utf-8"
    )
    shutil.copyfile(root / "website" / "style.css", output / "style.css")
    shutil.copyfile(root / "diffbutler.png", output / "diffbutler.png")


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    output = root / ".cache" / "site"
    build_site(root, output)
    print(f"Built website at {output / 'index.html'}")

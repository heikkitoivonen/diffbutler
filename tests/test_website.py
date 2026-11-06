"""The public site stays faithful to the README and works outside GitHub."""

from pathlib import Path

from scripts.build_site import RAW, REPOSITORY, build_site, render_readme


def test_readme_links_work_on_the_custom_domain() -> None:
    rendered = render_readme(
        "[Guide](docs/github.md#authentication-options) "
        "[Contribute](CONTRIBUTING.md) [License](LICENSE.txt) "
        "[Section](#install) [External](https://example.com/) "
        "[Email](mailto:hello@example.com)"
    )
    assert f'href="{REPOSITORY}/blob/main/docs/github.md#authentication-options"' in rendered
    assert f'href="{REPOSITORY}/blob/main/CONTRIBUTING.md"' in rendered
    assert f'href="{REPOSITORY}/blob/main/LICENSE.txt"' in rendered
    assert 'href="#install"' in rendered
    assert 'href="https://example.com/"' in rendered
    assert 'href="mailto:hello@example.com"' in rendered


def test_main_branch_images_are_served_from_the_site() -> None:
    assets: set[str] = set()
    rendered = render_readme(
        f"![Shot]({RAW}diffbutler.png) ![Flow]({RAW}docs/diagrams/flow.svg) "
        "![Badge](https://example.com/badge.svg)",
        assets,
    )
    assert 'src="diffbutler.png"' in rendered
    assert 'src="docs/diagrams/flow.svg"' in rendered
    assert 'src="https://example.com/badge.svg"' in rendered
    assert assets == {"diffbutler.png", "docs/diagrams/flow.svg"}


def test_headings_get_unique_anchors_and_code_stays_literal() -> None:
    rendered = render_readme(
        "## Install\n\n## Install\n\n"
        '```sh\nexport GITHUB_TOKEN="$(gh auth token)"\n```\n\n'
        "<script>alert('not executable')</script>"
    )
    assert '<h2 id="install">Install</h2>' in rendered
    assert '<h2 id="install-1">Install</h2>' in rendered
    assert "$(gh auth token)" in rendered
    assert "<script>" not in rendered


def test_build_publishes_readme_and_only_public_assets(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "site"
    build_site(root, output)
    page = (output / "index.html").read_text()
    assert render_readme((root / "README.md").read_text()) in page
    assert '<h1 id="diffbutler">DiffButler</h1>' in page
    assert 'src="diffbutler.png"' in page
    assert 'href="style.css"' in page
    assert '<link rel="icon" href="icon.svg" type="image/svg+xml">' in page
    assert '<a class="brand" href="#diffbutler" aria-label="DiffButler home">' in page
    assert '<img src="icon.svg" width="44" height="44" alt="">' in page
    assert '<link rel="canonical" href="https://diffbutler.com/">' in page
    assert "<!-- README -->" not in page
    diagrams = sorted(path.name for path in (root / "docs" / "diagrams").glob("*.svg"))
    assert diagrams
    for name in diagrams:
        assert f'src="docs/diagrams/{name}"' in page
    assert {path.name for path in output.iterdir()} == {
        "index.html",
        "style.css",
        "diffbutler.png",
        "icon.svg",
        "docs",
    }
    assert sorted(path.name for path in (output / "docs" / "diagrams").iterdir()) == diagrams
    for name in diagrams:
        published = (output / "docs" / "diagrams" / name).read_bytes()
        assert published == (root / "docs" / "diagrams" / name).read_bytes()
    assert (output / "diffbutler.png").read_bytes() == (root / "diffbutler.png").read_bytes()
    assert (output / "icon.svg").read_bytes() == (root / "website" / "icon.svg").read_bytes()

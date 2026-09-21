"""Configuration precedence, diagnostics, and source-only defaults."""

from pathlib import Path

import pytest

from diffbutler.config.settings import ConfigError, config_path, load_settings


def test_defaults_do_not_need_a_config_or_forge(tmp_path: Path) -> None:
    settings = load_settings()
    assert settings.forges == settings.reviewers == []
    assert settings.review.mode == "source_only"
    assert settings.db.url == f"sqlite+aiosqlite:///{tmp_path}/data/diffbutler/diffbutler.db"
    assert not (tmp_path / "data").exists()


def test_precedence_and_partial_nested_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    path.write_text("""
[general]
poll_interval_s = 240
max_concurrent_reviews = 3
[review]
checkout = "worktree"
min_confidence = 0.7
[repos."acme/api"]
checkout = "mediated"
path = "sources"
""")
    assert load_settings(path).review.checkout == "worktree"
    assert load_settings(path, repo="acme/api").review.checkout == "mediated"
    monkeypatch.setenv("DIFFBUTLER_REVIEW__CHECKOUT", "worktree")
    monkeypatch.setenv("DIFFBUTLER_GENERAL__POLL_INTERVAL_S", "300")
    settings = load_settings(path, repo="acme/api")
    assert settings.review.checkout == "worktree"
    assert settings.general.poll_interval_s == 300
    assert settings.general.max_concurrent_reviews == 3
    assert settings.review.min_confidence == 0.7
    assert settings.repos["acme/api"].path == tmp_path / "sources"
    assert (
        load_settings(
            path, repo="acme/api", overrides={"review": {"checkout": "mediated"}}
        ).review.checkout
        == "mediated"
    )


def test_xdg_config_is_loaded() -> None:
    path = config_path()
    path.parent.mkdir(parents=True)
    path.write_text("[general]\npoll_interval_s = 60\n")
    assert load_settings().general.poll_interval_s == 60


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("[general]\npoll_interval_s = 0", "general.poll_interval_s"),
        ("[general]\nauto_review = true", "general.auto_review"),
        ('[review]\nmode = "execute"', "source_only"),
        ("[review]\nmin_confidence = 1.1", "review.min_confidence"),
        ('[review]\ndefault_reviewer = "missing"', "configured reviewers.id"),
        ('[[reviewers]]\nid="a"\ncommand=["a"]\n[[reviewers]]\nid="a"\ncommand=["b"]', "unique"),
        ('[db]\nurl = "SENSITIVE"\npassword = "SENSITIVE"', "db.password"),
        ('[db\nsecret = "SENSITIVE"', "invalid TOML"),
    ],
)
def test_bad_configuration_is_actionable_without_values(
    tmp_path: Path, document: str, message: str
) -> None:
    path = tmp_path / "bad.toml"
    path.write_text(document)
    with pytest.raises(ConfigError, match=message) as error:
        load_settings(path)
    assert "SENSITIVE" not in str(error.value)


def test_missing_explicit_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        load_settings(tmp_path / "absent.toml")


def test_invalid_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIFFBUTLER_REVIEWERS", "secret-invalid-json")
    with pytest.raises(ConfigError, match="environment") as error:
        load_settings()
    assert "secret-invalid-json" not in str(error.value)


def test_documented_config_example_parses(tmp_path: Path) -> None:
    guide = (Path(__file__).parents[1] / "docs" / "configuration.md").read_text()
    example = guide.split("```toml\n")[1].split("```")[0]
    path = tmp_path / "example.toml"
    path.write_text(example)
    settings = load_settings(path, repo="acme/api")
    assert settings.review.checkout == "mediated"
    assert settings.repos["acme/api"].enabled is False


def test_dashboard_columns_and_single_key_binding(tmp_path: Path) -> None:
    path = tmp_path / "dashboard.toml"
    path.write_text(
        '[ui]\ncolumns=["title", "ci", "files"]\nsort_by="attention"\n[keys]\nresume_session="v"\n'
    )
    settings = load_settings(path)
    assert settings.keys["resume_session"] == ["v"]
    path.write_text('[ui]\ncolumns=["unknown"]\n')
    with pytest.raises(ConfigError, match="valid columns"):
        load_settings(path)

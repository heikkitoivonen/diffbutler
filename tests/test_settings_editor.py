"""The settings editor changes only edited values and keeps concurrent file updates."""

from importlib.resources import files
from pathlib import Path

import pytest

from diffbutler.config.editor import (
    SECTION,
    SECTIONS,
    ConfigEditor,
    Entry,
    format_value,
    overridden,
    parse_value,
)
from diffbutler.config.settings import ConfigError, Settings, load_settings, validate_document
from diffbutler.config.transport import save_git_transport

DEFAULTS = files("diffbutler.config").joinpath("default.toml").read_text(encoding="utf-8")
FORGE = {
    "kind": "github",
    "base_url": "https://api.github.com",
    "token_env": "GITHUB_TOKEN",
    "include_team_requests": None,
    "login": None,
    "repositories": ["acme/*"],
}


def write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(content)
    return path


def test_sections_cover_every_setting_with_help() -> None:
    assert {section.name for section in SECTIONS} == set(Settings.model_fields)
    for section in SECTIONS:
        for spec in section.fields:
            assert spec.help, f"{section.name}.{spec.label} needs help text"
    labels = {spec.label for spec in SECTION["review"].fields}
    assert "max_diff_bytes" not in labels  # Legacy settings stay hidden and untouched.
    assert {"attention.blocking", "columns", "sort_by"} <= {
        spec.label for spec in SECTION["ui"].fields
    }


def test_unchanged_save_leaves_the_file_alone(tmp_path: Path) -> None:
    path = write(tmp_path, DEFAULTS)
    editor = ConfigEditor.open(path)
    assert not editor.save(editor.draft())
    assert path.read_text() == DEFAULTS


def test_scalar_edits_preserve_comments_and_unset_values(tmp_path: Path) -> None:
    path = write(tmp_path, DEFAULTS)
    editor = ConfigEditor.open(path)
    draft = editor.draft()
    draft.tables["general"]["max_concurrent_reviews"] = 4
    draft.tables["general"]["poll_interval_s"] = 120
    draft.tables["db"]["auto_migrate"] = None
    draft.tables["ui"]["attention.blocking"] = 90
    assert editor.save(draft)
    content = path.read_text()
    assert "max_concurrent_reviews = 4" in content
    assert "auto_migrate" not in content
    assert "blocking = 90" in content
    assert "# Omit workspace_root to follow $XDG_DATA_HOME/diffbutler/reviews" in content
    settings = load_settings(path)
    assert settings.general.poll_interval_s == 120
    assert settings.db.auto_migrate is True
    assert settings.ui.attention.blocking == 90
    assert ConfigEditor.open(path).base == editor.base


def test_adding_to_default_empty_arrays_writes_tables_after_examples(tmp_path: Path) -> None:
    path = write(tmp_path, DEFAULTS)
    editor = ConfigEditor.open(path)
    draft = editor.draft()
    draft.entries["forges"].append(Entry(dict(FORGE)))
    reviewer = dict.fromkeys((spec.label for spec in SECTION["reviewers"].fields), None)
    draft.entries["reviewers"].append(
        Entry({**reviewer, "id": "codex", "command": ["codex"], "preset": "codex"})
    )
    draft.tables["review"]["default_reviewer"] = "codex"
    assert editor.save(draft)
    content = path.read_text()
    assert content.index("# Remove the top-level forges") < content.index("[[forges]]")
    assert not {"forges = []", "reviewers = []"} & set(content.splitlines())
    settings = load_settings(path)
    assert [forge.repositories for forge in settings.forges] == [["acme/*"]]
    assert settings.forges[0].include_team_requests is True
    assert [reviewer.id for reviewer in settings.reviewers] == ["codex"]


def test_list_entries_are_patched_and_removed_in_place(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "[[reviewers]]\nid = 'a'\ncommand = ['a']\n# keep b\n"
        "[[reviewers]]\nid = 'b'\ncommand = ['b']\ntimeout_s = 5\n"
        "[[reviewers]]\nid = 'c'\ncommand = ['c']\n",
    )
    editor = ConfigEditor.open(path)
    draft = editor.draft()
    del draft.entries["reviewers"][0]
    draft.entries["reviewers"][0].values["timeout_s"] = 60
    assert editor.save(draft)
    settings = load_settings(path)
    assert [(reviewer.id, reviewer.timeout_s) for reviewer in settings.reviewers] == [
        ("b", 60),
        ("c", 900),
    ]
    draft = editor.draft()
    draft.entries["reviewers"].clear()
    assert editor.save(draft)
    assert "reviewers" not in path.read_text()


def test_map_entries_can_be_renamed_added_removed_and_edited(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        '[repos."acme/api"]\n# local checkout\npath = "~/src/api"\n'
        '[repos."acme/old"]\nenabled = false\n'
        '[keys]\nopen_in_browser = "o"\n',
    )
    editor = ConfigEditor.open(path)
    draft = editor.draft()
    repos = draft.entries["repos"]
    repos[0].name = "acme/web"
    repos[0].values["prompt"] = "notes.md"
    del repos[1]
    repos.append(Entry({"enabled": None, "git_transport": "ssh"}, name="acme/new"))
    keys = draft.entries["keys"]
    assert keys[0].values == {"keys": "o"}
    keys[0].values["keys"] = ["o", "ctrl+o"]
    keys.append(Entry({"keys": ["comma"]}, name="settings"))
    assert editor.save(draft)
    content = path.read_text()
    assert "# local checkout" in content
    settings = load_settings(path)
    assert set(settings.repos) == {"acme/web", "acme/new"}
    assert settings.repos["acme/web"].prompt == tmp_path / "notes.md"
    assert str(settings.repos["acme/web"].path).endswith("src/api")
    assert settings.repos["acme/new"].git_transport == "ssh"
    assert settings.keys == {"open_in_browser": ["o", "ctrl+o"], "settings": ["comma"]}


@pytest.mark.parametrize(
    ("names", "message"),
    [(["", "b"], "enter a name"), (["b", "b"], "names must be unique")],
)
def test_map_names_must_be_present_and_unique(
    tmp_path: Path, names: list[str], message: str
) -> None:
    path = write(tmp_path, "[repos.a]\n[repos.b]\n")
    editor = ConfigEditor.open(path)
    draft = editor.draft()
    for entry, name in zip(draft.entries["repos"], names, strict=True):
        entry.name = name
    with pytest.raises(ConfigError, match=message):
        editor.save(draft)
    assert path.read_text() == "[repos.a]\n[repos.b]\n"


def test_background_transport_update_survives_unrelated_edits(tmp_path: Path) -> None:
    path = write(tmp_path, '[repos."acme/api"]\npath = "/src/api"\n[repos."acme/web"]\n')
    editor = ConfigEditor.open(path)
    save_git_transport(path, "acme/api", "ssh", expected="auto")
    draft = editor.draft()
    draft.tables["general"]["max_concurrent_reviews"] = 3
    draft.entries["repos"][0].values["enabled"] = False  # Same repository, other value.
    draft.entries["repos"][1].values["enabled"] = False
    assert editor.save(draft)
    settings = load_settings(path)
    assert settings.repos["acme/api"].git_transport == "ssh"
    assert settings.repos["acme/api"].enabled is False
    assert settings.repos["acme/web"].enabled is False
    assert settings.general.max_concurrent_reviews == 3


@pytest.mark.parametrize(
    "edit",
    ["repo", "rename", "remove", "scalar", "forge"],
)
def test_edits_to_values_changed_on_disk_are_conflicts(tmp_path: Path, edit: str) -> None:
    path = write(
        tmp_path,
        '[general]\nmax_concurrent_reviews = 2\n[repos."acme/api"]\npath = "/a"\n'
        '[[forges]]\nkind = "github"\nbase_url = "https://api.github.com"\nlogin = "me"\n',
    )
    editor = ConfigEditor.open(path)
    changed = path.read_text().replace("= 2", "= 5").replace('"me"', '"you"').replace("/a", "/b")
    path.write_text(changed)
    save_git_transport(path, "acme/api", "ssh", expected="auto")
    changed = path.read_text()
    draft = editor.draft()
    match edit:
        case "repo":
            draft.entries["repos"][0].values["path"] = "/c"
        case "rename":
            draft.entries["repos"][0].name = "acme/web"
        case "remove":
            draft.entries["repos"].clear()
        case "scalar":
            draft.tables["general"]["max_concurrent_reviews"] = 3
        case _:
            draft.entries["forges"][0].values["include_team_requests"] = False
    with pytest.raises(ConfigError, match="changed in the config file while Settings was open"):
        editor.save(draft)
    assert path.read_text() == changed


def test_new_map_entry_conflicts_with_one_added_on_disk(tmp_path: Path) -> None:
    path = write(tmp_path, "")
    editor = ConfigEditor.open(path)
    save_git_transport(path, "acme/api", "ssh", expected="auto")
    draft = editor.draft()
    draft.entries["repos"].append(Entry({"enabled": False}, name="acme/api"))
    with pytest.raises(ConfigError, match="Repository acme/api changed"):
        editor.save(draft)


def test_invalid_edits_are_rejected_before_writing(tmp_path: Path) -> None:
    path = write(tmp_path, DEFAULTS)
    editor = ConfigEditor.open(path)
    draft = editor.draft()
    draft.tables["review"]["default_reviewer"] = "missing"
    draft.tables["ui"]["columns"] = ["number", "bogus"]
    with pytest.raises(ConfigError) as error:
        editor.save(draft)
    assert "Unknown column" in str(error.value) and "bogus" not in str(error.value)
    assert path.read_text() == DEFAULTS


def test_missing_file_is_created_with_only_edited_values(tmp_path: Path) -> None:
    path = tmp_path / "new" / "config.toml"
    editor = ConfigEditor.open(path)
    draft = editor.draft()
    draft.tables["ui"]["include_own_prs"] = True
    assert editor.save(draft)
    assert path.read_text() == "[ui]\ninclude_own_prs = true\n"


@pytest.mark.parametrize(
    ("content", "message"),
    [("[general", "not valid TOML"), ("[general]\nmax_concurrent_reviews = 0\n", "invalid")],
)
def test_open_rejects_unreadable_configuration(tmp_path: Path, content: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        ConfigEditor.open(write(tmp_path, content))


def test_file_validation_ignores_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIFFBUTLER_GENERAL__MAX_CONCURRENT_REVIEWS", "0")
    validate_document({"general": {"max_concurrent_reviews": 1}})
    monkeypatch.delenv("DIFFBUTLER_GENERAL__MAX_CONCURRENT_REVIEWS")
    monkeypatch.setenv("DIFFBUTLER_GENERAL__MAX_CONCURRENT_REVIEWS", "7")
    with pytest.raises(ConfigError, match=r"general\.max_concurrent_reviews"):
        validate_document({"general": {"max_concurrent_reviews": 0}})


def test_environment_overrides_are_detected_for_fields_and_parents() -> None:
    environ = {"DIFFBUTLER_GENERAL__POLL_INTERVAL_S": "1", "diffbutler_forges": "[]"}
    assert overridden("general.poll_interval_s", environ)
    assert not overridden("general.max_concurrent_reviews", environ)
    assert overridden("forges", environ)
    assert overridden("ui.attention.blocking", {"DIFFBUTLER_UI__ATTENTION": "{}"})


@pytest.mark.parametrize(
    ("section", "label", "text", "value"),
    [
        ("forges", "repositories", "[]", []),
        ("general", "poll_interval_s", " 90 ", 90),
        ("review", "min_confidence", "0.25", 0.25),
        ("ui", "browser_command", "'my browser' --new-tab", ["my browser", "--new-tab"]),
        ("review", "prompt", "~/notes.md", "~/notes.md"),
        ("general", "snapshot_root", "  ", None),
    ],
)
def test_text_values_round_trip(section: str, label: str, text: str, value: object) -> None:
    spec = next(spec for spec in SECTION[section].fields if spec.label == label)
    assert parse_value(spec, text) == value
    assert parse_value(spec, format_value(spec, parse_value(spec, text))) == value


@pytest.mark.parametrize(
    ("label", "text", "message"),
    [("poll_interval_s", "soon", "whole number"), ("snapshot_root", "", None)],
)
def test_text_value_errors_name_the_setting(label: str, text: str, message: str | None) -> None:
    spec = next(spec for spec in SECTION["general"].fields if spec.label == label)
    if message is None:
        assert parse_value(spec, text) is None
    else:
        with pytest.raises(ConfigError, match=f"{label}: enter a {message}"):
            parse_value(spec, text)
    words = next(spec for spec in SECTION["ui"].fields if spec.label == "columns")
    with pytest.raises(ConfigError, match="matching quotes"):
        parse_value(words, "'unterminated")


def test_entries_added_to_inline_collections_stay_valid_toml(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        'forges = [{kind = "github", base_url = "https://x", login = "a"}]\n'
        'repos = {"acme/api" = {enabled = false}}\n'
        "ui = {include_own_prs = true}\n",
    )
    editor = ConfigEditor.open(path)
    draft = editor.draft()
    draft.entries["forges"].append(Entry({"kind": "github", "base_url": "https://y", "login": "b"}))
    draft.entries["repos"].append(Entry({"path": "/src/web"}, name="acme/web"))
    draft.tables["ui"]["attention.blocking"] = 7
    assert editor.save(draft)
    settings = load_settings(path)  # Loading parses the written text again.
    assert [forge.login for forge in settings.forges] == ["a", "b"]
    assert set(settings.repos) == {"acme/api", "acme/web"}
    assert settings.ui.attention.blocking == 7 and settings.ui.include_own_prs


def test_empty_list_is_saved_instead_of_restoring_the_default(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        '[[forges]]\nkind = "github"\nbase_url = "https://x"\nlogin = "a"\n'
        'repositories = ["acme/api"]\n',
    )
    editor = ConfigEditor.open(path)
    draft = editor.draft()
    spec = next(spec for spec in SECTION["forges"].fields if spec.label == "repositories")
    draft.entries["forges"][0].values["repositories"] = parse_value(spec, "[]")
    assert editor.save(draft)
    assert load_settings(path).forges[0].repositories == []
    saved = ConfigEditor.open(path).base.entries["forges"][0].values["repositories"]
    assert format_value(spec, saved) == "[]"

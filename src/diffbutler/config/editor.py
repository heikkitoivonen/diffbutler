"""Edit configuration values in place, preserving comments and concurrent file changes.

The editor works on what the file says, not on effective settings: an unset value
shows its built-in default and is only written once changed. Saving re-reads the
file under the config lock and applies just the edited values, so background
updates such as a remembered Git transport survive. A value edited both here and
in the file since opening is reported as a conflict instead of being overwritten.
"""

import copy
import os
import shlex
import tomllib
import types
from collections.abc import Mapping, MutableMapping, MutableSequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Union, cast, get_args, get_origin

import tomlkit
from pydantic import BaseModel
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import AoT, InlineTable

from diffbutler.config.file import config_lock, publish_config
from diffbutler.config.settings import (
    UI,
    ConfigError,
    Database,
    Forge,
    General,
    Repository,
    Review,
    Reviewer,
    config_path,
    validate_document,
)
from diffbutler.domain.dashboard import COLUMN_IDS

type Value = bool | int | float | str | list[str] | None
FieldKind = Literal["bool", "choice", "int", "float", "text", "words"]
SectionKind = Literal["table", "list", "map"]

HELP: dict[str, str] = {
    "general.workspace_root": "Review home for session workspaces and conversations. Existing "
    "data is not moved; DiffButler uses the new location from its next operation.",
    "general.repository_root": "Shared bare Git repositories. Existing data is not moved; "
    "DiffButler uses the new location from its next operation.",
    "general.repository_retention_days": "Evict unused shared repositories after this many days.",
    "general.snapshot_root": "Optional private directory for reconstructible source snapshots, "
    "separate from the other managed roots.",
    "general.poll_interval_s": "Seconds between forge refreshes. Empty uses 60 with "
    "authenticated accounts and 600 with any public account; low quota slows polling further.",
    "general.max_concurrent_reviews": "Background AI reviews allowed to run at once.",
    "db.url": "SQLite database URL. Existing data is not moved; DiffButler uses the new "
    "database from its next operation.",
    "db.auto_migrate": "Back up and migrate an older database schema automatically.",
    "review.default_reviewer": "Reviewer ID used when none is chosen; must name a reviewer.",
    "review.findings_threshold": "Lowest severity shown as a finding.",
    "review.min_confidence": "Lowest reviewer confidence (0 to 1) shown as a finding.",
    "review.mode": "Background reviews only read source.",
    "review.prompt": "Optional file with extra review instructions.",
    "forges.kind": "Forge type; only GitHub is implemented.",
    "forges.base_url": "API URL, for example https://api.github.com.",
    "forges.token_env": "Name of the environment variable holding the token, never the token. "
    "Empty uses public access, which requires a login.",
    "forges.include_team_requests": "Include review requests made to your teams.",
    "forges.login": "Your username on this forge.",
    "forges.repositories": "Monitored repositories as owner/repo or owner/*, space-separated. "
    "Empty searches review requests in all accessible repositories.",
    "reviewers.id": "Unique name used to select this reviewer.",
    "reviewers.kind": "Reviewer type.",
    "reviewers.command": "Executable and arguments, space-separated with shell-style quoting; "
    "no shell runs.",
    "reviewers.preset": "Native Codex or Claude Code integration.",
    "reviewers.timeout_s": "Seconds before a background review is stopped.",
    "reviewers.parse": "Output format of custom reviewers.",
    "reviewers.json_path": "Field holding the review in JSON output.",
    "reviewers.stdin": "Send the prompt on standard input.",
    "ui.include_own_prs": "Show PRs authored by your account in the queue.",
    "ui.include_assigned_prs": "Show PRs assigned to your account in the queue.",
    "ui.browser_command": "Browser command and arguments; the PR URL is appended. "
    "Empty uses the system browser.",
    "ui.columns": "Queue columns, space-separated: " + " ".join(COLUMN_IDS) + ".",
    "ui.sort_by": "Default sort column; a sort chosen in the dashboard takes precedence.",
    "ui.sort_desc": "Sort in descending order by default.",
    "ui.preview_pane": "Show details of the selected PR below the queue.",
    "ui.default_filter": "View shown when the dashboard starts.",
    "ui.attention.blocking": "Attention score added for blocking findings.",
    "ui.attention.important": "Attention score added for important findings.",
    "ui.attention.age_pressure_per_day": "Attention score added per day of PR age.",
    "ui.attention.size_penalty_per_100_lines": "Attention score removed per 100 changed lines.",
    "repos.enabled": "Repository AI opt-in metadata; it never starts a review.",
    "repos.path": "Local checkout used as a source for this repository.",
    "repos.default_reviewer": "Reviewer ID for this repository; overrides the review default.",
    "repos.prompt": "Review instructions file for this repository.",
    "repos.git_transport": "Preferred Git transport; a successful fetch remembers it.",
    "keys.keys": "Keys for this dashboard action, space-separated, for example: o ctrl+o.",
}
CHOICES: dict[str, tuple[str, ...]] = {"ui.sort_by": COLUMN_IDS}


@dataclass(frozen=True)
class FieldSpec:
    label: str  # Dotted path within its table, e.g. "attention.blocking".
    kind: FieldKind
    optional: bool
    required: bool
    default: Value
    choices: tuple[str, ...] = ()
    help: str = ""


@dataclass(frozen=True)
class SectionSpec:
    name: str
    title: str
    kind: SectionKind
    fields: tuple[FieldSpec, ...]
    entry_title: str = ""


@dataclass
class Entry:
    """One forge, reviewer, repository or key binding; origin locates it in the file."""

    values: dict[str, Value]
    origin: int | str | None = None
    name: str = ""


@dataclass
class Draft:
    tables: dict[str, dict[str, Value]] = field(default_factory=dict[str, dict[str, Value]])
    entries: dict[str, list[Entry]] = field(default_factory=dict[str, list[Entry]])


def plain(value: object) -> Value:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, list):
        return [str(item) for item in cast(list[object], value)]
    return cast(Value, value)


def field_specs(section: str, model: type[BaseModel], prefix: str = "") -> list[FieldSpec]:
    specs: list[FieldSpec] = []
    for name, info in model.model_fields.items():
        if info.exclude:
            continue  # Legacy settings that are accepted but ignored.
        label = prefix + name
        annotation: Any = info.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            specs.extend(field_specs(section, annotation, label + "."))
            continue
        optional = False
        if get_origin(annotation) in {Union, types.UnionType}:
            arguments = [item for item in get_args(annotation) if item is not type(None)]
            optional = len(arguments) < len(get_args(annotation))
            (annotation,) = arguments
        choices: tuple[str, ...] = CHOICES.get(f"{section}.{label}", ())
        if get_origin(annotation) is Literal:
            kind: FieldKind = "choice"
            choices = tuple(get_args(annotation))
        elif choices:
            kind = "choice"
        elif annotation is bool:
            kind = "bool"
        elif annotation is int:
            kind = "int"
        elif annotation is float:
            kind = "float"
        elif annotation in {str, Path}:
            kind = "text"
        elif get_origin(annotation) is list and get_args(annotation) == (str,):
            kind = "words"
        else:
            raise TypeError(f"No settings editor for {section}.{label}")
        required = info.is_required()
        specs.append(
            FieldSpec(
                label,
                kind,
                optional,
                required,
                None if required else plain(info.get_default(call_default_factory=True)),
                choices,
                HELP.get(f"{section}.{label}", ""),
            )
        )
    return specs


SECTIONS: tuple[SectionSpec, ...] = (
    SectionSpec("general", "General", "table", tuple(field_specs("general", General))),
    SectionSpec("db", "Database", "table", tuple(field_specs("db", Database))),
    SectionSpec("review", "Review", "table", tuple(field_specs("review", Review))),
    SectionSpec(
        "reviewers", "Reviewers", "list", tuple(field_specs("reviewers", Reviewer)), "Reviewer"
    ),
    SectionSpec("forges", "Forges", "list", tuple(field_specs("forges", Forge)), "Forge"),
    SectionSpec(
        "repos",
        "Repositories",
        "map",
        tuple(field_specs("repos", Repository)),
        "Repository",
    ),
    SectionSpec("ui", "Interface", "table", tuple(field_specs("ui", UI))),
    SectionSpec(
        "keys",
        "Keys",
        "map",
        (FieldSpec("keys", "words", False, True, None, help=HELP["keys.keys"]),),
    ),
)
SECTION = {section.name: section for section in SECTIONS}


EMPTY_LIST = "[]"  # Distinguishes an empty list from an unset one in text input.


def format_value(spec: FieldSpec, value: Value) -> str:
    if value is None:
        return ""
    if spec.kind == "words":
        if value == []:
            return EMPTY_LIST
        return shlex.join([value] if isinstance(value, str) else cast(list[str], value))
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def parse_value(spec: FieldSpec, text: str) -> Value:
    """Convert text input; empty means unset, so the built-in default applies."""
    text = text.strip()
    if not text:
        return None
    if spec.kind == "words" and text == EMPTY_LIST:
        return []
    try:
        match spec.kind:
            case "int":
                return int(text)
            case "float":
                return float(text)
            case "words":
                return shlex.split(text)
            case _:
                return text
    except ValueError:
        expected = {"int": "a whole number", "float": "a number", "words": "matching quotes"}
        raise ConfigError(f"{spec.label}: enter {expected[spec.kind]}") from None


def overridden(label: str, environ: Mapping[str, str] | None = None) -> bool:
    """Whether a DIFFBUTLER_* variable overrides a dotted setting or one of its parents."""
    names = {
        name.upper() for name in (os.environ if environ is None else environ) if name.isascii()
    }
    parts = label.upper().split(".")
    return any(
        "DIFFBUTLER_" + "__".join(parts[:length]) in names for length in range(1, len(parts) + 1)
    )


def lookup(table: object, label: str) -> Value:
    for part in label.split("."):
        if not isinstance(table, Mapping):
            return None
        table = cast(Mapping[str, object], table).get(part)
    return plain(table)


def project(document: Mapping[str, Any]) -> Draft:
    draft = Draft()
    for section in SECTIONS:
        content: object = document.get(section.name)
        if section.kind == "table":
            draft.tables[section.name] = {
                spec.label: lookup(content, spec.label) for spec in section.fields
            }
        elif section.kind == "list":
            draft.entries[section.name] = [
                Entry({spec.label: lookup(entry, spec.label) for spec in section.fields}, index)
                for index, entry in enumerate(cast(list[object], content or []))
            ]
        else:
            draft.entries[section.name] = [
                Entry(
                    {"keys": plain(entry)}
                    if section.name == "keys"
                    else {spec.label: lookup(entry, spec.label) for spec in section.fields},
                    name,
                    name,
                )
                for name, entry in cast(dict[str, object], content or {}).items()
            ]
    return draft


def assign(table: MutableMapping[str, Any], label: str, value: Value) -> None:
    *parents, key = label.split(".")
    for part in parents:
        if part not in table:
            if value is None:
                return
            table[part] = (
                tomlkit.inline_table() if isinstance(table, InlineTable) else tomlkit.table()
            )
        table = cast(MutableMapping[str, Any], table[part])
    if value is None:
        table.pop(key, None)
    else:
        table[key] = value


def new_table(values: Mapping[str, Value], *, inline: bool = False) -> Any:
    table = tomlkit.inline_table() if inline else tomlkit.table()
    for label, value in values.items():
        assign(table, label, value)
    if not inline:
        table.add(tomlkit.nl())
    return table


def conflict(where: str) -> ConfigError:
    return ConfigError(
        f"{where} changed in the config file while Settings was open; "
        "close Settings and open it again to edit the new values"
    )


def entry_label(section: SectionSpec, entry: Entry) -> str:
    if isinstance(entry.origin, int):
        return f"{section.entry_title} {entry.origin + 1}"
    return f"{section.entry_title or 'Key'} {entry.name or entry.origin}"


def apply(document: MutableMapping[str, Any], base: Draft, current: Draft, draft: Draft) -> bool:
    """Apply the values edited between base and draft onto the re-read document."""
    changed = False
    for section in SECTIONS:
        name = section.name
        if section.kind == "table":
            for label, value in draft.tables[name].items():
                old = base.tables[name][label]
                if value == old:
                    continue
                if current.tables[name][label] != old:
                    raise conflict(f"{name}.{label}")
                if name not in document:
                    if value is None:
                        continue
                    document[name] = tomlkit.table()
                assign(cast(MutableMapping[str, Any], document[name]), label, value)
                changed = True
        elif section.kind == "list":
            changed |= apply_list(document, section, base.entries[name], current, draft)
        else:
            changed |= apply_map(document, section, base.entries[name], current, draft)
    return changed


def apply_list(
    document: MutableMapping[str, Any],
    section: SectionSpec,
    base: list[Entry],
    current: Draft,
    draft: Draft,
) -> bool:
    entries = draft.entries[section.name]
    if entries == base:
        return False
    if [entry.values for entry in current.entries[section.name]] != [
        entry.values for entry in base
    ]:
        raise conflict(section.title)
    kept = sorted(entry.origin for entry in entries if isinstance(entry.origin, int))
    array = cast(MutableSequence[object] | None, document.get(section.name))
    if array is None or (not array and not isinstance(array, AoT)):
        # Replace the default empty inline array with tables at the end of the file.
        document.pop(section.name, None)
        document[section.name] = tomlkit.aot()
    array = cast(MutableSequence[object], document[section.name])
    for index in reversed(range(len(array))):
        if index not in kept:
            del array[index]
    for entry in entries:
        if isinstance(entry.origin, int):
            table = cast(MutableMapping[str, Any], array[kept.index(entry.origin)])
            for label, value in entry.values.items():
                if value != base[entry.origin].values[label]:
                    assign(table, label, value)
        else:
            # Keep the file's style; a standard table inside an inline array is invalid.
            array.append(new_table(entry.values, inline=not isinstance(array, AoT)))
    if not array:
        document.pop(section.name)
    return True


def apply_map(
    document: MutableMapping[str, Any],
    section: SectionSpec,
    base: list[Entry],
    current: Draft,
    draft: Draft,
) -> bool:
    entries = draft.entries[section.name]
    if entries == base:
        return False
    names = [entry.name.strip() for entry in entries]
    for entry, name in zip(entries, names, strict=True):
        if not name:
            raise ConfigError(f"{entry_label(section, entry)}: enter a name")
    if len(set(names)) != len(names):
        raise ConfigError(f"{section.title}: names must be unique")
    originals = {cast(str, entry.origin): entry.values for entry in base}
    present = {cast(str, entry.origin): entry.values for entry in current.entries[section.name]}
    renamed = {
        cast(str, entry.origin): name
        for entry, name in zip(entries, names, strict=True)
        if entry.origin is not None and name != entry.origin
    }
    edited = {
        cast(str, entry.origin)
        for entry in entries
        if entry.origin is not None and entry.values != originals[cast(str, entry.origin)]
    }
    removed = set(originals) - {entry.origin for entry in entries}
    title = section.entry_title or "Key"
    # Removing or renaming replaces the whole entry; an edit touches only its changed values.
    for name in removed | set(renamed):
        if present.get(name) != originals[name]:
            raise conflict(f"{title} {name}")
    for entry in entries:
        origin = cast(str, entry.origin)
        if origin in edited and origin not in renamed:
            for label, value in entry.values.items():
                old = originals[origin][label]
                if value != old and (origin not in present or present[origin][label] != old):
                    raise conflict(f"{title} {origin}")
    vacated = removed | set(renamed)
    for entry, name in zip(entries, names, strict=True):
        if name != entry.origin and name in present and name not in vacated:
            raise conflict(f"{title} {name}")
    if section.name not in document:
        document[section.name] = tomlkit.table(is_super_table=True)
    table = cast(MutableMapping[str, Any], document[section.name])
    moved = {origin: table.pop(origin) for origin in renamed}
    for name in removed:
        del table[name]
    for entry, name in zip(entries, names, strict=True):
        if entry.origin is None:
            table[name] = (
                entry.values["keys"]
                if section.name == "keys"
                else new_table(entry.values, inline=isinstance(table, InlineTable))
            )
            continue
        origin = cast(str, entry.origin)
        if origin in moved:
            table[name] = moved[origin]
        if origin not in edited:
            continue
        if section.name == "keys":
            table[name] = entry.values["keys"]
            continue
        item = cast(MutableMapping[str, Any], table[name])
        for label, value in entry.values.items():
            if value != originals[origin][label]:
                assign(item, label, value)
    return True


class ConfigEditor:
    def __init__(self, path: Path, base: Draft) -> None:
        self.path = path
        self.base = base

    @staticmethod
    def read(path: Path) -> tuple[str | None, tomlkit.TOMLDocument]:
        try:
            original = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            original = None
        except OSError, UnicodeError:
            raise ConfigError(f"Could not read configuration file: {path}") from None
        try:
            document = tomlkit.parse(original or "")
        except TOMLKitError:
            raise ConfigError(
                "Configuration file is not valid TOML; fix it with diffbutler config --edit"
            ) from None
        try:
            validate_document(document.unwrap())
        except ConfigError as exc:
            raise ConfigError(
                f"Configuration file is invalid ({exc}); fix it with diffbutler config --edit"
            ) from None
        return original, document

    @classmethod
    def open(cls, path: Path | None) -> ConfigEditor:
        selected = (path or config_path()).expanduser().resolve()
        _, document = cls.read(selected)
        return cls(selected, project(document))

    def draft(self) -> Draft:
        return copy.deepcopy(self.base)

    def save(self, draft: Draft) -> bool:
        """Write the edited values; False when nothing was edited."""
        if draft == self.base:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with config_lock(self.path):
                original, document = self.read(self.path)
                if not apply(document, self.base, project(document), draft):
                    return False
                content = tomlkit.dumps(document)
                try:
                    written = tomllib.loads(content)
                except tomllib.TOMLDecodeError:
                    raise ConfigError("Settings could not be saved as valid TOML") from None
                validate_document(written)
                if not publish_config(self.path, original, content):
                    raise conflict("The configuration file")
                self.base = project(document)
        except OSError, UnicodeError, TOMLKitError:
            raise ConfigError(f"Could not save configuration file: {self.path}") from None
        return True

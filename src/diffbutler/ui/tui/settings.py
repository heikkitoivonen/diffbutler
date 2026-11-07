"""Edit every configuration setting from the running dashboard."""

import asyncio
import logging
from collections.abc import Sequence
from typing import ClassVar, cast

from textual import on
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Input,
    Label,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

from diffbutler.config.editor import (
    SECTION,
    SECTIONS,
    ConfigEditor,
    Draft,
    Entry,
    FieldSpec,
    SectionSpec,
    Value,
    format_value,
    overridden,
    parse_value,
)
from diffbutler.config.settings import ConfigError

# Prefilled values for added entries; GitHub is the only implemented forge.
NEW_ENTRY: dict[str, dict[str, Value]] = {
    "forges": {"kind": "github", "base_url": "https://api.github.com"}
}
OVERRIDE_NOTE = " A DIFFBUTLER_* environment variable overrides this; saving changes only the file."


def placeholder(spec: FieldSpec) -> str:
    if spec.required:
        return "required"
    if spec.default in (None, "", []):
        return "not set"
    return "default: " + format_value(spec, spec.default)


class FieldRow(Vertical):
    """One setting; an untouched control keeps the file's value, including unset."""

    def __init__(self, path: str, spec: FieldSpec, value: Value) -> None:
        super().__init__(classes="field")
        self.path = path
        self.spec = spec
        self.base = value
        effective = value if value is not None else spec.default
        self.control: Checkbox | Select[str] | Input
        # Record what each control starts with; a Select reports no value until mounted.
        self.initial: object
        if spec.kind == "bool":
            self.initial = bool(effective)
            self.control = Checkbox(spec.label, self.initial, compact=True)
        elif spec.kind == "choice":
            self.initial = effective if effective in spec.choices else None
            self.control = Select[str](
                [(choice, choice) for choice in spec.choices],
                allow_blank=spec.optional or spec.required,
                value=Select.NULL if self.initial is None else self.initial,
                prompt="not set",
                compact=True,
            )
        else:
            self.initial = format_value(spec, value)
            self.control = Input(self.initial, placeholder=placeholder(spec), compact=True)

    def compose(self) -> ComposeResult:
        with Horizontal(classes="field-row"):
            if not isinstance(self.control, Checkbox):
                yield Label(self.spec.label + (" *" if self.spec.required else ""))
            yield self.control
        note = OVERRIDE_NOTE if self.path and overridden(self.path) else ""
        yield Static(self.spec.help + note, classes="help", markup=False)

    def current(self) -> object:
        if isinstance(self.control, Input):
            return self.control.value
        value = self.control.value
        return None if value is Select.NULL else value

    def value(self) -> Value:
        current = self.current()
        if current == self.initial:
            return self.base
        if isinstance(self.control, Input):
            return parse_value(self.spec, self.control.value)
        return cast(Value, current)


class TableForm(Vertical):
    def __init__(self, section: SectionSpec, values: dict[str, Value]) -> None:
        super().__init__(classes="form")
        self.section = section
        self.rows = [
            FieldRow(f"{section.name}.{spec.label}", spec, values[spec.label])
            for spec in section.fields
        ]

    def compose(self) -> ComposeResult:
        yield from self.rows

    def values(self) -> dict[str, Value]:
        return {row.spec.label: row.value() for row in self.rows}


class EntryForm(Vertical):
    """A forge, reviewer, repository or key binding; origin locates it in the file."""

    def __init__(self, section: SectionSpec, entry: Entry) -> None:
        super().__init__(classes="entry")
        self.section = section
        self.origin = entry.origin
        self.name_input = (
            Input(
                entry.name,
                placeholder="owner/repo" if section.name == "repos" else "dashboard action",
                compact=True,
            )
            if section.kind == "map"
            else None
        )
        self.title = ""
        # Lists are overridden only as a whole, which the tab notes; named entries can be.
        name = f"{section.name}.{entry.origin}" if isinstance(entry.origin, str) else ""
        self.rows = [
            FieldRow(
                name if section.name == "keys" or not name else f"{name}.{spec.label}",
                spec,
                entry.values.get(spec.label),
            )
            for spec in section.fields
        ]

    def compose(self) -> ComposeResult:
        yield Static("", classes="entry-title", markup=False)
        if self.name_input is not None:
            with Horizontal(classes="field-row"):
                yield Label("repository *" if self.section.name == "repos" else "action *")
                yield self.name_input
        yield from self.rows
        yield Button(f"Remove {self.section.entry_title.lower() or 'binding'}", compact=True)

    def entry(self) -> Entry:
        try:
            values = {row.spec.label: row.value() for row in self.rows}
        except ConfigError as exc:
            raise ConfigError(f"{self.title}: {exc}") from None
        return Entry(values, self.origin, self.name_input.value if self.name_input else "")


class SettingsScreen(ModalScreen[bool]):
    """Returns True after saving changes; the caller applies them from the file."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("ctrl+s", "save", "Save"),
        ("escape", "close", "Close"),
    ]
    CSS = """
    SettingsScreen { align: center middle; }
    #settings { width: 95%; height: 95%; background: $surface; padding: 0 1; }
    #settings-title, #settings-status { height: auto; }
    #settings TabbedContent { height: 1fr; }
    #settings TabPane { padding: 0; }
    .field { height: auto; margin: 0 0 1 0; }
    .field-row { height: auto; }
    .field-row Label { width: 32; }
    .field-row Input, .field-row Select { width: 1fr; }
    .help { color: $text-muted; }
    .entry { height: auto; border: round $primary; padding: 0 1; margin: 0 0 1 0; }
    .entry-title { text-style: bold; }
    .section-note { color: $text-muted; margin: 0 0 1 0; }
    """

    def __init__(self, editor: ConfigEditor, actions: Sequence[str] = ()) -> None:
        super().__init__()
        self.editor = editor
        self.actions = actions
        self.saving = False
        self.discard = False

    def compose(self) -> ComposeResult:
        draft = self.editor.draft()
        with Vertical(id="settings"):
            yield Static(f"Settings · {self.editor.path}", id="settings-title", markup=False)
            with TabbedContent():
                for section in SECTIONS:
                    with (
                        TabPane(section.title, id=f"settings-{section.name}"),
                        VerticalScroll(),
                    ):
                        yield from self.section_widgets(section, draft)
            yield Static(
                "Ctrl+S saves and applies · Escape closes", id="settings-status", markup=False
            )
        yield Footer()

    def section_widgets(self, section: SectionSpec, draft: Draft) -> list[Widget]:
        if section.kind == "table":
            return [TableForm(section, draft.tables[section.name])]
        notes: list[str] = []
        if section.name == "keys" and self.actions:
            notes.append("Actions: " + ", ".join(self.actions) + ".")
        if section.kind == "list" or section.name == "keys":
            notes.append("Fields marked * are required.")
        if overridden(section.name):
            notes.append(OVERRIDE_NOTE.strip())
        title = section.entry_title.lower() or "key binding"
        return [
            Static(" ".join(notes), classes="section-note", markup=False),
            *(EntryForm(section, entry) for entry in draft.entries[section.name]),
            Button(f"Add {title}", name=section.name, classes="add", compact=True),
        ]

    def on_mount(self) -> None:
        self.number_entries()

    def number_entries(self) -> None:
        counts: dict[str, int] = {}
        for form in self.query(EntryForm):
            section = form.section
            counts[section.name] = counts.get(section.name, 0) + 1
            # List positions also appear zero-based in validation errors.
            title = (
                f"{section.entry_title} {counts[section.name]} "
                f"({section.name}.{counts[section.name] - 1})"
                if section.kind == "list"
                else section.entry_title or "Key binding"
            )
            form.title = title
            form.query_one(".entry-title", Static).update(title)

    def status(self, message: str) -> None:
        self.query_one("#settings-status", Static).update(message)

    @on(Button.Pressed)
    async def button(self, event: Button.Pressed) -> None:
        # The dashboard's own Button handler must not see these presses.
        event.stop()
        self.discard = False
        if event.button.has_class("add"):
            section = SECTION[event.button.name or ""]
            form = EntryForm(section, Entry(dict(NEW_ENTRY.get(section.name, {}))))
            scroll = self.query_one(f"#settings-{section.name} VerticalScroll", VerticalScroll)
            await scroll.mount(form, before=event.button)
            self.number_entries()
            form.scroll_visible()
            form.query(Input).first().focus()
            return
        form = next((node for node in event.button.ancestors if isinstance(node, EntryForm)), None)
        if form is not None:
            await form.remove()
            self.number_entries()

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.focus_next()

    @on(Input.Changed)
    @on(Checkbox.Changed)
    @on(Select.Changed)
    def changed(self, event: Input.Changed | Checkbox.Changed | Select.Changed) -> None:
        event.stop()
        self.discard = False

    def collect(self) -> Draft:
        draft = Draft()
        for form in self.query(TableForm):
            try:
                draft.tables[form.section.name] = form.values()
            except ConfigError as exc:
                raise ConfigError(f"{form.section.name}.{exc}") from None
        for section in SECTIONS:
            if section.kind != "table":
                draft.entries[section.name] = [
                    form.entry() for form in self.query(EntryForm) if form.section is section
                ]
        return draft

    def action_save(self) -> None:
        if self.saving:
            return
        try:
            draft = self.collect()
        except ConfigError as exc:
            self.status(f"Not saved: {exc}")
            return
        self.saving = True
        # Edits made after collecting the draft would otherwise be lost on dismissal.
        self.query_one(TabbedContent).disabled = True
        self.status("Saving…")
        self.run_worker(self.save(draft), exclusive=True, exit_on_error=False)

    async def save(self, draft: Draft) -> None:
        try:
            saved = await asyncio.to_thread(self.editor.save, draft)
        except ConfigError as exc:
            self.status(f"Not saved: {exc}")
            return
        except Exception as exc:
            logging.getLogger(__name__).exception("Saving settings failed")
            self.status(f"Not saved ({type(exc).__name__}); see diagnostic log for details.")
            return
        finally:
            self.saving = False
            self.query_one(TabbedContent).disabled = False
        self.dismiss(saved)

    def action_close(self) -> None:
        if self.saving:
            return
        try:
            dirty = self.collect() != self.editor.base
        except ConfigError:
            dirty = True
        if dirty and not self.discard:
            self.discard = True
            self.status("Unsaved changes: Ctrl+S saves them; Escape again discards them.")
            return
        self.dismiss(False)

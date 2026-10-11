"""Settings dialog (Tools > Add-ons > IPA Filler > Config).

Edits only the keys a new user needs (note type, word / IPA field, accent,
Claude on/off + API key); everything else stays in the JSON config, reachable
through "Advanced (JSON)..." (Anki's own `aqt.addons.ConfigEditor`). Saving
merges the values over the current config (`core.settings.merge_settings`),
so unknown keys survive. The decisions (preselection, validation, merge)
live in `core.settings`; this module is Qt glue.

Without an open collection (Add-ons opened from the profile manager) the
config action returns False, and Anki opens its JSON editor instead.
"""

from __future__ import annotations

import os
from typing import Any, cast

from aqt import mw
from aqt.addons import ConfigEditor
from aqt.qt import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    Qt,
    QVBoxLayout,
    QWidget,
)
from aqt.utils import disable_help_button, tooltip

from . import ui
from .core.sources.claude import ENV_KEY
from .core.settings import (
    Settings,
    guess_note_type,
    merge_settings,
    pick_fields,
    settings_from_config,
    validate,
)
from .ops import note_types_with_fields

TITLE = "IPA Filler Settings"
ACCENT_LABELS = {
    "uk": "British (UK) first, American (US) as fallback",
    "us": "American (US) first, British (UK) as fallback",
}
KEY_NOTE = (
    "Only headwords the free dictionaries miss (or only partly find) are sent "
    "to Anthropic, billed to your own key. The key is stored in plain text in "
    "the add-on's config (meta.json in the add-on folder). Leave it empty to "
    f"use the {ENV_KEY} environment variable instead."
)

_dialog: SettingsDialog | None = None


def register(addon: str) -> None:
    assert mw is not None
    mw.addonManager.setConfigAction(addon, _config_action)


def _config_action() -> bool | None:
    """Tools > Add-ons > Config. False = let Anki open its JSON editor."""
    assert mw is not None
    if mw.col is None:
        return False
    open_settings(QApplication.activeWindow() or mw)
    return None


def open_settings(parent: QWidget | None = None) -> SettingsDialog | None:
    """Show the dialog (non-modal; raises an already open one)."""
    global _dialog
    assert mw is not None
    if mw.col is None:
        tooltip("Open a profile first to change the IPA Filler settings.")
        return None
    if _dialog is not None:
        _dialog.raise_()
        _dialog.activateWindow()
        return _dialog
    _dialog = SettingsDialog(parent or mw)
    _dialog.show()
    return _dialog


class SettingsDialog(QDialog):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        assert mw is not None and mw.col is not None
        self.setWindowTitle(TITLE)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        disable_help_button(self)
        # `ConfigEditor(dlg, ...)` reads `dlg.mgr` (it expects the Add-ons
        # dialog); we are its parent, so provide the same attribute.
        self.mgr = mw.addonManager
        self.note_types = note_types_with_fields(mw.col)
        self.configured = Settings()
        self._build()
        self.load(ui.get_config())
        self.finished.connect(self._on_finished)

    # -- layout ---------------------------------------------------------------
    def _build(self) -> None:
        top = QVBoxLayout(self)

        intro = QLabel(
            "Fills an empty IPA field from free online dictionaries (Wiktionary "
            "data), in British RP style by default. Choose which notes to fill:"
        )
        intro.setWordWrap(True)
        top.addWidget(intro)
        self.notice = QLabel()  # e.g. configured note type no longer exists
        self.notice.setWordWrap(True)
        self.notice.setVisible(False)
        top.addWidget(self.notice)

        notes = QGroupBox("Notes")
        form = QFormLayout(notes)
        self.note_type = QComboBox()
        self.word_field = QComboBox()
        self.ipa_field = QComboBox()
        form.addRow("Note type:", self.note_type)
        form.addRow("Word field (looked up):", self.word_field)
        form.addRow("IPA field (filled):", self.ipa_field)
        top.addWidget(notes)

        pron = QGroupBox("Pronunciation")
        pform = QFormLayout(pron)
        self.accent = QComboBox()
        for code, label in ACCENT_LABELS.items():
            self.accent.addItem(label, code)
        pform.addRow("Accent:", self.accent)
        top.addWidget(pron)

        claude = QGroupBox("Claude fallback (optional)")
        cform = QFormLayout(claude)
        self.claude_enabled = QCheckBox("Ask Claude for headwords the dictionaries miss")
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText(f"empty: use {ENV_KEY}")
        note = QLabel(KEY_NOTE)
        note.setWordWrap(True)
        self.key_status = QLabel()
        self.key_status.setWordWrap(True)
        cform.addRow(self.claude_enabled)
        cform.addRow("Anthropic API key:", self.api_key)
        cform.addRow(note)
        cform.addRow(self.key_status)
        top.addWidget(claude)

        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #c0392b;")
        top.addWidget(self.error)

        row = QHBoxLayout()
        self.advanced = QPushButton("Advanced (JSON)...")
        self.advanced.setToolTip("All options, in Anki's add-on config editor")
        row.addWidget(self.advanced)
        row.addStretch(1)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        row.addWidget(self.buttons)
        top.addLayout(row)

        self.note_type.currentIndexChanged.connect(lambda _i: self._on_note_type_changed())
        self.word_field.currentIndexChanged.connect(lambda _i: self._update_state())
        self.ipa_field.currentIndexChanged.connect(lambda _i: self._update_state())
        self.claude_enabled.toggled.connect(lambda _b: self._update_state())
        self.api_key.textChanged.connect(lambda _t: self._update_state())
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.advanced.clicked.connect(self._on_advanced)
        self.setMinimumWidth(520)

    # -- values ---------------------------------------------------------------
    def load(self, config: dict[str, Any]) -> None:
        """Fill the widgets from `config` (also after the JSON editor saved)."""
        s = self.configured = settings_from_config(config)
        name = s.note_type if s.note_type in self.note_types else ""
        name = name or guess_note_type(self.note_types)
        self.note_type.blockSignals(True)
        self.note_type.clear()
        if not name:
            self.note_type.addItem("(choose a note type)", "")
        missing = bool(s.note_type) and s.note_type not in self.note_types
        self.notice.setText(
            f'The configured note type "{s.note_type}" was not found in this collection.'
            if missing
            else ""
        )
        self.notice.setVisible(missing)
        for nt in self.note_types:
            self.note_type.addItem(nt, nt)
        self.note_type.setCurrentIndex(max(0, self.note_type.findData(name)))
        self.note_type.blockSignals(False)
        self._on_note_type_changed()
        self.accent.setCurrentIndex(max(0, self.accent.findData(s.accent_first)))
        self.claude_enabled.setChecked(s.claude_enabled)
        self.api_key.setText(s.claude_api_key)
        self._update_state()

    def values(self) -> Settings:
        return Settings(
            note_type=str(self.note_type.currentData() or ""),
            word_field=str(self.word_field.currentData() or ""),
            ipa_field=str(self.ipa_field.currentData() or ""),
            accent_first=str(self.accent.currentData() or "uk"),
            claude_enabled=self.claude_enabled.isChecked(),
            claude_api_key=self.api_key.text().strip(),
        )

    def _on_note_type_changed(self) -> None:
        name = str(self.note_type.currentData() or "")
        fields = self.note_types.get(name, [])
        word, ipa = pick_fields(fields, self.configured, name)
        for combo, chosen in ((self.word_field, word), (self.ipa_field, ipa)):
            combo.blockSignals(True)
            combo.clear()
            if not chosen:
                combo.addItem("(choose a field)", "")
            for f in fields:
                combo.addItem(f, f)
            combo.setCurrentIndex(max(0, combo.findData(chosen)))
            combo.blockSignals(False)
        self._update_state()

    def _update_state(self) -> None:
        s = self.values()
        problem = validate(s, self.note_types.get(s.note_type))
        self.error.setText(problem or "")
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setEnabled(problem is None)
        self.api_key.setEnabled(s.claude_enabled)
        if not s.claude_enabled:
            self.key_status.setText("")
        elif s.claude_api_key:
            self.key_status.setText("Claude will use the key above.")
        elif os.environ.get(ENV_KEY, "").strip():
            self.key_status.setText(f"Claude will use {ENV_KEY} from the environment.")
        else:
            self.key_status.setText(
                f"No key: enter one above or start Anki with {ENV_KEY} set, "
                "otherwise Claude stays off."
            )

    # -- actions ----------------------------------------------------------------
    def accept(self) -> None:
        s = self.values()
        if validate(s, self.note_types.get(s.note_type)) is not None:
            return
        assert mw is not None
        merged = merge_settings(ui.get_config(), s)  # fresh: JSON edits survive
        mw.addonManager.writeConfig(ui.addon_name(), merged)
        tooltip("IPA Filler settings saved.", parent=self.parentWidget())
        super().accept()

    def _on_advanced(self) -> None:
        """Anki's JSON config editor, as a child of this dialog; when it
        saves, the widgets here are reloaded so OK can't undo those edits."""
        editor = ConfigEditor(cast(Any, self), ui.addon_name(), ui.get_config())
        editor.accepted.connect(lambda: self.load(ui.get_config()))

    def _on_finished(self, _result: int) -> None:
        global _dialog
        if _dialog is self:
            _dialog = None

"""In-window progress bar for the batch lookup.

Anki's `mw.progress` opens a separate modal dialog. Tiling Wayland compositors
(e.g. Hyprland's scrolling layout) float such a dialog on the workspace, so it
stays put when you scroll or switch to another window instead of following
Anki. This bar is a plain child widget at the bottom of the Browser window, so
it moves with the window and leaves Anki usable during a long run.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from aqt.qt import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    Qt,
)

if TYPE_CHECKING:  # aqt.qt re-exports sip, but mypy doesn't see it as exported
    from PyQt6 import sip
else:
    from aqt.qt import sip


class InlineProgress(QFrame):
    def __init__(self, window: QMainWindow, label: str, on_cancel: Callable[[], None]) -> None:
        central = window.centralWidget()
        super().__init__(central or window)
        self._on_cancel = on_cancel
        self.setFrameShape(QFrame.Shape.StyledPanel)

        self._label = QLabel(label, self)
        self._label.setTextFormat(Qt.TextFormat.PlainText)
        # Long headwords must not widen the Browser window.
        self._label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._bar = QProgressBar(self)
        self._bar.setRange(0, 0)  # busy until the first update
        self._bar.setFormat("%v / %m")
        self._cancel = QPushButton("Cancel", self)
        self._cancel.setAutoDefault(False)
        self._cancel.clicked.connect(self._cancel_clicked)

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 4, 8, 4)
        row.addWidget(self._label, 3)
        row.addWidget(self._bar, 2)
        row.addWidget(self._cancel)

        layout = central.layout() if central is not None else None
        if layout is not None:
            layout.addWidget(self)
        else:
            status = window.statusBar()
            assert status is not None
            status.addWidget(self, 1)
        self.show()

    def alive(self) -> bool:
        return not sip.isdeleted(self)

    def update_progress(self, done: int, total: int, label: str | None) -> None:
        if not self.alive():
            return
        self._bar.setRange(0, max(total, 1))
        self._bar.setValue(done)
        if label is not None:
            self._label.setText(label)

    def _cancel_clicked(self) -> None:
        self._cancel.setEnabled(False)
        self._cancel.setText("Cancelling...")
        self._on_cancel()

    def finish(self) -> None:
        if self.alive():
            self.hide()
            self.deleteLater()

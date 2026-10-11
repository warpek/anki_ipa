"""Browser action "Fill missing IPA": thin Qt glue around core.batch / ops.

The editor button lives in `editor_button.py` (registered from `register`).

Flow (each step's success starts the next):
1. `QueryOp` (collection): `ops.collect_words` -> candidate (nid, word) items.
2. `QueryOp.without_collection().with_progress()`: resolve the distinct
   headwords through `core.resolver` off the main thread; progress label
   and cancel (Esc / close on the progress window) via the main thread.
3. `CollectionOp`: `ops.apply_plan` writes everything as one undo step.
4. Summary dialog (`core.batch.summary_text`).
"""

from __future__ import annotations

import os
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from aqt import gui_hooks, mw
from aqt.browser.browser import Browser
from aqt.operations import CollectionOp, QueryOp
from aqt.qt import QAction, QMenu, QTimer, QWidget
from aqt.utils import showText, showWarning, tooltip

from .core.batch import (
    Fields,
    Item,
    build_plan,
    summarize,
    summary_text,
    unique_words,
)
from .core.batch import build_search as build_search
from .core.clean import strip_markup
from .core.resolver import Result
from .core.resolver import build_resolver as build_resolver  # patched by probes
from .ops import Collected, WriteOutcome, apply_plan, collect_words

ACTION_LABEL = "Fill missing IPA"
CACHE_FILE = "ipa_cache.json"

_addon = ""


def register(addon: str) -> None:
    global _addon
    _addon = addon
    gui_hooks.browser_menus_did_init.append(_on_browser_menus_did_init)
    gui_hooks.browser_will_show_context_menu.append(_on_browser_context_menu)

    from . import editor_button  # imports this module; avoid a cycle at import

    editor_button.register()


def get_config() -> dict[str, Any]:
    assert mw is not None
    return mw.addonManager.getConfig(_addon) or {}


def cache_path() -> str:
    assert mw is not None
    return os.path.join(mw.addonManager.addonsFolder(_addon), "user_files", CACHE_FILE)


def _add_action(browser: Browser, menu: QMenu) -> None:
    action = QAction(ACTION_LABEL, menu)
    action.triggered.connect(lambda: _on_fill(browser))
    menu.addAction(action)


def _on_browser_menus_did_init(browser: Browser) -> None:
    browser.form.menu_Notes.addSeparator()
    _add_action(browser, browser.form.menu_Notes)


def _on_browser_context_menu(browser: Browser, menu: QMenu) -> None:
    menu.addSeparator()
    _add_action(browser, menu)


# -- step 1: collect --------------------------------------------------------
def _on_fill(browser: Browser) -> None:
    assert mw is not None
    config = get_config()  # read on the main thread, passed down
    fcfg = Fields.from_config(config)
    selected = [int(n) for n in browser.selected_notes()]
    QueryOp(
        parent=browser,
        op=lambda col: collect_words(col, fcfg, selected or None),
        success=lambda c: _after_collect(browser, config, fcfg, c, bool(selected)),
    ).failure(lambda e: _on_failure(browser, e)).run_in_background()


def _after_collect(
    browser: Browser, config: dict[str, Any], fcfg: Fields, c: Collected, had_selection: bool
) -> None:
    if not c.items:
        where = "selected notes" if had_selection else f"notes matching {fcfg.search()}"
        why = ", ".join(f"{n} {r}" for r, n in sorted(c.skipped.items()))
        msg = f"{ACTION_LABEL}: nothing to do in the {where}" + (f" ({why})" if why else "")
        tooltip(msg, parent=browser)
        return
    _resolve(browser, config, fcfg, c.items)


# -- step 2: resolve (network, off the main thread) ---------------------------
@dataclass
class _Resolved:
    results: dict[str, Result]
    words_total: int
    cancelled: bool
    disabled: dict[str, str]
    errors: list[str]
    llm_status: str | None


def _resolve(browser: Browser, config: dict[str, Any], fcfg: Fields, items: Sequence[Item]) -> None:
    assert mw is not None
    words = unique_words(items)
    path = cache_path()
    cancel = threading.Event()

    # The worker never touches Qt: it reads `cancel`, which the main thread
    # sets from mw.progress.want_cancel() (polled + on every progress update).
    def poll_cancel() -> None:
        assert mw is not None
        if mw.progress.want_cancel():
            cancel.set()

    timer = QTimer(mw)
    timer.timeout.connect(poll_cancel)
    timer.start(150)

    def show_progress(done: int, total: int, word: str) -> None:
        assert mw is not None
        poll_cancel()
        label = None
        if word:
            shown = strip_markup(word).strip()
            shown = shown if len(shown) <= 60 else shown[:57] + "..."
            label = f"Looking up IPA {done + 1}/{total}: {shown}"
        mw.progress.update(label=label, value=done, max=total)

    def progress(done: int, total: int, word: str) -> None:
        assert mw is not None
        mw.taskman.run_on_main(lambda: show_progress(done, total, word))

    def op(_col: object) -> _Resolved:
        resolver = build_resolver(config, path, cancel=cancel.is_set, progress=progress)
        try:
            results = resolver.resolve_many(words)  # saves the cache itself
        finally:
            resolver.cache.save()  # also on exceptions outside resolve_many
        return _Resolved(
            dict(zip(words, results)),
            len(words),
            len(results) < len(words),
            dict(resolver.disabled),
            list(resolver.unexpected_errors),
            resolver.fallback_status(),
        )

    def done() -> None:
        timer.stop()
        timer.deleteLater()

    def on_success(r: _Resolved) -> None:
        done()
        _write(browser, fcfg, items, r)

    def on_failure(e: Exception) -> None:
        done()
        _on_failure(browser, e)

    # Note: with_progress() makes QueryOp use taskman.with_progress, which
    # ignores without_collection() (runs with uses_collection=True). Harmless
    # here: the progress window is modal and the op never touches `col`.
    (
        QueryOp(parent=browser, op=op, success=on_success)
        .failure(on_failure)
        .without_collection()
        .with_progress(f"Looking up IPA for {len(words)} headword(s)...")
        .run_in_background()
    )
    mw.progress.set_title(f"{ACTION_LABEL} (Esc to cancel)")


# -- step 3: write (one undo step) + step 4: summary ---------------------------
def _write(browser: Browser, fcfg: Fields, items: Sequence[Item], r: _Resolved) -> None:
    plan = build_plan(items, r.results, fcfg)

    def finish(outcome: WriteOutcome | None) -> None:
        written = outcome.written if outcome is not None else []
        s = summarize(
            items,
            r.results,
            plan,
            set(written),
            words_total=r.words_total,
            cancelled=r.cancelled,
            disabled=r.disabled,
            errors=r.errors,
            llm_status=r.llm_status,
        )
        _show_summary(browser, summary_text(s, fcfg))

    if not plan:
        finish(None)
        return
    CollectionOp(parent=browser, op=lambda col: apply_plan(col, plan, fcfg)).success(
        finish
    ).failure(lambda e: _on_failure(browser, e)).run_in_background()


def _show_summary(parent: QWidget, text: str) -> None:
    showText(text, parent=parent, title=ACTION_LABEL, copyBtn=True, plain_text_edit=True,
             minWidth=520, minHeight=420)


def _on_failure(parent: QWidget, e: Exception) -> None:
    showWarning(f"{ACTION_LABEL} failed:\n{type(e).__name__}: {e}", parent=parent)

"""Browser action "Fill missing IPA": thin Qt glue around core.batch / ops.

The Settings dialog lives in `settings_dialog.py` (registered from
`register`).

Flow (each step's success starts the next):
0. `ensure_setup`: note type / fields unset or missing in the collection ->
   offer to open Settings instead (the user then runs the action again).
1. `QueryOp` (collection): `ops.collect_words` -> candidate (nid, word) items.
2. `QueryOp.without_collection()`: resolve the distinct headwords through
   `core.resolver` off the main thread. Progress and Cancel are shown in a bar
   inside the Browser (`progress_bar.InlineProgress`), not in Anki's modal
   progress dialog, which tiling compositors float away from the window.
   Closing the Browser or the profile also cancels.
3. `CollectionOp`: `ops.apply_plan` writes everything as one undo step:
   the IPA fills and the not-found tag on definitive misses (notes already
   tagged are excluded by the search / skipped in a selection, and counted).
4. Summary dialog (`core.batch.summary_text`).

All batches of an add-on session share one `Cache` (found results only)
and one `HttpClient` (`session_resolver`), so they never overwrite each
other's cache entries and share the per-host throttle. Each run gets a new
`Resolver`, so its memo of misses lasts for that run only: a note whose
`ipa::notfound` tag was removed is looked up afresh next time.
"""

from __future__ import annotations

import functools
import os
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aqt import gui_hooks, mw
from aqt.browser.browser import Browser
from aqt.operations import CollectionOp, QueryOp
from aqt.qt import QAction, QMenu, QWidget
from aqt.utils import askUser, showText, showWarning, tooltip

if TYPE_CHECKING:  # aqt.qt re-exports sip, but mypy doesn't see it as exported
    from PyQt6 import sip
else:
    from aqt.qt import sip

from .core.batch import (
    SKIP_NOT_FOUND,
    Fields,
    Item,
    build_plan,
    summarize,
    summary_text,
    unique_words,
)
from .core.cache import Cache
from .core.clean import strip_markup
from .core.http import HttpClient, addon_version, make_user_agent
from .core.resolver import Resolver, Result
from .core.resolver import build_resolver as build_resolver  # patched by probes
from .core.settings import setup_prompt
from .ops import Collected, WriteOutcome, apply_plan, collect_words, setup_problem
from .progress_bar import InlineProgress

ACTION_LABEL = "Fill missing IPA"
ADDON_TITLE = "IPA Filler"
CACHE_FILE = "ipa_cache.json"

_addon = ""
_active: threading.Event | None = None  # cancel flag of the running batch
# Session-wide, created lazily on the main thread (see `session_resolver`).
_cache: Cache | None = None
_http: HttpClient | None = None


def register(addon: str) -> None:
    global _addon
    _addon = addon
    gui_hooks.browser_menus_did_init.append(_on_browser_menus_did_init)
    gui_hooks.browser_will_show_context_menu.append(_on_browser_context_menu)
    gui_hooks.profile_will_close.append(_on_profile_will_close)

    # It imports this module; import it here to avoid a cycle.
    from . import settings_dialog

    settings_dialog.register(addon)


def addon_name() -> str:
    """The add-on's package name (`anki_ipa`, or a number on AnkiWeb)."""
    return _addon


def get_config() -> dict[str, Any]:
    assert mw is not None
    return mw.addonManager.getConfig(_addon) or {}


def cache_path() -> str:
    assert mw is not None
    return os.path.join(mw.addonManager.addonsFolder(_addon), "user_files", CACHE_FILE)


@functools.cache
def user_agent() -> str:
    """User-Agent with the add-on's `human_version` (read once)."""
    return make_user_agent(addon_version(os.path.dirname(os.path.abspath(__file__))))


def shared_cache() -> Cache:
    """The session's lookup cache (`user_files/ipa_cache.json`). Main thread."""
    global _cache
    if _cache is None:
        _cache = Cache(cache_path())
    return _cache


def shared_http() -> HttpClient:
    """The session's HTTP client: one per-host throttle for every lookup."""
    global _http
    if _http is None:
        _http = HttpClient(user_agent=user_agent())
    return _http


def session_resolver(config: dict[str, Any], **kwargs: Any) -> Resolver:
    """A new `build_resolver` on the shared cache and client, one per run (its
    miss memo must not outlive the run). Call it on the main thread (it may
    create them); the resolver itself is used by a worker. Goes through the
    module-global `build_resolver`, which probes patch."""
    return build_resolver(
        config,
        cache_path(),
        cache=shared_cache(),
        http=shared_http(),
        user_agent=user_agent(),
        **kwargs,
    )


def ensure_setup(parent: QWidget, fcfg: Fields) -> bool:
    """True when the configured note type and fields exist in the collection.

    Otherwise ask whether to open the Settings dialog (and open it on Yes);
    the caller stops and the user starts the action again afterwards.
    """
    assert mw is not None and mw.col is not None
    problem = setup_problem(mw.col, fcfg)  # a few backend calls, main thread
    if problem is None:
        return True
    if askUser(setup_prompt(problem), parent=parent, title=ADDON_TITLE):
        from . import settings_dialog

        settings_dialog.open_settings(parent)
    return False


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


def _on_profile_will_close() -> None:
    if _active is not None:
        _active.set()
    if _cache is not None:
        _cache.save()  # never raises; a running batch saves again when it stops


def _end_run() -> None:
    global _active
    _active = None


def _parent(browser: Browser) -> QWidget:
    """The Browser, or the main window once the Browser has been closed."""
    assert mw is not None
    if sip.isdeleted(browser) or not browser.isVisible():
        return mw
    return browser


# -- step 1: collect --------------------------------------------------------
def _on_fill(browser: Browser) -> None:
    global _active
    assert mw is not None
    if _active is not None:
        tooltip(f"{ACTION_LABEL} is already running (see the bar in the Browser).", parent=browser)
        return
    config = get_config()  # read on the main thread, passed down
    fcfg = Fields.from_config(config)
    if not ensure_setup(browser, fcfg):
        return
    cancel = _active = threading.Event()
    selected = [int(n) for n in browser.selected_notes()]

    def on_failure(e: Exception) -> None:
        _end_run()
        _on_failure(_parent(browser), e)

    QueryOp(
        parent=browser,
        op=lambda col: collect_words(col, fcfg, selected or None),
        success=lambda c: _after_collect(browser, config, fcfg, c, bool(selected), cancel),
    ).failure(on_failure).run_in_background()


def _after_collect(
    browser: Browser,
    config: dict[str, Any],
    fcfg: Fields,
    c: Collected,
    had_selection: bool,
    cancel: threading.Event,
) -> None:
    if not c.items:
        _end_run()
        msg = c.nothing_to_do_html(fcfg, ACTION_LABEL, had_selection)
        tooltip(msg, parent=_parent(browser))
        return
    _resolve(browser, config, fcfg, c.items, c.skipped.get(SKIP_NOT_FOUND, 0), cancel)


# -- step 2: resolve (network, off the main thread) ---------------------------
@dataclass
class _Resolved:
    results: dict[str, Result]
    words_total: int
    cancelled: bool
    disabled: dict[str, str]
    errors: list[str]
    llm_status: str | None
    cache_error: str | None = None  # the cache file could not be written


def _resolve(
    browser: Browser,
    config: dict[str, Any],
    fcfg: Fields,
    items: Sequence[Item],
    skipped_not_found: int,
    cancel: threading.Event,
) -> None:
    assert mw is not None
    words = unique_words(items)

    # The worker never touches Qt: it reads `cancel`, which the bar's Cancel
    # button, closing the Browser (the bar is destroyed with it) or closing
    # the profile set on the main thread.
    bar = InlineProgress(browser, f"Looking up IPA for {len(words)} headword(s)...", cancel.set)
    bar.destroyed.connect(lambda *_: cancel.set())

    def show_progress(done: int, total: int, word: str) -> None:
        label = None
        if word:
            shown = strip_markup(word).strip()
            shown = shown if len(shown) <= 60 else shown[:57] + "..."
            label = f"Looking up IPA {done + 1}/{total}: {shown}"
        bar.update_progress(done, total, label)

    def progress(done: int, total: int, word: str) -> None:
        assert mw is not None
        mw.taskman.run_on_main(lambda: show_progress(done, total, word))

    # Built here: it may create the shared cache/client (main thread only).
    resolver = session_resolver(config, cancel=cancel.is_set, progress=progress)

    def op(_col: object) -> _Resolved:
        try:
            results = resolver.resolve_many(words)  # saves the cache itself
        finally:
            resolver.cache.save()  # also on exceptions; never raises OSError
        return _Resolved(
            dict(zip(words, results)),
            len(words),
            len(results) < len(words),
            dict(resolver.disabled),
            list(resolver.unexpected_errors),
            resolver.fallback_status(),
            resolver.cache.last_error,
        )

    def on_success(r: _Resolved) -> None:
        assert mw is not None
        bar.finish()
        if mw.col is None:  # profile closed meanwhile; nothing to write into
            _end_run()
            return
        _write(browser, fcfg, items, r, skipped_not_found)

    def on_failure(e: Exception) -> None:
        bar.finish()
        _end_run()
        _on_failure(_parent(browser), e)

    # No with_progress(): that would open Anki's floating modal dialog.
    (
        QueryOp(parent=browser, op=op, success=on_success)
        .failure(on_failure)
        .without_collection()
        .run_in_background()
    )


# -- step 3: write (one undo step) + step 4: summary ---------------------------
def _write(
    browser: Browser,
    fcfg: Fields,
    items: Sequence[Item],
    r: _Resolved,
    skipped_not_found: int = 0,
) -> None:
    # Fills plus not-found tags for definitive misses: one CollectionOp.
    plan = build_plan(items, r.results, fcfg)
    parent = _parent(browser)

    def on_failure(e: Exception) -> None:
        _end_run()
        _on_failure(parent, e)

    def finish(outcome: WriteOutcome | None) -> None:
        _end_run()
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
            cache_error=r.cache_error,
            skipped_not_found=skipped_not_found,
        )
        _show_summary(parent, summary_text(s, fcfg))

    if not plan:
        finish(None)
        return
    CollectionOp(parent=parent, op=lambda col: apply_plan(col, plan, fcfg)).success(
        finish
    ).failure(on_failure).run_in_background()


def _show_summary(parent: QWidget, text: str) -> None:
    showText(text, parent=parent, title=ACTION_LABEL, copyBtn=True, plain_text_edit=True,
             minWidth=520, minHeight=420)


def _on_failure(parent: QWidget, e: Exception) -> None:
    showWarning(f"{ACTION_LABEL} failed:\n{type(e).__name__}: {e}", parent=parent)

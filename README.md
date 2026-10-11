# IPA Filler (Anki add-on)

Fills empty IPA fields in your Anki notes with dictionary transcriptions:
broad British RP by default (`ˈrekɔːd`, `əʊ`, `r`), with American as the
fallback. It handles single words, phrases and whole sentences. Free online
dictionaries (Wiktionary data) come first; Claude can fill in what they miss
if you enable it with your own API key.

Supports **Anki 25.07 or newer** on Windows, macOS and Linux.

## Install

- **From AnkiWeb:** *Tools > Add-ons > Get Add-ons…* and enter the add-on's
  code (AnkiWeb link and code added after the first upload). Restart Anki.
- **From a GitHub release:** download `anki_ipa.ankiaddon` from the
  [releases page](https://github.com/warpek/anki_ipa/releases), then in Anki
  use *Tools > Add-ons > Install from file…* and pick it. Restart Anki.

Back up your collection before the first large run (*File > Create Backup*).

## Setup

Open *Tools > Add-ons > IPA Filler > Config*. This opens the **Settings**
dialog:

- **Note type**: the note type to fill.
- **Word field**: the headword that is looked up.
- **IPA field**: receives the transcription.
- **Accent**: British first (default) or American first.
- **Claude fallback**: off by default (see below).

The dialog suggests a note type with an IPA-looking field (`IPA`,
`Pronunciation`, …). Until a note type and both fields are set, the add-on
offers to open the dialog instead of running. The settings survive add-on
updates. *Advanced (JSON)…* opens Anki's config editor, whose help pane
documents every option (sources, style rules, tags, cache, rate limits).

## Usage

- **Browser > Notes > Fill missing IPA** (also in the right-click menu)
  fills the selected notes, or, with nothing selected, every note of the
  chosen type whose IPA field is empty. A progress bar with a Cancel button
  stays inside the Browser, and Anki remains usable meanwhile. The whole run
  is **one undo step** (*Edit > Undo "Fill IPA"*). A summary lists the
  sources used, partial results and the headwords that were not found.
- The **IPA** button in the editor toolbar fills the note you are editing. It
  asks before it replaces existing IPA. (The experimental Svelte editor
  supports it in the Browser and Edit windows, not in its Add window.)
- Filled notes are tagged:
  - `ipa::auto`: filled by the add-on;
  - `ipa::partial`: some words could only be written as `[word]`;
  - `ipa::llm`: Claude wrote the IPA.

  Review them with the Browser searches `tag:ipa::partial` and
  `tag:ipa::llm`, then remove the tags (*Notes > Remove Tags*).

### Output style

- Bare IPA without slashes, so put any `/…/` in your card template
  (`/{{IPA}}/`).
- At most two variants, joined with `, `.
- Phrases and sentences are transcribed in full. If the whole phrase isn't
  in the dictionary, it is built word by word with natural weak forms (`tə`,
  `ənd`), and unknown words become `[word]`. Sentence punctuation is kept.
- The headword is cleaned first (HTML, `&nbsp;`, `(verb)`-style labels,
  curly quotes).
- Each style rule (British vowel symbols, optional sounds, diacritics, stress
  on one-syllable words, weak forms) can be turned off in the JSON config.

## Claude fallback (optional)

Off by default; the add-on works fully without it. Turn it on in the Settings
dialog and enter **your own Anthropic API key**, or leave the key empty and
start Anki with the `ANTHROPIC_API_KEY` environment variable set. A key
entered in the dialog is stored in plain text in the add-on's config.

Only headwords the free sources missed or only partly found are sent, one
request per headword, to `claude-haiku-5-5`. The cost is billed to your key:
about $0.07 per 1,000 lookups. Results are cached and tagged `ipa::llm` for
review.

## Privacy and network

The add-on contacts these hosts only while it runs a lookup:

| Host | When | What is sent |
|---|---|---|
| `freedictionaryapi.com` | always (first source) | the headword text, or single words of it |
| `en.wiktionary.org` | when the first source has no answer | the same |
| `api.anthropic.com` | **only** with Claude enabled and your key | the cleaned headword text (word, phrase or sentence) the free sources missed |

No other note fields, deck names or account data are sent. Requests carry a
User-Agent with the add-on's name, version and homepage. Lookups are cached in
the add-on folder at `user_files/ipa_cache.json` (*Tools > Add-ons > View
Files*); it survives updates, and deleting it forces fresh lookups. Each site
gets at most one request per second, and the add-on backs off when a site
rate-limits.

## Licence and attribution

- **Code:** MIT License, see [LICENSE](LICENSE). Copyright 2026 Paweł
  Warowny.
- **Pronunciation data:** from [Wiktionary](https://en.wiktionary.org/)
  contributors, under
  [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/), retrieved
  via [FreeDictionaryAPI.com](https://freedictionaryapi.com/) and the
  [Wiktionary API](https://en.wiktionary.org/w/api.php). The add-on
  normalises these transcriptions (British style rules), so the IPA written
  into your notes is adapted CC BY-SA material. If you share or publish a deck
  filled this way, credit Wiktionary and keep that IPA under CC BY-SA 4.0.
  Full notice: [NOTICE.txt](NOTICE.txt) (shipped in the package).
- **Claude output** (`ipa::llm`) is generated under your own Anthropic
  account and its terms.

## Support

Bugs and questions: [GitHub issues](https://github.com/warpek/anki_ipa/issues).

## Development

Needs Python 3.13+ with Anki's `aqt`/`anki` packages importable (a system
Anki install, or the PyPI `aqt` wheels in a venv).

```sh
python3 -m venv --system-site-packages .venv && .venv/bin/pip install pytest mypy
.venv/bin/pytest                      # offline tests (fixtures, temp collection)
.venv/bin/pytest -m network           # live smoke tests against the real sources
.venv/bin/mypy src/anki_ipa tests     # strict, targets Python 3.13

# test against an official Anki release from PyPI
uv venv --python 3.13 /tmp/v2507 && uv pip install --python /tmp/v2507 "aqt==25.7.5" pytest
/tmp/v2507/bin/python -m pytest

# dev install: symlink the source folder into your add-ons folder, restart Anki
ln -s "$PWD/src/anki_ipa" ~/.local/share/Anki2/addons21/anki_ipa

# package: anki_ipa.ankiaddon at the repo root (adds LICENSE.txt + NOTICE.txt)
scripts/package.sh            # refuses if src/anki_ipa, LICENSE or NOTICE.txt have uncommitted changes
scripts/package.sh --dirty    # build anyway, for local testing
```

Layout: `src/anki_ipa/core/` is pure Python (cleaning, normalisation,
sources, resolver, cache, HTTP) with no Qt imports, so it is unit-testable;
`ui.py`, `editor_button.py`, `settings_dialog.py` and `progress_bar.py` are
the Anki/Qt glue, and `ops.py` holds the collection operations.

# IPA Filler (Anki add-on)

Fills empty IPA fields in Anki notes with broad British (RP) transcriptions in
learner's-dictionary style (`ˈrekɔːd`, `əʊ`, `r`). It works for single words,
phrases and whole sentences. The free sources come first, and Claude can fill
in what they miss if you enable it. Built for Anki 26.08 (minimum 23.10).
The defaults target a note type `English words` with fields `English` →
`IPA-english`, and you can change them in the add-on config.

## Install

- **From a package:** run `scripts/package.sh`, then in Anki use *Tools >
  Add-ons > Install from file…* and pick `anki_ipa.ankiaddon`. Restart Anki.
- **Development:** symlink the source folder into your add-ons folder, then
  restart Anki:

  ```sh
  ln -s "$PWD/src/anki_ipa" ~/.local/share/Anki2/addons21/anki_ipa
  ```

Back up your collection before the first large run (*File > Create Backup*).

## Usage

- **Browser > Notes > Fill missing IPA** (also in the right-click menu) fills
  the selected notes, or every matching note with an empty IPA field when
  nothing is selected. It shows progress (Esc cancels), writes everything as
  one undo step, and ends with a summary.
- The **IPA** button in the editor fills the note you are editing. It asks
  before it replaces existing IPA.
- Filled notes are tagged `ipa::auto`. Notes are also tagged `ipa::partial`
  when some words could only be written as `[word]`, and `ipa::llm` when
  Claude wrote the IPA. Review them with the Browser searches
  `tag:ipa::partial` and `tag:ipa::llm`.

The add-on's config page (*Tools > Add-ons > IPA Filler > Config*) documents
every option: target fields, sources, style, Claude, tags, the cache and the
rate limits.

## Sources

1. [FreeDictionaryAPI.com](https://freedictionaryapi.com/): a free, keyless
   API that serves parsed English Wiktionary entries.
2. The [Wiktionary API](https://en.wiktionary.org/w/api.php) (`action=raw`
   page wikitext, read for `{{IPA|en|…}}` and its accent labels).
3. Optional: the [Claude API](https://docs.anthropic.com/) with your own key
   (model `claude-haiku-5-5`). It is off by default, and the add-on works
   fully without it. When it is on, only headwords the free sources missed are
   sent, and results are tagged `ipa::llm`.

Lookups are cached in the add-on's `user_files/ipa_cache.json`. Requests are
throttled to one per second per site and back off when a site rate-limits.

## Attribution and licensing

- **Pronunciation data:** the IPA comes from
  [Wiktionary](https://en.wiktionary.org/) contributors and is licensed under
  [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). The add-on
  gets it through [FreeDictionaryAPI.com](https://freedictionaryapi.com/) and
  the [Wiktionary API](https://en.wiktionary.org/w/api.php). The add-on
  normalises these transcriptions to the deck's style, so the IPA written into
  your notes is adapted from CC BY-SA material. If you share or publish a deck
  filled this way, credit Wiktionary and keep that IPA under CC BY-SA 4.0.
- **Claude output** (notes tagged `ipa::llm`) is generated under your own
  Anthropic account and its terms.
- **Code:** MIT License, see [LICENSE](LICENSE). Copyright 2026 Paweł Warowny.

## Development

```sh
/usr/bin/python3 -m venv --system-site-packages .venv && .venv/bin/pip install pytest mypy
.venv/bin/pytest                      # offline tests (fixtures, temp collection)
.venv/bin/pytest -m network           # live smoke tests against the real sources
.venv/bin/mypy src/anki_ipa tests     # strict
scripts/package.sh                    # -> anki_ipa.ankiaddon
```

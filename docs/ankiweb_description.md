<!--
=====================================================================
NOT PART OF THE DESCRIPTION: values for the other AnkiWeb upload fields.
Paste only what is BELOW the "Description starts here" line into the
Description box.

Title (<80 chars):  IPA Filler: fill missing IPA pronunciations (British/US, phrases)
Tags:               ipa pronunciation english dictionary phonetics
Support page:       https://github.com/warpek/anki_ipa/issues
Branch:             one branch; minimum 25.07, maximum 26.09.3 (newest tested).
                    Enter the maximum WITHOUT a leading "-", so newer Anki
                    versions can still install it.
Package:            anki_ipa.ankiaddon from `scripts/package.sh` (built from a
                    clean, committed tree; no --dirty).
=====================================================================
Description starts here
-->
**Fills empty IPA fields in your Anki notes with British (or American) dictionary transcriptions, for single words, phrases and whole sentences.**

## What it does

- **Browser ▸ Notes ▸ Fill missing IPA** (also in the Browser's right-click menu) fills the selected notes, or, with nothing selected, every note of your chosen note type whose IPA field is empty (also fields holding only `&nbsp;` or `<br>`). A progress bar with a Cancel button stays inside the Browser, and Anki remains usable while it runs.
- The whole run is **one undo step** (*Edit ▸ Undo "Fill IPA"*), followed by a summary: how many notes were filled from which source, which were partial, and which headwords were not found.
- Filled notes are tagged so you can review them in the Browser:
  - `ipa::auto`: filled by this add-on
  - `ipa::partial`: some words could not be found and are written as `[word]`
  - `ipa::llm`: written by Claude (only if you enable it, see below)
- Words no dictionary has are tagged `ipa::notfound` and skipped in later runs, so they are not looked up again and again. To retry one, remove the tag and run *Fill missing IPA* again; the dictionaries are asked afresh. Network errors are not tagged; those notes are retried next run.

## Setup

1. Install the add-on and restart Anki.
2. Open **Tools ▸ Add-ons ▸ IPA Filler ▸ Config**. This opens a Settings dialog: pick the **note type**, the **word field** (the headword to look up) and the **IPA field** (receives the transcription), and click OK. The dialog suggests a note type with an IPA-looking field.
3. Select a few notes in the Browser and run **Notes ▸ Fill missing IPA**. Back up your collection before the first large run (*File ▸ Create Backup*).

Until it is set up, the add-on offers to open the Settings dialog instead of running. *Advanced (JSON)…* in the dialog gives every option (sources, style rules, tags).

## Output style

- **British RP by default**, in learner's-dictionary style (`ˈrekɔːd`, `əʊ`, `r`, `e`, `æ`), with American as the fallback when no British form exists. You can switch to American first in the Settings dialog.
- At most two variants, separated by `, `.
- **Bare IPA without slashes** (`ˈnjuːsəns`, not `/ˈnjuːsəns/`), so put any slashes in your card template, e.g. `/{{IPA}}/`.
- Phrases and sentences are transcribed in full. If a whole phrase is not in the dictionary, it is built **word by word**, with natural weak forms of function words (`tə`, `ənd`, `ə`); words that still cannot be found are written as `[word]` and the note is tagged `ipa::partial`.
- The headword is cleaned first: HTML, `&nbsp;`, parentheticals like `(verb)`, curly quotes. Sentence punctuation is kept in the IPA.

## Optional: Claude fallback (off by default)

If you turn it on in the Settings dialog and give it **your own Anthropic API key** (or the `ANTHROPIC_API_KEY` environment variable), headwords the free dictionaries missed or only partly found are sent to Claude (model `claude-haiku-5-5`), one request per headword. Its results are tagged `ipa::llm` for review. Usage is billed to your key, roughly $0.07 per 1,000 lookups. A key entered in the dialog is stored in plain text in the add-on's config. The add-on works fully without it.

## Privacy and network

The add-on only contacts these hosts, and only when you run it:

- `freedictionaryapi.com` (FreeDictionaryAPI.com) and `en.wiktionary.org` (Wiktionary API): it sends the **headword text** from your word field, or single words from it, to look up their pronunciation.
- `api.anthropic.com`: **only if you enable Claude** with your own key. It then sends the cleaned headword text (word, phrase or sentence) of the notes the free sources missed.

Nothing else from your collection is sent: no other fields, no deck or account data. Requests carry a User-Agent naming the add-on and its version. IPA that was found is cached locally in the add-on's `user_files/ipa_cache.json` (kept across updates), so reruns don't fetch it again, and requests are throttled to one per second per site.

## Licences and attribution

- The pronunciation data comes from **Wiktionary contributors** (https://en.wiktionary.org), under **CC BY-SA 4.0** (https://creativecommons.org/licenses/by-sa/4.0/), retrieved via FreeDictionaryAPI.com (https://freedictionaryapi.com) and the Wiktionary API. The IPA written into your notes is adapted (normalised to British style) and stays under CC BY-SA 4.0: if you share a deck filled this way, credit Wiktionary and keep that licence. See `NOTICE.txt` in the add-on folder.
- The add-on's code is **MIT** licensed (`LICENSE.txt`).
- Claude output is generated under your own Anthropic account and its terms.

## Supported versions

Anki **25.07 or newer**, on Windows, macOS and Linux.

## Support and source

Bug reports and questions: https://github.com/warpek/anki_ipa/issues
Source code: https://github.com/warpek/anki_ipa

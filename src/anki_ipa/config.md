**IPA Filler** fills an empty IPA field from free online dictionaries (Wiktionary data), with an optional Claude fallback. The output is broad **British RP** by default, in learner's-dictionary style (`ˈrekɔːd`, `əʊ`, `r`).

**Settings dialog** (*Tools > Add-ons > IPA Filler > Config*)

You are reading the JSON editor, opened from the dialog's *Advanced (JSON)…* button (or directly, when no profile is open). Most people only need the dialog:

- **Note type**, **Word field** (the headword that is looked up) and **IPA field** (receives the transcription). Nothing is saved until you click OK. On first use the dialog suggests a note type with an IPA-looking field (`IPA`, `Pronunciation`, …), and takes the first other field as the word field. The two fields must differ.
- **Accent**: British first with American as the fallback (default), or the other way round.
- **Claude fallback**: off by default. When you turn it on, **only** the headwords the free dictionaries missed or only partly found are sent to Anthropic, with **your own** API key, which you enter in the dialog or provide as the `ANTHROPIC_API_KEY` environment variable. A key entered in the dialog is stored in plain text in the add-on's config.

Until a note type and both fields are chosen (or if they are renamed or deleted later), *Fill missing IPA* offers to open the dialog instead of running. After saving, run the action again. The settings are kept in the add-on's `meta.json` and survive add-on updates.

The JSON keys below cover everything, including the options the dialog doesn't show.

**Usage**

- *Browser > Notes > Fill missing IPA* (also in the right-click menu): fills the selected notes, or, with nothing selected, every note of the chosen note type whose IPA field is empty (a field holding only `&nbsp;` or `<br>` counts as empty). A progress bar at the bottom of the Browser shows each headword (it stays inside the Browser window, so it moves with it on tiling window managers). Cancel, or closing the Browser, stops the run, and whatever was found so far is still written. Anki stays usable meanwhile; a note whose IPA you fill by hand during the run is left alone. The whole run is a single *Edit > Undo* step ("Fill IPA"). At the end a summary lists the sources used, the partial and Claude results, and the headwords that were not found.

**Target fields** (set in the dialog; empty by default)

- `note_type`: the exact name of the note type to fill.
- `word_field`: the field with the headword to look up. HTML, `&nbsp;`, parentheticals like `(verb)` and sentence punctuation are cleaned up before the lookup.
- `ipa_field`: the field that receives the IPA. It is stored bare, without slashes or brackets, so put any `/…/` in the card template (e.g. `/{{IPA}}/`).
- `overwrite_existing`: `true` makes the Browser action refill notes whose IPA is already filled.

**Sources**

- `sources`: free sources, tried in this order: `freedictionary` (FreeDictionaryAPI.com), then `wiktionary` (the Wiktionary API). Both serve Wiktionary pronunciations.
- `accent_preference`: the accent order, `["uk", "us"]` (default) or `["us", "uk"]` (set in the dialog). At most 2 distinct variants are kept, joined with `, `. The style options below still give British vowel symbols; turn `british_vowels` off for US-style output.
- `word_by_word`: if a whole phrase isn't found, look up each word. Words that still can't be found are written as `[word]`.
- **Rate limits:** each site gets at most 1 request per second. If a site answers "too busy" (HTTP 429/503), the add-on waits as the site asks and retries, up to 3 times. If it is still busy, or fails 3 times in a row (e.g. you are offline), that source is turned off for the rest of the run, the next one carries on, and the summary says so. FreeDictionaryAPI.com allows about 1,000 requests per hour, so a large first run may finish on Wiktionary alone.
- **Cache:** IPA that was found is cached, so reruns don't fetch it again. Misses ("not found") are **not** cached: whether a note is looked up again is decided only by the `ipa::notfound` tag (see *Tags*). The cache is in the add-on folder (*Tools > Add-ons > IPA Filler > View Files*) at `user_files/ipa_cache.json`, and it survives add-on updates. Deleting it (with Anki closed) only makes found words get fetched again. Changing the style options below takes effect without clearing it. If the file can't be written (disk full, no permission), the filled notes are still saved and the summary says so; the cache is retried on the next run.

**Style** (applied to dictionary results; all default `true`)

- `normalize_map`: character replacements (default `ɹ`→`r`, `ɡ`→`g`). `strip_syllable_dots` removes syllable dots.
- `british_vowels`: dictionary-style British vowels: `ɛ`→`e`, `ɛː`→`eə`, `aː`→`ɑː`, and `a`→`æ` (but not in `aɪ`/`aʊ`).
- `strip_optional_sounds`: drops optional `(r)`, `(h)`, `(t)`, `(d)`, `(p)` and `(k)`. Other optional sounds are kept without the brackets (`ˈles(ə)n`→`ˈlesən`).
- `strip_diacritics`: removes combining marks and `ˑ` (`snəʊ̯`→`snəʊ`). `ː`, `ˈ` and `ˌ` are kept.
- `strip_monosyllable_stress`: drops the stress mark on one-syllable words (`ˈhiː`→`hiː`).
- `weak_forms`: in phrases built word by word, function words take their weak form (`to`→`tə`, `and`→`ənd`), except at the end of the phrase. Set it to `false` to use the strong forms.
- A tiny built-in list fixes single words where Wiktionary's first form isn't the usual British dictionary form (`until`→`ənˈtɪl`).

**Claude** (optional, off by default)

- `claude.enabled` (dialog): `true` sends what the free sources missed to Claude, one request per headword, with the whole phrase or sentence. Its reply is used instead. Nothing is sent to Anthropic while this is `false`.
- `claude.api_key` (dialog): your own Anthropic API key. It is **stored in plain text** in the add-on's `meta.json`. To keep it out of there, leave it empty and start Anki with the `ANTHROPIC_API_KEY` environment variable set.
- `claude.model`: default `claude-haiku-5-5`.
- `claude.for_partial`: `true` (default) also sends partial results (`[word]` placeholders). With `false`, only complete misses are sent.
- Cost: billed to your key, about $0.07 per 1,000 lookups with Haiku 5.5. Its answers are cached; a headword it can't transcribe is tagged `ipa::notfound` like any other miss. A bad key (HTTP 401) or repeated rate limiting (HTTP 429/529) turns Claude off for the rest of the run.

**Tags** (search them in the Browser)

- `tag_auto` (`ipa::auto`): the IPA was filled by this add-on. Find these notes with `tag:ipa::auto`.
- `tag_llm` (`ipa::llm`): the IPA came from Claude. Review these notes with `tag:ipa::llm`.
- `tag_partial` (`ipa::partial`): some words were not found and are shown as `[word]`. Fix these notes by hand, or with Claude enabled. Find them with `tag:ipa::partial`.
- `tag_not_found` (`ipa::notfound`): no IPA was found at all, and every source (including Claude, when enabled) really answered "not found". The IPA field stays empty, and later runs of *Fill missing IPA* skip these notes, so the same words are not looked up again and again. To retry a note, remove the tag (find them with `tag:ipa::notfound`, then *Notes > Remove Tags*) and run *Fill missing IPA* again: the tag is the only thing that skips a note, so the sources are asked afresh. Misses caused by network errors, rate limits or a disabled source are **not** tagged, so they are retried next time. Partial results get `ipa::partial`, not this tag. Set it to `""` to turn off both the tagging and the skipping.
- After review, remove a tag with *Notes > Remove Tags*. Set a tag to `""` to stop adding it.

The IPA comes from Wiktionary contributors, under CC BY-SA 4.0, and the IPA written into your notes is an adapted version under the same licence. See `NOTICE.txt` in the add-on folder (*View Files*) for the full attribution; the code is MIT (`LICENSE.txt`).

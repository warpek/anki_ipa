**IPA Filler** fills an empty IPA field from free online dictionaries (Wiktionary data), with an optional Claude fallback.

**Usage**

- *Browser > Notes > Fill missing IPA* (also in the right-click menu): fills the selected notes, or, with nothing selected, every `note_type` note whose `ipa_field` is empty. A progress window shows each headword; Esc cancels, and whatever was found so far is still written. The whole run is a single *Edit > Undo* step ("Fill IPA"). At the end a summary lists the sources used, the partial and Claude results, and the headwords that were not found.
- **IPA** button in the editor toolbar (Browser, Edit and Add windows): fills the note being edited. It does nothing on other note types, and it asks before replacing an IPA field that already has text. On an existing note it is one undo step. In the Add window it fills the field only, and the note is saved when you click Add. The experimental Svelte editor (Preferences > Editing) supports the button in the Browser and Edit windows but not in its Add window.

**Target fields**

- `note_type`: the note type to fill (default `English words`).
- `word_field`: the field with the headword to look up (default `English`). HTML, `&nbsp;`, parentheticals like `(verb)` and sentence punctuation are cleaned up before the lookup.
- `ipa_field`: the field that receives the IPA (default `IPA-english`). It is stored without slashes, because the card template adds them.
- `overwrite_existing`: `true` makes the Browser action refill notes whose IPA is already filled. The editor button always asks instead.

**Sources**

- `sources`: free sources, tried in this order: `freedictionary` (FreeDictionaryAPI.com), then `wiktionary` (the Wiktionary API). Both serve Wiktionary pronunciations.
- `accent_preference`: the accent order, e.g. `["uk", "us"]`. At most 2 distinct variants are kept, joined with `, `.
- `word_by_word`: if a whole phrase isn't found, look up each word. Words that still can't be found are written as `[word]`.
- **Rate limits:** each site gets at most 1 request per second. If a site answers "too busy" (HTTP 429/503), the add-on waits as the site asks and retries, up to 3 times. If it is still busy, or fails 3 times in a row (e.g. you are offline), that source is turned off for the rest of the run, the next one carries on, and the summary says so. FreeDictionaryAPI.com allows about 1,000 requests per hour, so a large first run may finish on Wiktionary alone.
- **Cache:** every lookup is cached, misses too (misses are retried after 30 days). The cache is in the add-on folder (*Tools > Add-ons > IPA Filler > View Files*) at `user_files/ipa_cache.json`, and it survives add-on updates. To start fresh, close Anki and delete that file. Changing the style options below takes effect without clearing it.

**Style** (applied to dictionary results; all default `true`)

- `normalize_map`: character replacements (default `ɹ`→`r`, `ɡ`→`g`). `strip_syllable_dots` removes syllable dots.
- `british_vowels`: dictionary-style British vowels: `ɛ`→`e`, `ɛː`→`eə`, `aː`→`ɑː`, and `a`→`æ` (but not in `aɪ`/`aʊ`).
- `strip_optional_sounds`: drops optional `(r)`, `(h)`, `(t)`, `(d)`, `(p)` and `(k)`. Other optional sounds are kept without the brackets (`ˈles(ə)n`→`ˈlesən`).
- `strip_diacritics`: removes combining marks and `ˑ` (`snəʊ̯`→`snəʊ`). `ː`, `ˈ` and `ˌ` are kept.
- `strip_monosyllable_stress`: drops the stress mark on one-syllable words (`ˈhiː`→`hiː`).
- `weak_forms`: in phrases built word by word, function words take their weak form (`to`→`tə`, `and`→`ənd`), except at the end of the phrase. Set it to `false` to use the strong forms.
- A tiny built-in list fixes single words where Wiktionary's first form isn't the usual British dictionary form (`until`→`ənˈtɪl`).

**Claude** (optional, off by default)

- `claude.enabled`: `true` sends anything the free sources missed to Claude, one request per headword, with the whole phrase or sentence. Its reply is used instead.
- `claude.api_key`: your own Anthropic API key. It is **stored in plain text** in the add-on's `meta.json`. To keep it out of there, leave it empty and start Anki with the `ANTHROPIC_API_KEY` environment variable set.
- `claude.model`: default `claude-haiku-5-5`.
- `claude.for_partial`: `true` (default) also sends partial results (`[word]` placeholders). With `false`, only complete misses are sent.
- Cost: billed to your key, about $0.07 per 1,000 lookups with Haiku 5.5. Results are cached. A bad key (HTTP 401) or repeated rate limiting (HTTP 429/529) turns Claude off for the rest of the run.

**Tags** (added to every filled note; search them in the Browser)

- `tag_auto` (`ipa::auto`): the IPA was filled by this add-on. Find these notes with `tag:ipa::auto`.
- `tag_llm` (`ipa::llm`): the IPA came from Claude. Review these notes with `tag:ipa::llm`.
- `tag_partial` (`ipa::partial`): some words were not found and are shown as `[word]`. Fix these notes by hand, or with Claude enabled. Find them with `tag:ipa::partial`.
- After review, remove a tag with *Notes > Remove Tags*. Set a tag to `""` to stop adding it.

The IPA comes from Wiktionary, under CC BY-SA 4.0. See the add-on's README for attribution.

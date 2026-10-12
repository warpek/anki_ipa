# Your to-do list: publishing IPA Filler 0.1.0

What is already done: the public repo https://github.com/warpek/anki_ipa and the GitHub release
https://github.com/warpek/anki_ipa/releases/tag/v0.1.0 (with `anki_ipa.ankiaddon` attached).

## 1. Upload to AnkiWeb (manual; AnkiWeb has no upload API)
- [ ] Log in at https://ankiweb.net and open https://ankiweb.net/shared/upload
- [ ] **File:** `anki_ipa.ankiaddon` from this folder (or download it from the GitHub release).
- [ ] **Title, Tags, Support page:** copy them from the comment block at the top of
      `docs/ankiweb_description.md`.
- [ ] **Description:** paste everything *below* the "Description starts here" line of
      `docs/ankiweb_description.md`.
- [ ] **Branch:** min `25.07`, max `26.09.3`. Do **not** put a `-` before the max, so newer Anki
      versions can still install it.
- [ ] Note the add-on's AnkiWeb link and numeric code once it's published.

## 2. Add the AnkiWeb link to the README
- [ ] In `README.md` (Install section, "From AnkiWeb"), replace "(AnkiWeb link and code added after
      the first upload)" with the real link and code. Commit and push, or ask Claude to do it.

## 3. Ask someone to test on Windows and macOS
- [ ] Testing so far was on Linux only: headless runs of official Anki 25.07.5, 26.08.1 and 26.09.3.
      Ask one person on Windows and one on macOS to install the release, open the settings, run
      *Fill missing IPA* on a few notes and try *Edit > Undo*.

## 4. Set up your own install again
- [ ] Back up first (*File > Create Backup*).
- [ ] Restart Anki. The first *Fill missing IPA* asks you to set it up.
      The dialog preselects `English words` / `English` / `IPA-english`, so just press OK.
      (The defaults are now empty, so this one-time step is needed.)
- [ ] Optional: in the same dialog, turn on Claude and enter your Anthropic API key.

## 5. Later releases
- [ ] Bump `human_version` in `src/anki_ipa/manifest.json`, commit everything, and run
      `scripts/package.sh`. It refuses to build an uncommitted tree unless you pass `--dirty`.
- [ ] Create a GitHub release (`gh release create vX.Y.Z anki_ipa.ankiaddon`).
- [ ] On AnkiWeb, edit the add-on (https://ankiweb.net/shared/mine) and upload the new file to the
      same branch. Raise the branch's max version after you test a newer Anki.

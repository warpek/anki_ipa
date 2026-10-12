This folder holds the IPA Filler lookup cache (JSON).
Anki keeps the contents of user_files/ when the add-on is upgraded.
It holds only IPA that was found; misses are not cached (the ipa::notfound
tag decides whether a note is looked up again). Deleting the cache file only
makes found words get fetched again.

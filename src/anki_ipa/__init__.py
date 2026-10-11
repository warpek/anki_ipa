"""IPA Filler: fills missing IPA in the Browser. Registers hooks only."""

import sys

# Register hooks only inside a running Anki (aqt imported and mw set), so that
# tests can import anki_ipa.core without pulling in Qt.
_aqt = sys.modules.get("aqt")
if _aqt is not None and getattr(_aqt, "mw", None) is not None:
    from .ui import register

    register(__name__)

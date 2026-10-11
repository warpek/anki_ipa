import json
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "src" / "anki_ipa"


def test_core_imports_without_qt() -> None:
    import anki_ipa.core
    import anki_ipa.core.sources  # noqa: F401

    assert "aqt" not in sys.modules or sys.modules["aqt"].mw is None


def test_manifest_and_config_parse() -> None:
    manifest = json.loads((PKG / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["package"] == "anki_ipa"
    config = json.loads((PKG / "config.json").read_text(encoding="utf-8"))
    assert config["ipa_field"] == "IPA-english"
    assert config["claude"]["enabled"] is False

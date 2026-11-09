"""Headless smoke checks for translation enrichment and local word-cache output."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import tab_translation as translation_tab


def assert_condition(condition: bool, message: str) -> None:
    """What: Raise AssertionError with the supplied explanation when false.

    Why included: Keeps the standalone smoke test independent from a test framework.
    Where used: Assertions throughout run_strongs_translation_tests().
    Returns: None when condition is true; raises AssertionError otherwise.
    """
    if not condition:
        raise AssertionError(message)


def run_strongs_translation_tests(temp_root: Path) -> None:
    """Exercise Strong's enrichment and serialized translation word-cache output.

    What: Creates a temporary Greek dictionary, enriches a sample word, and verifies
    meanings, Strong's metadata, completeness, and absence of removed network APIs.
    Why included: Protects offline translation enrichment and persistence behavior.
    Where used: main_cli() in this standalone backend smoke test.
    Returns: None; failed expectations raise AssertionError.
    """
    word_cache_root = temp_root / "Data" / "translation_word_cache"
    biblical_lexicon_root = temp_root / "Data" / "biblical_lexicon"
    biblical_lexicon_root.mkdir(parents=True, exist_ok=True)
    (biblical_lexicon_root / "strongs-greek-dictionary.js").write_text(
        'var strongsGreekDictionary = {"G3056":{"lemma":"λογος","strongs_def":"something said","kjv_def":"word, speech"}}; module.exports = strongsGreekDictionary;',
        encoding="utf-8",
    )
    translation_tab._STRONGS_INDEX_CACHE.pop("el", None)
    translation_tab._STRONGS_NUMBER_CACHE.pop("el", None)

    source = {
        "id": "sample-greek-source",
        "display_name": "Sample Greek Source",
        "language_code": "el",
    }
    words = {
        "λογος": {
            "lemma": "λογος",
            "afr": "woord | spraak",
            "eng": "word",
            "detail_complete": True,
        }
    }
    result = translation_tab.enrich_word_directory_meanings(
        source,
        word_cache_root,
        list(words),
        words,
        source_group=[source],
        mark_deep_complete=True,
    )

    assert_condition("speech" in result["λογος"]["eng"], "Strong's should append English senses")
    assert_condition(result["λογος"]["detail_complete"], "Multiple EN/AF meanings should be complete")
    cache_path = translation_tab.get_word_cache_path(word_cache_root, source)
    saved_payload = json.loads(cache_path.read_text(encoding="utf-8"))
    saved_word = saved_payload["words"]["λογος"]
    assert_condition(saved_word["strongs"] == "G3056", "Strong's number should be saved")
    assert_condition(len(saved_word["eng_meanings"]) > 1, "English meaning array should be explicit")
    assert_condition(len(saved_word["afr_meanings"]) > 1, "Afrikaans meaning array should be explicit")
    assert_condition(not hasattr(translation_tab, "request_translation_payload"), "Network translator must be removed")
    assert_condition(not hasattr(translation_tab, "launch_headless_translation_build"), "Headless launcher must be removed")


def main_cli() -> None:
    """Run translation backend checks using disposable local data.

    What: Creates a temporary root, invokes the Strong's checks, removes the root
    in a finally block, and prints a success message.
    Why included: Makes the smoke test runnable without modifying real app data.
    Where used: Module entry point when this script is executed directly.
    Returns: None; cleanup runs even when a check fails.
    """
    temp_root = Path(tempfile.mkdtemp(prefix="the_light_smoke_"))
    try:
        run_strongs_translation_tests(temp_root)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
    print("Reliability smoke tests passed.")


if __name__ == "__main__":
    main_cli()

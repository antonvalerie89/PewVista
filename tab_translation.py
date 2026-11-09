"""Read normalized Bible caches and build/search Greek and Hebrew word data."""

from __future__ import annotations

import ctypes
import json
import os
import re
import time
import unicodedata
from pathlib import Path
import tkinter as tk
from tkinter import ttk

from app_paths import get_app_base_path as default_get_app_base_path, get_app_data_path

WORD_CACHE_FILE_SUFFIX = ".words.json"
WORD_CACHE_SUMMARY_FILE_SUFFIX = ".summary.json"
BUILD_STATUS_FILE_SUFFIX = ".build_status.json"
BUILD_LOCK_FILE_SUFFIX = ".build.lock.json"
MEANING_SEPARATOR = " | "
STATUS_UPDATE_INTERVAL = 100
BASE_SAVE_INTERVAL = 1024
DETAIL_BUILD_VERSION = 6
BUILD_LOCK_STALE_SECONDS = 60.0 * 60.0 * 6.0
STATUS_SUMMARY_CACHE_SECONDS = 15.0
SHARED_WORD_CACHE_NAMES = {
    "el": "greek",
    "he": "hebrew",
}
STRONGS_DICTIONARY_FILES = {
    "el": "strongs-greek-dictionary.js",
    "he": "strongs-hebrew-dictionary.js",
}
HEBREW_FORM_STRONGS_OVERRIDES = {
    "היתה": "H1961",
    "עלפני": "H6440",
    "מרחפת": "H7363",
}
_STRONGS_INDEX_CACHE: dict[str, dict[str, dict[str, object]]] = {}
_STRONGS_NUMBER_CACHE: dict[str, dict[str, dict[str, object]]] = {}
_GREEK_FORM_LEMMA_CACHE: dict[Path, dict[str, str]] = {}
ORIGINAL_LANGUAGE_NAME_HINTS = {
    "el": ("greek",),
    "he": ("hebrew", "westminster", "wlc"),
}
BOOK_NAMES = (
    "Genesis", "Exodus", "Leviticus", "Numbers", "Deuteronomy", "Joshua", "Judges", "Ruth",
    "1 Samuel", "2 Samuel", "1 Kings", "2 Kings", "1 Chronicles", "2 Chronicles", "Ezra", "Nehemiah",
    "Esther", "Job", "Psalms", "Proverbs", "Ecclesiastes", "Song of Solomon", "Isaiah", "Jeremiah",
    "Lamentations", "Ezekiel", "Daniel", "Hosea", "Joel", "Amos", "Obadiah", "Jonah", "Micah", "Nahum",
    "Habakkuk", "Zephaniah", "Haggai", "Zechariah", "Malachi", "Matthew", "Mark", "Luke", "John", "Acts",
    "Romans", "1 Corinthians", "2 Corinthians", "Galatians", "Ephesians", "Philippians", "Colossians",
    "1 Thessalonians", "2 Thessalonians", "1 Timothy", "2 Timothy", "Titus", "Philemon", "Hebrews", "James",
    "1 Peter", "2 Peter", "1 John", "2 John", "3 John", "Jude", "Revelation",
    "Tobit", "Judith", "Additions to Esther", "Wisdom of Solomon", "Ecclesiasticus",
    "Baruch", "Prayer of Azariah", "Susanna", "Bel and the Dragon", "1 Maccabees",
    "2 Maccabees", "1 Esdras", "Prayer of Manasseh", "2 Esdras",
)


def normalize_text(value: str) -> str:
    """Normalize whitespace and nonbreaking spaces in source/meaning text.

    What: Replaces NBSP with ordinary spaces and collapses repeated whitespace.
    Why included: Cache matching must not vary because of presentation formatting.
    Where used: Word tokenization, lexicon lookup, and meaning normalization.
    Returns: Single-space normalized text.
    """
    return " ".join((value or "").replace("\u00a0", " ").split())


def get_book_display_name(book_number: int) -> str:
    """Map a normalized Bible book number to its canonical/additional-book label.

    What: Indexes the standard 66 books plus the additional-book order used by local caches.
    Why included: Normalized cache filenames store book numbers, not display names.
    Where used: build_source_book_catalog() for reader selectors.
    Returns: Known book name, or a Book NN fallback for an unmapped number.
    """
    index = int(book_number) - 1
    return BOOK_NAMES[index] if 0 <= index < len(BOOK_NAMES) else f"Book {book_number:02d}"


def is_original_word_character(character: str) -> bool:
    """Check whether a character belongs inside a Greek/Hebrew source token.

    What: Accepts letters, combining marks, apostrophes, and language hyphens.
    Why included: Tokenization must retain script marks and internal punctuation.
    Where used: tokenize_original_words() while scanning verse text.
    Returns: True for accepted token characters.
    """
    if not character:
        return False
    if character in {"'", "’", "-", "־"}:
        return True
    category = unicodedata.category(character)
    return character.isalpha() or category in {"Mn", "Mc"}


def tokenize_original_words(verse_text: str) -> list[str]:
    """Split original-language verse text into normalized word tokens.

    What: Scans normalized characters, keeps letters/marks/internal punctuation, and trims
    boundary punctuation from completed tokens.
    Why included: Translation lookup and dictionary building operate per source word.
    Where used: Meaning rows and full-language word-directory builds.
    Returns: Ordered token strings with original display spelling preserved.
    """
    tokens: list[str] = []
    current: list[str] = []
    for character in normalize_text(verse_text):
        if is_original_word_character(character):
            current.append(character)
            continue
        if current:
            token = "".join(current).strip("-'’־")
            if token:
                tokens.append(token)
            current = []
    if current:
        token = "".join(current).strip("-'’־")
        if token:
            tokens.append(token)
    return tokens


def build_verse_meaning_rows(
    verse_text: str,
    word_directory: dict[str, dict[str, str]],
    source_language: str,
) -> list[tuple[str, str, str]]:
    """Build display rows that align source tokens with Afrikaans and English meanings.

    What: Normalizes lookup keys, searches exact and case-folded forms, and emits one row
    for each token in the verse.
    Why included: The Translation tab presents cached meanings beside original words.
    Where used: render_selected_verse() when displaying a cached passage.
    Returns: Tuples of source word, Afrikaans value, and English value.
    """
    rows: list[tuple[str, str, str]] = []
    if not isinstance(word_directory, dict):
        word_directory = {}
    normalized_directory = {
        normalize_word_key(str(key), source_language).casefold(): value
        for key, value in word_directory.items()
        if isinstance(value, dict) and normalize_word_key(str(key), source_language)
    }
    for token in tokenize_original_words(verse_text):
        lookup_candidates = {
            normalize_word_key(token, source_language),
            normalize_word_key(token, source_language).casefold(),
            token.casefold(),
            token,
        }
        entry: dict[str, str] = {}
        for candidate in lookup_candidates:
            if not candidate:
                continue
            if candidate in word_directory and isinstance(word_directory[candidate], dict):
                entry = word_directory[candidate]
                break
            if candidate.casefold() in normalized_directory and isinstance(normalized_directory[candidate.casefold()], dict):
                entry = normalized_directory[candidate.casefold()]
                break
        rows.append((
            token,
            str(entry.get("afr") or ""),
            str(entry.get("eng") or ""),
        ))
    return rows


def normalize_word_key(token: str, source_language: str) -> str:
    """Canonicalize a word for dictionary lookup without changing its display form.

    What: Removes combining marks, normalizes Unicode, and strips Hebrew maqaf where needed.
    Why included: Equivalent accented/normalized forms should match one cache key.
    Where used: Token indexes, Strong's matching, and word-cache serialization.
    Returns: Canonical lookup key, or empty text for empty input.
    """
    normalized_token = normalize_text(token)
    if not normalized_token:
        return ""
    collapsed_characters: list[str] = []
    for character in unicodedata.normalize("NFD", normalized_token):
        if unicodedata.category(character) in {"Mn", "Mc"}:
            continue
        collapsed_characters.append(character)
    collapsed_token = "".join(collapsed_characters)
    if source_language == "he":
        collapsed_token = collapsed_token.replace("־", "")
    return normalize_text(unicodedata.normalize("NFC", collapsed_token)).strip("-'’־")


def get_lookup_word(token: str, source_language: str) -> str:
    """Apply language-specific spelling normalization to a lookup token.

    What: Converts Greek terminal sigma to its standard final form.
    Why included: Greek dictionaries may index lemma spellings with final sigma.
    Where used: Lookup workflows that match source text to Greek lexicon entries.
    Returns: Normalized lookup word; display token is not modified.
    """
    lookup_word = normalize_text(token)
    if source_language == "el" and lookup_word.endswith("σ"):
        return f"{lookup_word[:-1]}ς"
    return lookup_word


def load_strongs_lemma_index(word_cache_root: Path, source_language: str) -> dict[str, dict[str, object]]:
    """Load the local Strong's JavaScript dictionary into lemma and number indexes.

    What: Parses the embedded JSON object, normalizes lemma keys, and caches both lookup maps.
    Why included: Deep word enrichment needs offline lexicon access without network requests.
    Where used: apply_strongs_entry() for Greek/Hebrew dictionary matching.
    Returns: Normalized lemma index; empty mapping when the file is absent or invalid.
    """
    cached_index = _STRONGS_INDEX_CACHE.get(source_language)
    if isinstance(cached_index, dict):
        return cached_index
    dictionary_name = STRONGS_DICTIONARY_FILES.get(source_language, "")
    dictionary_path = word_cache_root.parent / "biblical_lexicon" / dictionary_name
    if not dictionary_name or not dictionary_path.exists():
        return {}
    try:
        dictionary_text = dictionary_path.read_text(encoding="utf-8-sig")
        dictionary_payload = json.loads(dictionary_text[dictionary_text.index("{"):dictionary_text.rindex("}") + 1])
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    lemma_index: dict[str, dict[str, object]] = {}
    number_index: dict[str, dict[str, object]] = {}
    for strongs_number, raw_entry in dictionary_payload.items():
        if not isinstance(raw_entry, dict):
            continue
        lemma = normalize_word_key(str(raw_entry.get("lemma") or ""), source_language).casefold()
        if not lemma:
            continue
        entry = dict(raw_entry)
        entry["strongs"] = str(strongs_number)
        lemma_index[lemma] = entry
        number_index[str(strongs_number)] = entry
    _STRONGS_INDEX_CACHE[source_language] = lemma_index
    _STRONGS_NUMBER_CACHE[source_language] = number_index
    return lemma_index


def load_greek_form_lemma_index(word_cache_root: Path) -> dict[str, str]:
    """Load the local MorphGNT mapping from inflected Greek forms to lemmas.

    What: Reads the JSON mapping, canonicalizes both sides, and caches it by file path.
    Why included: Greek Strong's lookup often requires a lemma instead of the inflected form.
    Where used: get_strongs_lookup_candidates() during enrichment.
    Returns: Normalized form-to-lemma dictionary.
    """
    index_path = word_cache_root.parent / "biblical_lexicon" / "morphgnt-greek-forms.json"
    cached_index = _GREEK_FORM_LEMMA_CACHE.get(index_path)
    if isinstance(cached_index, dict):
        return cached_index
    payload = read_json_payload(index_path)
    form_lemma_index = {
        normalize_word_key(str(form), "el").casefold(): normalize_word_key(str(lemma), "el").casefold()
        for form, lemma in payload.items()
        if normalize_word_key(str(form), "el") and normalize_word_key(str(lemma), "el")
    }
    _GREEK_FORM_LEMMA_CACHE[index_path] = form_lemma_index
    return form_lemma_index


def get_strongs_lookup_candidates(
    word: str,
    lemma: str,
    source_language: str,
    greek_form_lemmas: dict[str, str] | None = None,
) -> list[str]:
    """Generate ordered candidate keys for Strong's lookup from a source word and lemma.

    What: Combines normalized forms, Greek inflection mappings, and Hebrew prefix/final-letter
    variants, deduplicating candidates as it expands them.
    Why included: Lexicon data may index lemmas or simplified orthographic forms.
    Where used: apply_strongs_entry() before searching the Strong's index.
    Returns: Ordered unique lookup strings.
    """
    candidates = deduplicate_meanings([
        normalize_word_key(lemma, source_language).casefold(),
        normalize_word_key(word, source_language).casefold(),
    ])
    if source_language == "el":
        form_lemma = (greek_form_lemmas or {}).get(
            normalize_word_key(word, source_language).casefold()
        )
        if form_lemma:
            candidates.insert(0, form_lemma)
        return deduplicate_meanings(candidates)
    if source_language != "he":
        return candidates
    prefixes = {"ו", "ה", "ב", "כ", "ל", "מ", "ש"}
    compound_prefixes = ("על", "אל", "מן")
    pending = list(candidates)
    for _ in range(2):
        next_pending: list[str] = []
        for candidate in pending:
            if len(candidate) > 2 and candidate[0] in prefixes:
                stripped = candidate[1:]
                if stripped not in candidates:
                    candidates.append(stripped)
                    next_pending.append(stripped)
        pending = next_pending
    for candidate in list(candidates):
        for prefix in compound_prefixes:
            if candidate.startswith(prefix) and len(candidate) > len(prefix) + 1:
                candidates.append(candidate[len(prefix):])
        if candidate.endswith("תה") and len(candidate) > 3:
            candidates.append(candidate[:-2] + "ה")
        final_letter = {"כ": "ך", "מ": "ם", "נ": "ן", "פ": "ף", "צ": "ץ"}.get(candidate[-1:])
        if final_letter:
            candidates.append(candidate[:-1] + final_letter)
    candidates = deduplicate_meanings(candidates)
    return candidates


def extract_strongs_meanings(strongs_entry: dict[str, object]) -> list[str]:
    """Extract distinct English senses from one Strong's dictionary entry.

    What: Keeps strongs_def intact and splits kjv_def only at top-level commas/semicolons.
    Why included: Parenthesized/bracketed punctuation should not fragment a single sense.
    Where used: apply_strongs_entry() when enriching English meanings.
    Returns: Ordered deduplicated meaning strings.
    """
    meanings: list[str] = []
    definition = normalize_text(str(strongs_entry.get("strongs_def") or "")).strip(" .;:")
    if definition:
        meanings.append(definition)
    kjv_definition = normalize_text(str(strongs_entry.get("kjv_def") or ""))
    current_part: list[str] = []
    nesting_depth = 0
    for character in kjv_definition:
        if character in "([":
            nesting_depth += 1
        elif character in ")]" and nesting_depth > 0:
            # Only matching closes reduce depth; unmatched punctuation must not expose inner separators.
            nesting_depth -= 1
        if character in ",;" and nesting_depth == 0:
            # Split only top-level punctuation so commas inside parenthetical senses stay intact.
            meanings.append("".join(current_part).strip(" .;:"))
            current_part = []
        else:
            current_part.append(character)
    meanings.append("".join(current_part).strip(" .;:"))
    return deduplicate_meanings(meanings)


def apply_strongs_entry(
    word_cache_root: Path,
    word: str,
    source_language: str,
    entry: dict[str, str],
) -> bool:
    """Add matching Strong's number, lemma, and dictionary senses to one word entry.

    What: Applies language-specific candidates, merges English senses, and marks the entry
    as checked when a match exists.
    Why included: Local Strong's data supplements base translations with deeper meanings.
    Where used: enrich_word_directory_meanings() during deep cache builds.
    Returns: True when a matching entry adds multiple English senses, otherwise False.
    """
    strongs_index = load_strongs_lemma_index(word_cache_root, source_language)
    strongs_entry = None
    override_number = HEBREW_FORM_STRONGS_OVERRIDES.get(normalize_word_key(word, source_language)) if source_language == "he" else None
    if override_number:
        strongs_entry = (_STRONGS_NUMBER_CACHE.get(source_language) or {}).get(override_number)
    greek_form_lemmas = load_greek_form_lemma_index(word_cache_root) if source_language == "el" else None
    for candidate in get_strongs_lookup_candidates(
        word,
        str(entry.get("lemma") or ""),
        source_language,
        greek_form_lemmas,
    ):
        if isinstance(strongs_entry, dict):
            break
        strongs_entry = strongs_index.get(candidate)
        if isinstance(strongs_entry, dict):
            break
    if not isinstance(strongs_entry, dict):
        return False
    strongs_meanings = extract_strongs_meanings(strongs_entry)
    if not strongs_meanings:
        return False
    english_meanings = deduplicate_meanings(normalize_meaning_values(entry.get("eng")) + strongs_meanings)
    entry["eng"] = MEANING_SEPARATOR.join(english_meanings)
    entry["lemma"] = str(strongs_entry.get("lemma") or entry.get("lemma") or word)
    entry["strongs"] = str(strongs_entry.get("strongs") or "")
    return len(english_meanings) > 1


def token_matches_source_language(token: str, source_language: str) -> bool:
    """Check that a token contains the selected original-language script.

    What: Accepts Greek/Hebrew script ranges, marks, and internal punctuation while rejecting
    foreign alphabetic/digit characters.
    Why included: Mixed-script Bible caches should not pollute one language dictionary.
    Where used: Source discovery, token scanning, and cache canonicalization.
    Returns: True when the token matches/contains the target script.
    """
    normalized_token = normalize_text(token)
    if not normalized_token:
        return False
    script_ranges = {
        "he": ((0x0590, 0x05FF),),
        "el": ((0x0370, 0x03FF), (0x1F00, 0x1FFF)),
    }
    ranges = script_ranges.get(source_language)
    if not ranges:
        return True
    has_target_script = False
    for character in normalized_token:
        code_point = ord(character)
        if any(start <= code_point <= end for start, end in ranges):
            has_target_script = True
            continue
        if unicodedata.category(character) in {"Mn", "Mc"}:
            continue
        if character in {"'", "’", "-", "־"}:
            continue
        if character.isalpha() or character.isdigit():
            return False
    return has_target_script


def discover_original_language_sources(cache_root: Path) -> list[dict[str, object]]:
    """Discover cached Bible sources identified as Greek or Hebrew.

    What: Reads one sample book per cache folder and matches source names against language hints.
    Why included: Translation lookup should only offer caches with supported original-language text.
    Where used: refresh_sources() in the Translation tab.
    Returns: Display-name-sorted source mappings with cache directory/language metadata.
    """
    sources: list[dict[str, object]] = []
    if not cache_root.exists():
        return sources
    for cache_directory in sorted((path for path in cache_root.iterdir() if path.is_dir()), key=lambda item: item.name.casefold()):
        sample_files = sorted(cache_directory.glob("book_*.json"), key=lambda item: item.name.casefold())
        if not sample_files:
            continue
        try:
            payload = json.loads(sample_files[0].read_text(encoding="utf-8"))
        except Exception:
            continue
        source_name = str(payload.get("source_name") or cache_directory.name).strip() or cache_directory.name
        normalized_name = source_name.casefold()
        language_code = ""
        for candidate_language_code, hints in ORIGINAL_LANGUAGE_NAME_HINTS.items():
            if any(hint in normalized_name for hint in hints):
                language_code = candidate_language_code
                break
        if not language_code:
            continue
        sources.append(
            {
                "id": cache_directory.name,
                "display_name": source_name,
                "cache_directory": cache_directory,
                "language_code": language_code,
            }
        )
    return sorted(sources, key=lambda item: str(item.get("display_name") or "").casefold())


def load_book_payload(book_path: Path) -> dict[str, object] | None:
    """Read one normalized book JSON cache file defensively.

    What: Parses UTF-8 JSON and verifies the top-level value is an object.
    Why included: Cache files can be absent, corrupt, or replaced during background work.
    Where used: Catalog construction and chapter loading.
    Returns: Payload mapping, or None when unreadable/invalid.
    """
    try:
        payload = json.loads(book_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def build_source_book_catalog(source: dict[str, object]) -> list[dict[str, object]]:
    """Build an ordered book catalog from a source's normalized JSON files.

    What: Parses book numbers, chapter keys, and per-chapter verse counts from valid caches.
    Why included: Reader controls need book names and chapter/verse option bounds.
    Where used: Bible and Translation tab book selector helpers.
    Returns: Ordered book mappings with number, name, counts, and cache Path.
    """
    cache_directory = source.get("cache_directory")
    if not isinstance(cache_directory, Path):
        return []
    catalog: list[dict[str, object]] = []
    for book_path in sorted(cache_directory.glob("book_*.json"), key=lambda item: item.name.casefold()):
        match = re.fullmatch(r"book_(\d+)\.json", book_path.name, flags=re.IGNORECASE)
        if match is None:
            continue
        book_number = int(match.group(1) or 0)
        payload = load_book_payload(book_path)
        if not isinstance(payload, dict):
            continue
        chapters = payload.get("chapters")
        if not isinstance(chapters, dict) or not chapters:
            continue
        chapter_numbers = sorted(int(chapter_number) for chapter_number in chapters if str(chapter_number).isdigit())
        if not chapter_numbers:
            continue
        verse_counts = {
            int(chapter_number): len(chapter_verses)
            for chapter_number, chapter_verses in chapters.items()
            if str(chapter_number).isdigit() and isinstance(chapter_verses, dict)
        }
        catalog.append(
            {
                "number": book_number,
                "name": str(payload.get("book_name") or get_book_display_name(book_number)),
                "chapter_count": len(chapter_numbers),
                "verse_counts": verse_counts,
                "book_path": book_path,
            }
        )
    return catalog


def load_book_chapters(book_path: Path) -> dict[int, dict[int, str]]:
    """Normalize cached chapter JSON into integer-indexed verse text mappings.

    What: Validates chapter/verse dictionaries and drops invalid keys or blank text.
    Why included: Reader and translation code need consistent integer lookups.
    Where used: Verse readers, catalogs, and full-language cache building.
    Returns: Mapping of chapter number to verse-number/text mapping.
    """
    payload = load_book_payload(book_path)
    chapters = payload.get("chapters") if isinstance(payload, dict) else None
    if not isinstance(chapters, dict):
        return {}
    normalized_chapters: dict[int, dict[int, str]] = {}
    for chapter_number, chapter_verses in chapters.items():
        if not str(chapter_number).isdigit() or not isinstance(chapter_verses, dict):
            continue
        normalized_chapters[int(chapter_number)] = {
            int(verse_number): str(verse_text)
            for verse_number, verse_text in chapter_verses.items()
            if str(verse_number).isdigit() and str(verse_text).strip()
        }
    return normalized_chapters


def get_word_cache_path(word_cache_root: Path, source: dict[str, object]) -> Path:
    """Resolve the canonical shared word-cache file for a language/source.

    What: Uses greek/hebrew filenames for recognized language codes and source ID otherwise.
    Why included: Sources in one language should share a single dictionary cache.
    Where used: Word-directory load/save and metadata lookup operations.
    Returns: Path to the language cache JSON file.
    """
    source_language = str(source.get("language_code") or "")
    cache_name = SHARED_WORD_CACHE_NAMES.get(source_language, str(source.get("id") or "source"))
    return word_cache_root / f"{cache_name}{WORD_CACHE_FILE_SUFFIX}"


def get_word_cache_summary_path(word_cache_root: Path, source_language: str) -> Path:
    """Resolve the compact summary JSON path for one language cache.

    What: Builds a shared language-name filename with the summary suffix.
    Why included: The UI needs aggregate counts without loading the full dictionary.
    Where used: Summary build/load status functions.
    Returns: Summary Path beneath word_cache_root.
    """
    cache_name = SHARED_WORD_CACHE_NAMES.get(source_language, source_language or "source")
    return word_cache_root / f"{cache_name}{WORD_CACHE_SUMMARY_FILE_SUFFIX}"


def get_build_status_path(word_cache_root: Path, source_language: str) -> Path:
    """Resolve the per-language progress JSON polled by the Translation UI.

    What: Builds the shared language-name filename with the build-status suffix.
    Why included: Long background builds need observable phase/count progress.
    Where used: save_build_status() and load_build_status().
    Returns: Status Path beneath word_cache_root.
    """
    cache_name = SHARED_WORD_CACHE_NAMES.get(source_language, source_language or "source")
    return word_cache_root / f"{cache_name}{BUILD_STATUS_FILE_SUFFIX}"


def get_build_lock_path(word_cache_root: Path, source_language: str) -> Path:
    """Resolve the per-language lock-file path for dictionary builds.

    What: Builds a stable shared-language lock filename.
    Why included: UI and headless builders must not scan/enrich the same language concurrently.
    Where used: Lock acquisition, inspection, and release functions.
    Returns: Lock Path beneath word_cache_root.
    """
    cache_name = SHARED_WORD_CACHE_NAMES.get(source_language, source_language or "source")
    return word_cache_root / f"{cache_name}{BUILD_LOCK_FILE_SUFFIX}"


def get_word_directory_cache_key(source: dict[str, object]) -> str:
    """Choose an in-memory dictionary key shared across same-language Bible sources.

    What: Uses canonical Greek/Hebrew names when language is recognized, else source ID.
    Why included: Per-language dictionaries are reused by all matching source caches.
    Where used: Translation tab's word_directory_cache and build ownership state.
    Returns: Stable language or source identifier string.
    """
    source_language = str(source.get("language_code") or "")
    return SHARED_WORD_CACHE_NAMES.get(source_language, str(source.get("id") or "source"))


def read_json_payload(path: Path) -> dict[str, object]:
    """What: Read a JSON object from the target or its sibling temporary file.

    Why included: A completed temporary write can recover valid cache/status data after interruption.
    Where used: Word cache, lock, status, and summary readers.
    Returns: Decoded mapping, or an empty mapping when neither file is valid.
    """
    candidate_paths = [path, path.with_suffix(path.suffix + ".tmp")]
    for candidate_path in candidate_paths:
        if not candidate_path.exists():
            continue
        try:
            payload = json.loads(candidate_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict):
            if candidate_path != path:
                try:
                    write_json_payload(path, payload)
                except Exception:
                    pass
            return payload
    return {}


def write_json_payload(path: Path, payload: dict[str, object], *, allow_inplace_fallback: bool = False) -> None:
    """Write UTF-8 JSON atomically through a sibling temporary file.

    What: Creates the parent folder, writes formatted JSON, and replaces the destination;
    optional fallback writes in place when Windows holds the file open.
    Why included: Cache/status files should not be left partially serialized on interruption.
    Where used: Word directories, summaries, progress files, and lock files.
    Returns: None; raises write/replace failures unless the enabled fallback succeeds.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized_payload = json.dumps(payload, ensure_ascii=False, indent=2)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary_path.write_text(serialized_payload, encoding="utf-8")
        temporary_path.replace(path)
    except PermissionError:
        if not allow_inplace_fallback:
            raise
        path.write_text(serialized_payload, encoding="utf-8")


def save_build_status(
    word_cache_root: Path,
    source_language: str,
    phase: str,
    current: int,
    total: int,
    *,
    active: bool,
) -> None:
    """Persist one phase/count update for a language dictionary build.

    What: Writes language, normalized phase, current/total counts, active flag, and timestamp.
    Why included: The Translation tab polls status while cache work runs asynchronously.
    Where used: Base scan, dictionary enrichment, and lock/build workflows.
    Returns: None; uses in-place fallback to tolerate transient Windows locks.
    """
    status_path = get_build_status_path(word_cache_root, source_language)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "language_code": source_language,
        "language_name": SHARED_WORD_CACHE_NAMES.get(source_language, source_language).title(),
        "phase": normalize_text(phase),
        "current": max(0, int(current)),
        "total": max(0, int(total)),
        "active": bool(active),
        "updated_at": time.time(),
    }
    write_json_payload(status_path, payload, allow_inplace_fallback=True)


def load_build_lock(word_cache_root: Path, source_language: str) -> dict[str, object]:
    """What: Read the lock payload for one language, returning empty state if absent.

    Why included: Callers need owner/PID details before deciding whether a build can start.
    Where used: Build active checks, acquisition, and release.
    Returns: Lock mapping or empty dictionary.
    """
    lock_path = get_build_lock_path(word_cache_root, source_language)
    if not lock_path.exists() and not lock_path.with_suffix(lock_path.suffix + ".tmp").exists():
        return {}
    return read_json_payload(lock_path)


def process_is_running(process_id: int) -> bool:
    """What: Check whether a process ID still refers to a live process.

    Why included: Stale lock files must be distinguished from active workers.
    Where used: build_lock_is_active() and acquire_build_lock().
    Returns: True when the process appears active; False for invalid or exited IDs.
    """
    if process_id <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        process_handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, process_id)
        if not process_handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(process_handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            ctypes.windll.kernel32.CloseHandle(process_handle)
    try:
        os.kill(process_id, 0)
    except OSError:
        return False
    return True


def build_lock_is_active(word_cache_root: Path, source_language: str) -> bool:
    """What: Determine whether a language lock represents an active process.

    Why included: UI should not launch duplicate deep-detail work while another process owns it.
    Where used: Translation tab's Strong's update control.
    Returns: True when the lock PID is currently running.
    """
    payload = load_build_lock(word_cache_root, source_language)
    process_id = int(payload.get("pid") or 0)
    created_at = float(payload.get("created_at") or 0.0)
    if process_id > 0 and process_is_running(process_id):
        return True
    if created_at > 0.0 and (time.time() - created_at) <= BUILD_LOCK_STALE_SECONDS:
        return False
    return False


def acquire_build_lock(word_cache_root: Path, source_language: str, owner_name: str) -> tuple[bool, dict[str, object]]:
    """Claim the per-language build lock unless another live process owns it.

    What: Reads prior owner state, removes stale locks, and atomically writes current PID/name/time.
    Why included: UI and headless builders must not duplicate expensive cache builds.
    Where used: run_with_build_lock() and local Strong's enrichment workers.
    Returns: Acquired flag and the active/new owner payload.
    """
    lock_path = get_build_lock_path(word_cache_root, source_language)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    existing_payload = load_build_lock(word_cache_root, source_language)
    existing_pid = int(existing_payload.get("pid") or 0)
    if existing_pid > 0 and process_is_running(existing_pid):
        return False, existing_payload
    if existing_payload:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            return False, existing_payload
    payload = {
        "language_code": source_language,
        "owner_name": normalize_text(owner_name),
        "pid": os.getpid(),
        "created_at": time.time(),
    }
    write_json_payload(lock_path, payload)
    return True, payload


def release_build_lock(word_cache_root: Path, source_language: str, owner_payload: dict[str, object] | None) -> None:
    """What: Remove the language lock only when this process still owns it.

    Why included: A stale worker must not delete a newer process's lock after replacement.
    Where used: run_with_build_lock() and the deep-detail worker finalizer.
    Returns: None; missing/foreign locks are left unchanged.
    """
    lock_path = get_build_lock_path(word_cache_root, source_language)
    if not lock_path.exists():
        return
    current_payload = load_build_lock(word_cache_root, source_language)
    if owner_payload and current_payload.get("pid") != owner_payload.get("pid"):
        return
    try:
        lock_path.unlink(missing_ok=True)
    except Exception:
        return


def run_with_build_lock(word_cache_root: Path, source_language: str, owner_name: str, work):
    """Run one callable while holding the per-language dictionary-build lock.

    What: Acquires ownership, invokes work(), and releases the same owner payload in finally.
    Why included: Failed work must not leave an active lock that blocks later retries.
    Where used: Headless or UI language-wide build operations.
    Returns: work() result; raises RuntimeError if another process owns the lock.
    """
    acquired, lock_payload = acquire_build_lock(word_cache_root, source_language, owner_name)
    if not acquired:
        owner_label = normalize_text(str(lock_payload.get("owner_name") or "another process"))
        raise RuntimeError(f"A background build is already running via {owner_label}.")
    try:
        return work()
    finally:
        release_build_lock(word_cache_root, source_language, lock_payload)


def load_build_status(word_cache_root: Path, source_language: str) -> dict[str, object]:
    """What: Load the current progress snapshot for one language build.

    Why included: The UI must distinguish idle, active, and completed cache work.
    Where used: Translation status polling and summary refresh.
    Returns: Status mapping, or empty dictionary if no status file exists.
    """
    status_path = get_build_status_path(word_cache_root, source_language)
    if not status_path.exists() and not status_path.with_suffix(status_path.suffix + ".tmp").exists():
        return {}
    return read_json_payload(status_path)


def load_language_cache_summary(word_cache_root: Path, source_language: str) -> dict[str, object]:
    """What: Load compact aggregate word-cache counts for one language.

    Why included: UI summaries should avoid deserializing the complete word dictionary.
    Where used: Translation tab status and progress presentation.
    Returns: Summary mapping, or empty dictionary when absent/invalid.
    """
    return read_json_payload(get_word_cache_summary_path(word_cache_root, source_language))


def build_language_cache_summary(words: dict[str, dict[str, str]], metadata: dict[str, object]) -> dict[str, object]:
    """What: Count cached words, completed translations, details, and Strong's mappings.

    Why included: Progress UI needs compact health/completeness indicators.
    Where used: save_word_directory() when writing the summary file.
    Returns: Counts mapping plus supplied cache metadata.
    """
    blank_any = sum(
        1
        for entry in words.values()
        if not str((entry or {}).get("eng") or "").strip() or not str((entry or {}).get("afr") or "").strip()
    )
    return {
        "total_words": len(words),
        "completed_base": len(words) - blank_any,
        "blank_any": blank_any,
        "detail_complete": sum(1 for entry in words.values() if bool((entry or {}).get("detail_complete"))),
        "strongs_mapped": sum(1 for entry in words.values() if str((entry or {}).get("strongs") or "").strip()),
        "multiple_english": sum(1 for entry in words.values() if len(normalize_meaning_values((entry or {}).get("eng"))) > 1),
        "multiple_afrikaans": sum(1 for entry in words.values() if len(normalize_meaning_values((entry or {}).get("afr"))) > 1),
        "metadata": metadata,
    }


def normalize_meaning_values(value) -> list[str]:
    """What: Split/normalize meaning values and remove case-insensitive duplicates.

    Why included: Entries may store meanings as a joined string or a sequence.
    Where used: Cache summaries, merging, Strong's enrichment, and row construction.
    Returns: Ordered unique meaning strings preserving the first display spelling.
    """
    raw_values: list[str] = []
    if isinstance(value, str):
        raw_values.extend(part.strip() for part in value.split(MEANING_SEPARATOR))
    elif isinstance(value, (list, tuple, set)):
        raw_values.extend(str(item or "").strip() for item in value)
    normalized_values: list[str] = []
    seen_values: set[str] = set()
    for raw_value in raw_values:
        normalized_value = normalize_text(raw_value)
        if not normalized_value:
            continue
        lookup_value = normalized_value.casefold()
        if lookup_value in seen_values:
            continue
        seen_values.add(lookup_value)
        normalized_values.append(normalized_value)
    return normalized_values


def merge_word_entry(target_entry: dict[str, str], incoming_entry: dict[str, str]) -> dict[str, str]:
    """What: Merge two records while preserving the more complete values.

    Why included: Shared/legacy cache files can overlap but contain different metadata or senses.
    Where used: Canonicalization and legacy word-cache migration.
    Returns: New merged entry mapping; inputs are not mutated.
    """
    merged_entry = {
        "lemma": str(target_entry.get("lemma") or "").strip(),
        "strongs": str(target_entry.get("strongs") or "").strip(),
        "strongs_checked": bool(target_entry.get("strongs_checked")),
        "afr": str(target_entry.get("afr") or "").strip(),
        "eng": str(target_entry.get("eng") or "").strip(),
        "detail_complete": bool(target_entry.get("detail_complete")),
    }
    incoming_lemma = str(incoming_entry.get("lemma") or "").strip()
    incoming_strongs = str(incoming_entry.get("strongs") or "").strip()
    incoming_afr = str(incoming_entry.get("afr") or "").strip()
    incoming_eng = str(incoming_entry.get("eng") or "").strip()
    if incoming_lemma and not merged_entry["lemma"]:
        merged_entry["lemma"] = incoming_lemma
    if incoming_strongs and not merged_entry["strongs"]:
        merged_entry["strongs"] = incoming_strongs
    if bool(incoming_entry.get("strongs_checked")):
        merged_entry["strongs_checked"] = True
    if incoming_afr and len(incoming_afr) > len(merged_entry["afr"]):
        merged_entry["afr"] = incoming_afr
    if incoming_eng and len(incoming_eng) > len(merged_entry["eng"]):
        merged_entry["eng"] = incoming_eng
    if bool(incoming_entry.get("detail_complete")):
        merged_entry["detail_complete"] = True
    return merged_entry


def canonicalize_word_directory(words: dict[str, dict[str, str]], source_language: str) -> dict[str, dict[str, str]]:
    """What: Normalize word keys, filter foreign-script tokens, and merge collisions.

    Why included: Persisted caches need one canonical key per source-language word.
    Where used: save_word_directory() before serialization.
    Returns: Canonical word-to-entry mapping.
    """
    canonical_words: dict[str, dict[str, str]] = {}
    for raw_word, entry in words.items():
        normalized_word = normalize_word_key(str(raw_word), source_language)
        if not normalized_word or not token_matches_source_language(normalized_word, source_language):
            continue
        canonical_words[normalized_word] = merge_word_entry(canonical_words.get(normalized_word, {}), entry or {})
    return canonical_words


def load_words_from_cache_file(cache_path: Path, source_language: str) -> dict[str, dict[str, str]]:
    """What: Read and normalize the words mapping from one cache file.

    Why included: Cache formats may be incomplete or contain noncanonical keys.
    Where used: load_word_directory() for shared and legacy caches.
    Returns: Normalized word entries, or empty mapping when file/payload is unavailable.
    """
    if not cache_path.exists() and not cache_path.with_suffix(cache_path.suffix + ".tmp").exists():
        return {}
    payload = read_json_payload(cache_path)
    words = payload.get("words") if isinstance(payload, dict) else None
    if not isinstance(words, dict):
        return {}
    normalized_words: dict[str, dict[str, str]] = {}
    for word, entry in words.items():
        if not isinstance(entry, dict):
            continue
        normalized_word = normalize_word_key(str(word), source_language)
        if not normalized_word:
            continue
        normalized_words[normalized_word] = merge_word_entry({}, entry)
    return normalized_words


def load_word_cache_metadata(word_cache_root: Path, source: dict[str, object]) -> dict[str, object]:
    """What: Read metadata from the cache file selected for a source.

    Why included: Build completeness and source coverage determine reuse vs rebuild.
    Where used: Cache-matching and language dictionary build workflows.
    Returns: Metadata mapping, or empty mapping when absent/invalid.
    """
    cache_path = get_word_cache_path(word_cache_root, source)
    if not cache_path.exists() and not cache_path.with_suffix(cache_path.suffix + ".tmp").exists():
        return {}
    payload = read_json_payload(cache_path)
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    return metadata if isinstance(metadata, dict) else {}


def build_word_cache_metadata(
    sources: list[dict[str, object]],
    base_complete: bool,
    deep_complete: bool,
    detail_build_version: int = 0,
) -> dict[str, object]:
    """What: Create language scope, source IDs, completion flags, and detail version metadata.

    Why included: Cached dictionaries must record exactly which sources/build schema they cover.
    Where used: Base/deep build completion and word-cache saving.
    Returns: JSON-serializable metadata mapping.
    """
    language_code = str(sources[0].get("language_code") or "") if sources else ""
    source_ids = sorted({str(source.get("id") or "") for source in sources if isinstance(source, dict) and str(source.get("id") or "")})
    return {
        "dictionary_scope": "language",
        "language_code": language_code,
        "source_ids": source_ids,
        "base_complete": bool(base_complete),
        "deep_complete": bool(deep_complete),
        "detail_build_version": max(0, int(detail_build_version)),
    }


def word_cache_matches_sources(metadata: dict[str, object], sources: list[dict[str, object]]) -> bool:
    """Check whether cache metadata covers the exact current source-ID set.

    What: Validates source_ids metadata and compares its IDs with the requested sources.
    Why included: A shared language dictionary can be reused only when its coverage matches.
    Where used: build_full_word_directory() to select resume vs full scan behavior.
    Returns: True for an exact source-set match; False for absent/invalid metadata.
    """
    if not isinstance(metadata, dict) or not sources:
        return False
    metadata_ids = metadata.get("source_ids")
    if not isinstance(metadata_ids, list):
        return False
    expected_ids = {str(source.get("id") or "") for source in sources if isinstance(source, dict)}
    return expected_ids == {str(item or "") for item in metadata_ids}


def get_legacy_word_cache_paths(word_cache_root: Path, source: dict[str, object]) -> list[Path]:
    """What: Find source-specific/older cache files compatible with the source language.

    Why included: Upgraded app versions migrate older per-source files into shared language caches.
    Where used: load_word_directory() and cleanup_legacy_word_cache_files().
    Returns: Ordered unique legacy cache paths, excluding the canonical shared cache.
    """
    source_language = str(source.get("language_code") or "")
    shared_cache_path = get_word_cache_path(word_cache_root, source)
    legacy_paths: list[Path] = []
    seen_paths: set[Path] = set()
    source_specific_path = word_cache_root / f"{str(source.get('id') or 'source')}{WORD_CACHE_FILE_SUFFIX}"
    if source_specific_path != shared_cache_path and source_specific_path.exists():
        legacy_paths.append(source_specific_path)
        seen_paths.add(source_specific_path)
    if not word_cache_root.exists():
        return legacy_paths
    for cache_path in sorted(word_cache_root.glob(f"*{WORD_CACHE_FILE_SUFFIX}"), key=lambda item: item.name.casefold()):
        if cache_path == shared_cache_path or cache_path in seen_paths:
            continue
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(payload.get("language_code") or "") != source_language:
            continue
        legacy_paths.append(cache_path)
        seen_paths.add(cache_path)
    return legacy_paths


def cleanup_legacy_word_cache_files(word_cache_root: Path, source: dict[str, object]) -> None:
    """What: Remove legacy cache files after their entries have been merged into the shared cache.

    Why included: Avoids stale duplicates and repeated merge work on later loads.
    Where used: save_word_directory() after a successful canonical write.
    Returns: None; ignores individual unlink failures.
    """
    for legacy_path in get_legacy_word_cache_paths(word_cache_root, source):
        try:
            legacy_path.unlink(missing_ok=True)
        except Exception:
            continue


def load_word_directory(word_cache_root: Path, source: dict[str, object]) -> dict[str, dict[str, str]]:
    """Load the shared language cache and merge compatible legacy source caches.

    What: Reads the canonical cache, then merges each matching legacy entry by word key.
    Why included: Existing installations may contain per-source caches from older formats.
    Where used: Preview lookup, deep enrichment, and full dictionary builds.
    Returns: Combined normalized word directory.
    """
    source_language = str(source.get("language_code") or "")
    merged_words = load_words_from_cache_file(get_word_cache_path(word_cache_root, source), source_language)
    for legacy_path in get_legacy_word_cache_paths(word_cache_root, source):
        for word, entry in load_words_from_cache_file(legacy_path, source_language).items():
            merged_words[word] = merge_word_entry(merged_words.get(word, {}), entry)
    return merged_words


def word_directory_is_complete(words: dict[str, dict[str, str]]) -> bool:
    """What: Check that a nonempty word directory has both base languages for every word.

    Why included: Build resume logic must distinguish complete translation coverage from partial data.
    Where used: Metadata generation and full language cache builds.
    Returns: True only when every entry has nonblank Afrikaans and English values.
    """
    if not words:
        return False
    for entry in words.values():
        if not isinstance(entry, dict):
            return False
        if not str(entry.get("afr") or "").strip():
            return False
        if not str(entry.get("eng") or "").strip():
            return False
    return True


def save_word_directory(word_cache_root: Path, source: dict[str, object], words: dict[str, dict[str, str]], metadata: dict[str, object] | None = None) -> None:
    """Canonicalize and persist the shared dictionary, metadata, and count summary.

    What: Normalizes keys/entries, serializes meaning arrays, writes the main cache and
    summary atomically, then removes compatible legacy files.
    Why included: All consumers need a consistent language-wide cache with explicit completeness.
    Where used: Full builds, deep enrichment, and incremental dictionary updates.
    Returns: None; writes JSON files under word_cache_root.
    """
    cache_path = get_word_cache_path(word_cache_root, source)
    source_language = str(source.get("language_code") or "")
    canonical_words = canonicalize_word_directory(words, source_language)
    if metadata is None:
        metadata = load_word_cache_metadata(word_cache_root, source)
    payload = {
        "source_name": f"{SHARED_WORD_CACHE_NAMES.get(source_language, str(source.get('display_name') or 'Bible')).title()} Word Dictionary",
        "language_code": source_language,
        "metadata": metadata,
        "words": {
            word: {
                "lemma": str(entry.get("lemma") or ""),
                "strongs": str(entry.get("strongs") or ""),
                "strongs_checked": bool(entry.get("strongs_checked")),
                "afr": entry.get("afr", ""),
                "eng": entry.get("eng", ""),
                "afr_meanings": normalize_meaning_values(entry.get("afr")),
                "eng_meanings": normalize_meaning_values(entry.get("eng")),
                "detail_complete": bool(entry.get("detail_complete")),
            }
            for word, entry in sorted(canonical_words.items())
        },
    }
    write_json_payload(cache_path, payload)
    write_json_payload(
        get_word_cache_summary_path(word_cache_root, source_language),
        build_language_cache_summary(canonical_words, metadata),
    )
    cleanup_legacy_word_cache_files(word_cache_root, source)


def deduplicate_meanings(values: list[str]) -> list[str]:
    """What: Normalize a meaning sequence and deduplicate it case-insensitively.

    Why included: Base translations and Strong's senses may overlap or repeat.
    Where used: Strong's candidate/meaning merging.
    Returns: Ordered unique normalized meaning strings.
    """
    ordered_values: list[str] = []
    seen_values: set[str] = set()
    for value in values:
        normalized_value = normalize_text(value)
        if not normalized_value:
            continue
        lookup_value = normalized_value.casefold()
        if lookup_value in seen_values:
            continue
        seen_values.add(lookup_value)
        ordered_values.append(normalized_value)
    return ordered_values


def enrich_word_directory_meanings(
    source: dict[str, object],
    word_cache_root: Path,
    words_to_enrich: list[str],
    existing_directory: dict[str, dict[str, str]] | None = None,
    include_partials: bool = True,
    source_group: list[dict[str, object]] | None = None,
    mark_deep_complete: bool = False,
    force_refresh: bool = False,
    report_progress=None,
) -> dict[str, dict[str, str]]:
    """Enrich requested words with local Strong's data and persist completion metadata.

    What: Migrates stale detail metadata, filters pending words, applies dictionary matches,
    reports progress, and saves merged entries/summary when needed.
    Why included: Base translation values remain intact while deeper lexicon senses are
    incrementally added offline.
    Where used: UI-triggered deep detail and full language dictionary builds.
    Returns: Updated word directory mapping.
    """
    source_language = str(source.get("language_code") or "")
    group_sources = [entry for entry in (source_group or [source]) if isinstance(entry, dict)] or [source]
    all_words = dict(existing_directory or load_word_directory(word_cache_root, source))
    existing_metadata = load_word_cache_metadata(word_cache_root, source)
    saved_detail_build_version = int(existing_metadata.get("detail_build_version") or 0)
    if saved_detail_build_version < DETAIL_BUILD_VERSION:
        for entry in all_words.values():
            if isinstance(entry, dict) and bool(entry.get("detail_complete")) and len(normalize_meaning_values(entry.get("eng"))) <= 1:
                entry["detail_complete"] = False
            if source_language == "el" and isinstance(entry, dict) and not str(entry.get("strongs") or "").strip():
                entry["strongs_checked"] = False
        migration_metadata = build_word_cache_metadata(
            group_sources,
            base_complete=True,
            deep_complete=False,
            detail_build_version=DETAIL_BUILD_VERSION,
        )
        save_word_directory(word_cache_root, source, all_words, metadata=migration_metadata)
    pending_words: list[str] = []
    for word in dict.fromkeys(words_to_enrich):
        if not token_matches_source_language(word, source_language):
            continue
        entry = all_words.get(word) or {}
        if (str(entry.get("strongs") or "").strip() or bool(entry.get("strongs_checked"))) and not force_refresh:
            continue
        if not include_partials and str(entry.get("eng") or "").strip() and str(entry.get("afr") or "").strip():
            continue
        pending_words.append(word)
    total_pending = len(pending_words)
    save_build_status(
        word_cache_root,
        source_language,
        "Preparing Strong's meanings",
        0,
        max(1, total_pending),
        active=True,
    )
    for index, word in enumerate(pending_words, start=1):
        entry = all_words.setdefault(word, {"afr": "", "eng": "", "detail_complete": False})
        strongs_applied = apply_strongs_entry(word_cache_root, word, source_language, entry)
        entry["strongs_checked"] = True
        english_meanings = normalize_meaning_values(entry.get("eng"))
        afrikaans_meanings = normalize_meaning_values(entry.get("afr"))
        entry["detail_complete"] = (
            strongs_applied
            and len(english_meanings) > 1
            and len(afrikaans_meanings) > 1
        )
        if index == 1 or index % STATUS_UPDATE_INTERVAL == 0 or index == total_pending:
            if callable(report_progress):
                report_progress(index, max(1, total_pending), "Checking Strong's matches")
            save_build_status(word_cache_root, source_language, "Checking Strong's matches", index, max(1, total_pending), active=True)
    # Reflect true completion in metadata regardless of which caller ran this pass,
    # so small per-verse lookups that finish the last remaining words still mark it complete.
    all_entries_detail_complete = bool(all_words) and all(bool((entry or {}).get("detail_complete")) for entry in all_words.values())
    if total_pending > 0 or mark_deep_complete or all_entries_detail_complete:
        final_metadata = build_word_cache_metadata(
            group_sources,
            base_complete=True,
            deep_complete=all_entries_detail_complete,
            detail_build_version=DETAIL_BUILD_VERSION,
        )
        save_word_directory(word_cache_root, source, all_words, metadata=final_metadata)
    mapped_words = sum(1 for entry in all_words.values() if str((entry or {}).get("strongs") or "").strip())
    save_build_status(word_cache_root, source_language, "Strong's mappings saved", mapped_words, len(all_words), active=False)
    return all_words


def build_full_word_directory(
    source: dict[str, object],
    word_cache_root: Path,
    report_progress=None,
    source_group: list[dict[str, object]] | None = None,
) -> dict[str, dict[str, str]]:
    """Build or resume the shared word dictionary for one original language.

    What: Reuses compatible cache entries, scans normalized source book caches when needed,
    applies available lexicon mappings, saves metadata/summary, and reports progress.
    Why included: Translation lookup requires a local word directory independent of network access.
    Where used: Headless build scripts and background build workflows.
    Returns: Completed/partially completed word-to-meaning mapping.
    """
    source_language = str(source.get("language_code") or "")
    save_build_status(word_cache_root, source_language, "Preparing base meanings", 0, 1, active=True)
    group_sources = [entry for entry in (source_group or [source]) if isinstance(entry, dict)] or [source]
    existing_words = load_word_directory(word_cache_root, source)
    existing_metadata = load_word_cache_metadata(word_cache_root, source)
    existing_detail_build_version = int(existing_metadata.get("detail_build_version") or 0)
    existing_words = {
        word: entry
        for word, entry in existing_words.items()
        if token_matches_source_language(word, source_language)
    }
    resume_from_saved_cache = bool(existing_words) and word_cache_matches_sources(existing_metadata, group_sources)
    if existing_words:
        seeded_metadata = build_word_cache_metadata(
            group_sources,
            base_complete=word_directory_is_complete(existing_words),
            deep_complete=all(bool((entry or {}).get("detail_complete")) for entry in existing_words.values()),
            detail_build_version=existing_detail_build_version,
        )
        save_build_status(word_cache_root, source_language, "Loading installed dictionary", len(existing_words), len(existing_words), active=True)
        if callable(report_progress):
            report_progress(len(existing_words), len(existing_words), "Loading installed dictionary")
        save_word_directory(word_cache_root, source, existing_words, metadata=seeded_metadata)
    all_words: dict[str, dict[str, str]] = dict(existing_words)
    if not resume_from_saved_cache:
        catalog_entries: list[dict[str, object]] = []
        for grouped_source in group_sources:
            catalog_entries.extend(build_source_book_catalog(grouped_source))
        total_books = max(1, len(catalog_entries))
        seen_words: set[str] = set(all_words)
        for book_index, entry in enumerate(catalog_entries, start=1):
            save_build_status(word_cache_root, source_language, "Scanning cached verses", book_index, total_books, active=True)
            if callable(report_progress):
                report_progress(book_index, total_books, "Scanning cached verses")
            book_path = entry.get("book_path")
            if not isinstance(book_path, Path):
                continue
            chapters = load_book_chapters(book_path)
            for chapter_verses in chapters.values():
                for verse_text in chapter_verses.values():
                    for token in tokenize_original_words(verse_text):
                        normalized_token = normalize_word_key(token, source_language)
                        if not token_matches_source_language(normalized_token, source_language):
                            continue
                        if normalized_token in seen_words:
                            continue
                        seen_words.add(normalized_token)
                        all_words[normalized_token] = {"lemma": "", "afr": "", "eng": "", "detail_complete": False}
        total_known_words = max(1, len(all_words))
        save_build_status(word_cache_root, source_language, "Applying installed dictionary", 0, total_known_words, active=True)
        for word_index, (word, entry) in enumerate(all_words.items(), start=1):
            apply_strongs_entry(word_cache_root, word, source_language, entry)
            if callable(report_progress):
                report_progress(word_index, total_known_words, "Applying installed dictionary")
            save_build_status(word_cache_root, source_language, "Applying installed dictionary", word_index, total_known_words, active=True)
    else:
        total_known_words = max(1, len(all_words))
        completed_words = sum(
            1
            for entry in all_words.values()
            if str((entry or {}).get("eng") or "").strip() and str((entry or {}).get("afr") or "").strip()
        )
        save_build_status(word_cache_root, source_language, "Resuming saved dictionary", completed_words, total_known_words, active=True)
        if callable(report_progress):
            report_progress(completed_words, total_known_words, "Resuming saved dictionary")
    dictionary_phase_metadata = build_word_cache_metadata(
        group_sources,
        base_complete=word_directory_is_complete(all_words),
        deep_complete=all(bool((entry or {}).get("detail_complete")) for entry in all_words.values()),
        detail_build_version=existing_detail_build_version,
    )
    save_word_directory(word_cache_root, source, all_words, metadata=dictionary_phase_metadata)
    final_metadata = build_word_cache_metadata(
        group_sources,
        base_complete=word_directory_is_complete(all_words),
        deep_complete=all(bool((entry or {}).get("detail_complete")) for entry in all_words.values()),
        detail_build_version=existing_detail_build_version,
    )
    save_word_directory(word_cache_root, source, all_words, metadata=final_metadata)
    save_build_status(word_cache_root, source_language, "Local word directory updated", len(all_words), len(all_words), active=False)
    return all_words


def build_translation_tab(tab_frame: tk.Frame, context: dict[str, object]) -> None:
    """Build the original-language word lookup and local cache controls.

    What: Discovers Greek/Hebrew sources, provides book/chapter/verse selectors, renders
    word meanings, and starts background cache/deep-detail work with status updates.
    Why included: Translation lookup is backed by normalized Bible caches and local
    language dictionaries, not network translation requests.
    Where used: main.create_tabs_content() for the Translation notebook page.
    Returns: None; uses app context for base paths, worker dispatch, styling, and tab activity.
    """
    apply_layout_style = context["apply_layout_style"]

    def style_widgets(widgets, label_widgets=(), input_widgets=()) -> None:
        """What: Register generated Translation widgets and apply shared application styling.

        Why included: Dynamic table rows need to follow theme/font changes and be removed from
        registries when destroyed.
        Where used: Initial controls and rebuilt word-meaning table cells.
        Returns: None; updates context registries and widget styles.
        """
        all_widgets = list(dict.fromkeys([*widgets, *label_widgets, *input_widgets]))
        for key, group in (("tab_widgets", all_widgets), ("tab_labels", label_widgets), ("tab_inputs", input_widgets)):
            registry = context.get(key)
            if isinstance(registry, list):
                for widget in group:
                    if widget not in registry:
                        registry.append(widget)
                        widget.bind("<Destroy>", lambda event, current=widget, items=registry: items.remove(current) if event.widget is current and current in items else None, add="+")
        apply_layout_style(all_widgets, label_widgets=label_widgets, input_widgets=input_widgets)

    get_app_base_path = context.get("get_app_base_path")
    start_background_task = context.get("start_background_task")
    is_tab_active = context.get("is_tab_active")

    if callable(get_app_base_path):
        try:
            app_base_path = Path(get_app_base_path())
        except Exception:
            app_base_path = default_get_app_base_path()
    else:
        app_base_path = default_get_app_base_path()

    cache_root = app_base_path / "Bibles" / ".read_bible_cache"
    word_cache_root = get_app_data_path(app_base_path) / "translation_word_cache"
    word_cache_root.mkdir(parents=True, exist_ok=True)

    state: dict[str, object] = {
        "sources": [],
        "source_by_display": {},
        "book_catalog_cache": {},
        "word_directory_cache": {},
        "active_directory_loads": set(),
        "active_deep_detail_source_id": None,
        "status_summary_cache": {},
        "status_summary_loaded_at": {},
        "word_directory_status_updated_at": {},
    }

    tab_frame.columnconfigure(0, weight=1, minsize=1260)
    tab_frame.rowconfigure(0, weight=0)
    tab_frame.rowconfigure(1, weight=1, minsize=520)
    tab_frame.rowconfigure(2, weight=0)

    controls_frame = tk.Frame(tab_frame, bd=1, relief="solid", padx=12, pady=12)
    controls_frame.grid(row=0, column=0, sticky="ew", padx=4, pady=(12, 8))
    for column_index in range(4):
        controls_frame.columnconfigure(column_index, weight=1)

    source_label = tk.Label(controls_frame, text="Bible", anchor="w", justify="left")
    source_label.grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=(0, 4))

    verse_preview_var = tk.StringVar(value="")
    book_label = tk.Label(controls_frame, text="Book", anchor="w", justify="left")
    book_label.grid(row=0, column=1, sticky="ew", padx=6, pady=(0, 4))
    chapter_label = tk.Label(controls_frame, text="Chapter", anchor="w", justify="left")
    chapter_label.grid(row=0, column=2, sticky="ew", padx=6, pady=(0, 4))
    verse_label = tk.Label(controls_frame, text="Verse", anchor="w", justify="left")
    verse_label.grid(row=0, column=3, sticky="ew", padx=6, pady=(0, 4))

    source_var = tk.StringVar(value="")
    book_var = tk.StringVar(value="")
    chapter_var = tk.StringVar(value="")
    verse_var = tk.StringVar(value="")
    status_var = tk.StringVar(value="")

    source_combobox = ttk.Combobox(controls_frame, textvariable=source_var, state="readonly", style="Content.TCombobox")
    source_combobox.grid(row=1, column=0, sticky="ew", padx=(0, 6))
    book_combobox = ttk.Combobox(controls_frame, textvariable=book_var, state="readonly", style="Content.TCombobox")
    book_combobox.grid(row=1, column=1, sticky="ew", padx=6)
    chapter_combobox = ttk.Combobox(controls_frame, textvariable=chapter_var, state="readonly", width=8, style="Content.TCombobox")
    chapter_combobox.grid(row=1, column=2, sticky="ew", padx=6)
    verse_combobox = ttk.Combobox(controls_frame, textvariable=verse_var, state="readonly", width=8, style="Content.TCombobox")
    verse_combobox.grid(row=1, column=3, sticky="ew", padx=6)

    content_frame = tk.Frame(tab_frame, bd=1, relief="solid", padx=12, pady=12)
    content_frame.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 8))
    content_frame.configure(width=1260)
    content_frame.columnconfigure(0, weight=1)
    content_frame.rowconfigure(0, weight=0)
    content_frame.rowconfigure(1, weight=0)
    content_frame.rowconfigure(2, weight=1)
    content_frame.rowconfigure(3, weight=0)

    table_header = tk.Frame(content_frame)
    table_header.grid(row=0, column=0, sticky="ew")
    table_header.columnconfigure(0, weight=22, minsize=220)
    table_header.columnconfigure(1, weight=39, minsize=320)
    table_header.columnconfigure(2, weight=39, minsize=320)
    tk.Label(table_header, text="Original Word", anchor="w", justify="left").grid(row=0, column=0, sticky="ew", padx=(0, 8), pady=(0, 6))
    tk.Label(table_header, text="Afrikaans Meaning", anchor="w", justify="left").grid(row=0, column=1, sticky="ew", padx=8, pady=(0, 6))
    tk.Label(table_header, text="English Meaning", anchor="w", justify="left").grid(row=0, column=2, sticky="ew", padx=(8, 0), pady=(0, 6))

    verse_preview_label = tk.Label(
        content_frame,
        textvariable=verse_preview_var,
        anchor="w",
        justify="left",
        wraplength=1180,
        padx=8,
        pady=8,
        bd=1,
        relief="solid",
    )
    verse_preview_label.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 10))

    table_canvas = tk.Canvas(content_frame, highlightthickness=0)
    table_canvas.grid(row=2, column=0, sticky="nsew")
    table_scrollbar = tk.Scrollbar(content_frame, orient="vertical", command=table_canvas.yview)
    table_scrollbar.grid(row=2, column=1, sticky="ns")
    table_x_scrollbar = tk.Scrollbar(content_frame, orient="horizontal", command=table_canvas.xview)
    table_x_scrollbar.grid(row=3, column=0, sticky="ew", pady=(8, 0))
    table_canvas.configure(yscrollcommand=table_scrollbar.set, xscrollcommand=table_x_scrollbar.set)
    content_frame.rowconfigure(2, weight=1)
    content_frame.rowconfigure(3, weight=0)

    table_body = tk.Frame(table_canvas)
    table_body.columnconfigure(0, weight=22, minsize=220)
    table_body.columnconfigure(1, weight=39, minsize=320)
    table_body.columnconfigure(2, weight=39, minsize=320)
    table_window = table_canvas.create_window((0, 0), window=table_body, anchor="nw")

    def sync_table_scroll_region(_event=None) -> None:
        """What: Recompute the word table canvas bounds after its body changes.

        Why included: The table is rebuilt as tokens/meanings change and may exceed viewport height.
        Where used: table_body Configure binding.
        Returns: None; updates the canvas scrollregion.
        """
        table_canvas.configure(scrollregion=table_canvas.bbox("all"))

    def resize_table_body(event) -> None:
        """What: Resize the embedded table window to the visible canvas width.

        Why included: Meaning columns should use available width while preserving horizontal scroll.
        Where used: table_canvas Configure binding.
        Returns: None; updates the embedded canvas-window width.
        """
        table_canvas.itemconfigure(table_window, width=event.width)

    def bind_mousewheel_scroll(widget: tk.Widget, scroll_target) -> None:
        """What: Bind nested table widgets to scroll the shared table canvas.

        Why included: Wheel input should work over cells as well as over the canvas itself.
        Where used: Table canvas/body/scrollbar and meaning row cells.
        Returns: None; installs wheel event bindings.
        """
        def handle_mousewheel(event) -> str:
            """What: Convert Windows/Linux mouse-wheel direction into canvas scroll units.

            Why included: Platform-specific wheel events need one shared scroll behavior.
            Where used: Bindings installed by bind_mousewheel_scroll().
            Returns: "break" after scrolling or consuming invalid events.
            """
            if event.delta:
                delta = -1 if event.delta > 0 else 1
            elif getattr(event, "num", None) == 4:
                delta = -1
            elif getattr(event, "num", None) == 5:
                delta = 1
            else:
                return "break"
            try:
                scroll_target.yview_scroll(delta, "units")
            except tk.TclError:
                return "break"
            return "break"

        widget.bind("<MouseWheel>", handle_mousewheel, add="+")
        widget.bind("<Button-4>", handle_mousewheel, add="+")
        widget.bind("<Button-5>", handle_mousewheel, add="+")

    table_body.bind("<Configure>", sync_table_scroll_region)
    table_canvas.bind("<Configure>", resize_table_body)
    bind_mousewheel_scroll(table_canvas, table_canvas)
    bind_mousewheel_scroll(table_body, table_canvas)
    bind_mousewheel_scroll(table_scrollbar, table_canvas)

    def clear_table_rows() -> None:
        """What: Destroy current table cells before rendering a newly selected verse.

        Why included: Old tokens/meanings must not remain alongside new lookup results.
        Where used: render_selected_verse() before adding current rows.
        Returns: None; clears all children from table_body.
        """
        for child in table_body.winfo_children():
            child.destroy()

    def add_table_row(original_text: str, afrikaans_text: str, english_text: str) -> None:
        """What: Add one original/Afrikaans/English meaning row to the table.

        Why included: Each source token needs aligned cells for the available translations.
        Where used: render_selected_verse() for every generated meaning tuple.
        Returns: None; creates, styles, and scroll-binds three Labels.
        """
        row_index = len(table_body.winfo_children()) // 3
        row_values = (original_text, afrikaans_text, english_text)
        for column_index, value in enumerate(row_values):
            cell = tk.Label(
                table_body,
                text=value,
                anchor="w",
                justify="left",
                wraplength=360,
                padx=8,
                pady=8,
            )
            if column_index == 0:
                cell.configure(wraplength=220)
            elif column_index in {1, 2}:
                cell.configure(wraplength=380)
            cell.grid(row=row_index, column=column_index, sticky="nsew", padx=(0 if column_index == 0 else 4, 0 if column_index == 2 else 4), pady=1)
            style_widgets([cell], label_widgets=[cell])
            bind_mousewheel_scroll(cell, table_canvas)

    def set_progress(message: str, current: int | float | None = None, total: int | float | None = None) -> None:
        """What: Update the Translation status text and optional progress bar values.

        Why included: Background cache builders need to communicate phase and completion.
        Where used: Verse cache loading, Strong's enrichment, and source-selection feedback.
        Returns: None; updates status_var and available progress controls.
        """
        status_var.set(message)

    def handle_translation_tab_changed(_event=None) -> None:
        """What: Refresh source/book controls when Translation becomes the active tab.

        Why included: Cache availability can change while the user works elsewhere.
        Where used: Notebook <<NotebookTabChanged>> binding.
        Returns: None; no-ops while the tab is hidden.
        """
        if callable(is_tab_active) and is_tab_active(tab_frame):
            if not state.get("sources"):
                refresh_sources()
            update_book_options()

    def get_selected_source() -> dict[str, object] | None:
        """What: Resolve the current source-combobox label to its cached source mapping.

        Why included: Book/verse lookup requires cache path and language metadata.
        Where used: Catalog lookup, verse rendering, and background build selection.
        Returns: Selected source mapping, or None when unavailable.
        """
        selected_display_name = source_var.get().strip()
        source_map = state.get("source_by_display") or {}
        source = source_map.get(selected_display_name)
        return source if isinstance(source, dict) else None

    def get_book_catalog(source: dict[str, object]) -> list[dict[str, object]]:
        """What: Load and cache the book catalog for one selected source ID.

        Why included: Catalog parsing is repeated across book/chapter/verse updates.
        Where used: Book option refresh and selected-book resolution.
        Returns: Ordered catalog mappings.
        """
        source_id = str(source.get("id") or "")
        cached_catalog = (state.get("book_catalog_cache") or {}).get(source_id)
        if isinstance(cached_catalog, list):
            return cached_catalog
        catalog = build_source_book_catalog(source)
        state.setdefault("book_catalog_cache", {})[source_id] = catalog
        return catalog

    def get_selected_book_entry() -> dict[str, object] | None:
        """What: Match the selected book-combobox value within the current source catalog.

        Why included: Chapter/verse lookup needs the selected book cache path and counts.
        Where used: Passage option refresh and verse rendering.
        Returns: Matching book mapping, or None for invalid source/selection.
        """
        source = get_selected_source()
        if not isinstance(source, dict):
            return None
        selected_book_value = book_var.get().strip()
        for entry in get_book_catalog(source):
            if not isinstance(entry, dict):
                continue
            expected_value = f"{int(entry.get('number') or 0):02d} {str(entry.get('name') or 'Book')}"
            if expected_value == selected_book_value:
                return entry
        return None

    def get_word_directory(source: dict[str, object]) -> dict[str, dict[str, str]]:
        """What: Read the in-memory word directory associated with a source cache key.

        Why included: Verse rendering should reuse loaded meanings without rereading JSON.
        Where used: render_selected_verse() before falling back to disk cache loading.
        Returns: Cached word entries, or an empty mapping.
        """
        cache_key = get_word_directory_cache_key(source)
        cached_directory = (state.get("word_directory_cache") or {}).get(cache_key)
        return cached_directory if isinstance(cached_directory, dict) else {}

    def get_language_sources(source: dict[str, object]) -> list[dict[str, object]]:
        """What: Collect every discovered source that shares the requested language code.

        Why included: A language-wide dictionary cache records the complete source set it covers.
        Where used: Cache completeness checks and Strong's update/build requests.
        Returns: Source mappings with the same language_code.
        """
        source_language = str(source.get("language_code") or "")
        return [
            entry
            for entry in (state.get("sources") or [])
            if isinstance(entry, dict) and str(entry.get("language_code") or "") == source_language
        ]

    def dictionary_already_complete(source: dict[str, object], require_deep_complete: bool = False) -> bool:
        """What: Check cache source coverage and base/deep completion metadata.

        Why included: Avoids rebuilding a matching dictionary that already satisfies the request.
        Where used: Translation build/status decisions before scheduling work.
        Returns: True when metadata covers current language sources and required phases are complete.
        """
        metadata = load_word_cache_metadata(word_cache_root, source)
        language_sources = get_language_sources(source)
        if not word_cache_matches_sources(metadata, language_sources):
            return False
        if not bool(metadata.get("base_complete")):
            return False
        if require_deep_complete and not bool(metadata.get("deep_complete")):
            return False
        return True

    def render_selected_verse() -> None:
        """What: Load the selected verse, map its tokens to meanings, and rebuild table rows.

        Why included: The Translation view is driven by one source/book/chapter/verse selection.
        Where used: Verse selector changes and cache-load completion callbacks.
        Returns: None; starts asynchronous local-cache loading when needed and reports missing data.
        """
        clear_table_rows()
        verse_preview_var.set("")
        source = get_selected_source()
        book_entry = get_selected_book_entry()
        if not isinstance(source, dict) or not isinstance(book_entry, dict):
            return
        if not chapter_var.get().strip().isdigit() or not verse_var.get().strip().isdigit():
            return
        book_path = book_entry.get("book_path")
        if not isinstance(book_path, Path):
            return
        chapters = load_book_chapters(book_path)
        chapter_number = int(chapter_var.get())
        verse_number = int(verse_var.get())
        verse_text = str((chapters.get(chapter_number) or {}).get(verse_number) or "").strip()
        if not verse_text:
            set_progress("The selected verse is empty in this cached Bible.")
            return
        verse_preview_var.set(verse_text)
        source_language = str(source.get("language_code") or "")
        word_directory = get_word_directory(source)
        if not word_directory:
            cached_directory = load_word_directory(word_cache_root, source)
            if isinstance(cached_directory, dict) and cached_directory:
                word_directory = cached_directory
                state.setdefault("word_directory_cache", {})[get_word_directory_cache_key(source)] = word_directory
        if not word_directory:
            cache_key = get_word_directory_cache_key(source)
            active_loads = state.setdefault("active_directory_loads", set())
            if cache_key not in active_loads and callable(start_background_task):
                active_loads.add(cache_key)

                def worker(_report_progress):
                    """What: Read the language word directory from local cache storage.

                    Why included: Cache I/O should not block verse selection or table interaction.
                    Where used: Background task started when the selected source cache is not loaded.
                    Returns: Word-to-meaning mapping for the selected source language.
                    """
                    return load_word_directory(word_cache_root, source)

                def on_success(result) -> None:
                    """What: Cache the loaded dictionary and rerender the current selected verse.

                    Why included: The user may change verse while the cache is loading; render uses current controls.
                    Where used: on_success callback for the preview-cache worker.
                    Returns: None; releases the active-load marker.
                    """
                    active_loads.discard(cache_key)
                    if isinstance(result, dict):
                        state.setdefault("word_directory_cache", {})[cache_key] = result
                    render_selected_verse()

                def on_error(error: Exception) -> None:
                    """What: Release the active-load marker and report a local cache read failure.

                    Why included: Failed loads must allow a later retry and explain missing meanings.
                    Where used: on_error callback for the preview-cache worker.
                    Returns: None; updates the status line.
                    """
                    active_loads.discard(cache_key)
                    set_progress(f"Could not load the local word cache: {error}")

                start_background_task(
                    f"translation_preview_cache_{cache_key}",
                    worker,
                    on_success=on_success,
                    on_error=on_error,
                )
            set_progress(f"Loading the local {SHARED_WORD_CACHE_NAMES.get(source_language, source_language)} word cache...")

        rows = build_verse_meaning_rows(verse_text, word_directory, source_language)
        for token, afrikaans_text, english_text in rows:
            add_table_row(token, afrikaans_text, english_text)
        if not rows:
            return
        missing_words = [
            token for token, afrikaans_text, english_text in rows
            if not afrikaans_text.strip() or not english_text.strip()
        ]
        if missing_words:
            set_progress("The word directory is still building. Known meanings are shown and missing words will fill in when the build finishes.")
        else:
            set_progress(f"Showing {len(rows)} word rows for the selected verse.")

    def update_verse_options() -> None:
        """What: Populate verse choices from the selected book/chapter and clamp the current value.

        Why included: A book or chapter change can invalidate the previously selected verse.
        Where used: Chapter selection and book-option refresh.
        Returns: None; rerenders the selected verse after updating controls.
        """
        book_entry = get_selected_book_entry()
        if not isinstance(book_entry, dict) or not chapter_var.get().strip().isdigit():
            verse_combobox.configure(values=[])
            verse_var.set("")
            render_selected_verse()
            return
        verse_counts = book_entry.get("verse_counts") if isinstance(book_entry.get("verse_counts"), dict) else {}
        chapter_number = int(chapter_var.get())
        verse_count = int(verse_counts.get(chapter_number) or 0)
        verse_values = [str(number) for number in range(1, verse_count + 1)]
        verse_combobox.configure(values=verse_values)
        if verse_values:
            current_value = verse_var.get().strip()
            verse_var.set(current_value if current_value in verse_values else verse_values[0])
        else:
            verse_var.set("")
        render_selected_verse()

    def update_chapter_options() -> None:
        """What: Populate chapters for the selected book and preserve a valid chapter when possible.

        Why included: Chapter choices depend on the current source/book catalog entry.
        Where used: Book selection and source-selection updates.
        Returns: None; refreshes verse options after normalization.
        """
        book_entry = get_selected_book_entry()
        if not isinstance(book_entry, dict):
            chapter_combobox.configure(values=[])
            chapter_var.set("")
            update_verse_options()
            return
        chapter_count = int(book_entry.get("chapter_count") or 0)
        chapter_values = [str(number) for number in range(1, chapter_count + 1)]
        chapter_combobox.configure(values=chapter_values)
        if chapter_values:
            current_value = chapter_var.get().strip()
            chapter_var.set(current_value if current_value in chapter_values else chapter_values[0])
        else:
            chapter_var.set("")
        update_verse_options()

    def update_book_options() -> None:
        """What: Populate book choices for the selected source and cascade chapter/verse refreshes.

        Why included: Book catalog options differ between cached Bible sources.
        Where used: Source selection and initial source loading.
        Returns: None; clears choices when the source is unavailable.
        """
        source = get_selected_source()
        if not isinstance(source, dict):
            book_combobox.configure(values=[])
            book_var.set("")
            update_chapter_options()
            return
        book_catalog = get_book_catalog(source)
        book_values = [f"{int(entry.get('number') or 0):02d} {str(entry.get('name') or 'Book')}" for entry in book_catalog if isinstance(entry, dict)]
        book_combobox.configure(values=book_values)
        if book_values:
            current_value = book_var.get().strip()
            book_var.set(current_value if current_value in book_values else book_values[0])
        else:
            book_var.set("")
        update_chapter_options()

    def handle_deep_detail_success(result) -> None:
        """What: Cache Strong's-enriched words, rerender, and mark the local update complete.

        Why included: Background enrichment results must be reflected in the selected verse table.
        Where used: on_success callback for the Strong's detail task.
        Returns: None; clears active deep-detail state and updates progress text.
        """
        source = get_selected_source()
        state["active_deep_detail_source_id"] = None
        if isinstance(result, dict) and isinstance(source, dict):
            state.setdefault("word_directory_cache", {})[get_word_directory_cache_key(source)] = result
        render_selected_verse()
        set_progress("Local Strong's update finished.", 1, 1)

    def handle_deep_detail_error(error: Exception) -> None:
        """What: Clear active deep-detail state and expose the worker failure in status.

        Why included: Failed enrichment must not appear permanently active or silently fail.
        Where used: on_error callback for the Strong's detail task.
        Returns: None; updates the status label.
        """
        state["active_deep_detail_source_id"] = None
        set_progress(f"Local Strong's update failed: {error}")

    def ensure_strongs_meanings_for_language(source_language: str) -> None:
        """What: Schedule one locked Strong's enrichment pass for all sources of a language.

        Why included: Deep lexicon details are local, potentially expensive, and must not overlap
        another UI/headless build.
        Where used: Deep-detail/update action in the Translation tab.
        Returns: None; reports unavailable sources, locks, worker support, and task progress.
        """
        if callable(is_tab_active) and not is_tab_active(tab_frame):
            return
        source = next(
            (
                candidate
                for candidate in (state.get("sources") or [])
                if isinstance(candidate, dict) and str(candidate.get("language_code") or "") == source_language
            ),
            None,
        )
        if not isinstance(source, dict):
            set_progress(f"No cached {SHARED_WORD_CACHE_NAMES.get(source_language, source_language)} Bible source is available.")
            return
        cache_key = get_word_directory_cache_key(source)
        language_sources = get_language_sources(source)
        source_language = str(source.get("language_code") or "")
        if state.get("active_deep_detail_source_id") == cache_key:
            return
        if build_lock_is_active(word_cache_root, source_language):
            set_progress(f"A local Strong's update is already running for {SHARED_WORD_CACHE_NAMES.get(source_language, 'the selected language')}.")
            return
        if not callable(start_background_task):
            set_progress("Background task support is unavailable, so the Strong's update cannot start.")
            return

        def worker(report_progress):
            """What: Acquire the language lock, seed/load its dictionary, enrich entries, and release.

            Why included: Competing builders must not duplicate scans or overwrite one another.
            Where used: Background worker passed to start_background_task().
            Returns: Enriched word directory; always releases its owned lock in finally.
            """
            acquired, lock_payload = acquire_build_lock(word_cache_root, source_language, f"local-strongs-{SHARED_WORD_CACHE_NAMES.get(source_language, source_language)}")
            if not acquired:
                owner_label = normalize_text(str(lock_payload.get("owner_name") or "another process"))
                raise RuntimeError(f"A local update is already running via {owner_label}.")
            try:
                base_directory = load_word_directory(word_cache_root, source)
                if not base_directory:
                    base_directory = build_full_word_directory(source, word_cache_root, report_progress, source_group=language_sources)
                source_words = list(base_directory.keys())
                return enrich_word_directory_meanings(
                    source,
                    word_cache_root,
                    source_words,
                    base_directory,
                    source_group=language_sources,
                    mark_deep_complete=True,
                    report_progress=report_progress,
                )
            finally:
                release_build_lock(word_cache_root, source_language, lock_payload)

        def on_progress(current, total, phase):
            """What: Display phase and item counts reported by the Strong's worker.

            Why included: Long lexicon passes need visible progress while the tab stays responsive.
            Where used: on_progress callback for the Strong's update task.
            Returns: None; updates the status/progress controls.
            """
            set_progress(f"{phase} for the local {SHARED_WORD_CACHE_NAMES.get(str(source.get('language_code') or ''), 'selected')} dictionary", current, total)

        state["active_deep_detail_source_id"] = cache_key
        set_progress(f"Preparing local Strong's update for {SHARED_WORD_CACHE_NAMES.get(str(source.get('language_code') or ''), 'the selected language')}...")
        start_background_task(
            f"translation_strongs_update_{cache_key}",
            worker,
            on_success=handle_deep_detail_success,
            on_error=handle_deep_detail_error,
            on_progress=on_progress,
        )

    def handle_source_change(_event=None) -> None:
        """What: Refresh book/chapter/verse choices after the source selector changes.

        Why included: Each cached source can have a different book catalog and coverage.
        Where used: Source Combobox <<ComboboxSelected>> binding.
        Returns: None; does nothing while the tab is inactive.
        """
        if callable(is_tab_active) and not is_tab_active(tab_frame):
            return
        update_book_options()

    def refresh_sources() -> None:
        """What: Discover Greek/Hebrew cache sources, clean old caches, and populate selectors.

        Why included: The tab should offer only supported original-language sources and shared cache data.
        Where used: Initial tab setup and activation when source state is empty.
        Returns: None; clears selectors and reports an empty state when no source is found.
        """
        sources = discover_original_language_sources(cache_root)
        for source in sources:
            if isinstance(source, dict):
                cleanup_legacy_word_cache_files(word_cache_root, source)
        state["sources"] = sources
        source_map = {str(source.get("display_name") or "Bible"): source for source in sources if isinstance(source, dict)}
        state["source_by_display"] = source_map
        values = list(source_map.keys())
        source_combobox.configure(values=values)
        if values:
            current_value = source_var.get().strip()
            source_var.set(current_value if current_value in values else values[0])
            handle_source_change()
        else:
            source_var.set("")
            update_book_options()
            set_progress("No Greek or Hebrew cached Bible sources were found in Bibles/.read_bible_cache.")

    source_combobox.bind("<<ComboboxSelected>>", handle_source_change, add="+")
    book_combobox.bind("<<ComboboxSelected>>", lambda _event=None: update_chapter_options(), add="+")
    chapter_combobox.bind("<<ComboboxSelected>>", lambda _event=None: update_verse_options(), add="+")
    verse_combobox.bind("<<ComboboxSelected>>", lambda _event=None: render_selected_verse(), add="+")

    style_widgets(
        [tab_frame, controls_frame, content_frame, table_header, table_body, table_canvas, table_scrollbar, table_x_scrollbar],
        label_widgets=[source_label, book_label, chapter_label, verse_label, verse_preview_label, *table_header.winfo_children()],
        input_widgets=[source_combobox, book_combobox, chapter_combobox, verse_combobox],
    )
    notebook = context.get("notebook")
    if isinstance(notebook, ttk.Notebook):
        notebook.bind("<<NotebookTabChanged>>", handle_translation_tab_changed, add="+")
    if not callable(is_tab_active) or is_tab_active(tab_frame):
        refresh_sources()
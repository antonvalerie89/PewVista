"""Regression checks for bilingual spelling and non-destructive Tk integration."""

import time
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from grammar_check import GrammarEngine, GrammarIssue, _character_offset
from spell_check import GRAMMAR_TAG, SPELL_TAG, SpellCheckController, SpellingEngine


class SpellCheckTests(unittest.TestCase):
    """What: Exercise real dictionaries and editable-field behavior.

    Why included: Spelling assistance must not corrupt content, flags, or read-only output.
    Where used: unittest discovery for tests/test_spell_check.py.
    """

    @classmethod
    def setUpClass(cls) -> None:
        """What: Load the real dictionaries once for the focused tests.

        Why included: Dictionary loading is shared rather than repeated for every assertion.
        Where used: unittest class setup.
        Returns: None.
        """
        cls.engine = SpellingEngine()

    def setUp(self) -> None:
        """What: Create an isolated Tk root and spelling controller.

        Why included: Every test needs independent events, timers, and widgets.
        Where used: unittest per-test setup.
        Returns: None.
        """
        self.root = tk.Tk()
        self.root.withdraw()
        self.controller = SpellCheckController(self.root)
        self.controller.engine = self.engine
        self.controller.grammar_engine.check = lambda text: []

    def tearDown(self) -> None:
        """What: Stop the spelling worker and destroy the isolated Tk window.

        Why included: Tests must not leave callbacks or windows behind.
        Where used: unittest per-test cleanup.
        Returns: None.
        """
        self.controller.close()
        self.controller.executor.shutdown(wait=True)
        self.controller.grammar_executor.shutdown(wait=True)
        self.root.destroy()

    def wait_for_errors(self, widget: tk.Text) -> None:
        """What: Process UI events until a spelling underline appears or a deadline expires.

        Why included: Worker results must be delivered on Tk's thread.
        Where used: Focused asynchronous highlighting tests.
        Returns: None; fails the test on timeout.
        """
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.root.update()
            if widget.tag_ranges(SPELL_TAG):
                return
            time.sleep(0.01)
        self.fail("Spelling result was not applied")

    def test_bilingual_lookup(self) -> None:
        """What: Recognise English/Afrikaans words only in their selected language.

        Why included: Foreign words must not be silently accepted by the second dictionary.
        Where used: Dictionary regression check.
        Returns: None.
        """
        self.assertTrue(self.engine.known("welcome"))
        self.assertTrue(self.engine.known("welkom", "af"))
        self.assertTrue(self.engine.known("gebed", "af"))
        self.assertFalse(self.engine.known("gebed", "en"))
        self.assertFalse(self.engine.known("prayer", "af"))
        self.assertFalse(self.engine.known("helllo"))
        self.assertIn("hello", self.engine.suggestions("helllo"))
        self.assertEqual(self.engine.errors("welcome welkom helllo"), [(8, 14), (15, 21)])

    def test_detected_language_rejects_foreign_words(self) -> None:
        """What: Detect the surrounding sentence language before looking up a foreign word.

        Why included: One valid Afrikaans word in English must not change the accepted dictionary.
        Where used: Regression for cross-language acceptance in both directions.
        Returns: None.
        """
        english = "We gather at church for gebed every Sunday."
        afrikaans = "Ons kom by die kerk vir prayer elke Sondag."
        self.assertEqual(self.engine.language(english), "en")
        self.assertEqual(self.engine.language(afrikaans), "af")
        self.assertEqual([english[start:end] for start, end in self.engine.errors(english)], ["gebed"])
        self.assertEqual([afrikaans[start:end] for start, end in self.engine.errors(afrikaans)], ["prayer"])
        self.assertEqual(self.engine.errors("Ons bid saam in die kerk."), [])
        self.assertEqual(self.engine.language(""), "en")

    def test_afrikaans_dictionary_evidence_and_corrections(self) -> None:
        """What: Recognise Afrikaans fields that the statistical classifier labels English.

        Why included: Short greetings and ministry text must use the Afrikaans dictionary and corrections.
        Where used: Automatic Afrikaans language regression checks.
        Returns: None.
        """
        for text in ("welkom", "gebed", "Welkom by ons kerk", "Ek is lief vir Jesus."):
            self.assertEqual(self.engine.language(text), "af", text)
            self.assertEqual(self.engine.errors(text), [], text)
        sentence = "Ons bid saam vir die welkomm byeenkoms."
        self.assertEqual(self.engine.language(sentence), "af")
        self.assertIn("welkomm", [sentence[start:end] for start, end in self.engine.errors(sentence)])
        self.assertIn("welkom", self.engine.suggestions("welkomm", self.engine.language(sentence)))
        self.assertEqual(self.engine.language("goeie more"), "af")
        self.assertEqual(self.engine.errors("Goeie m\u00f4re"), [])

    def test_afrikaans_context_menu_uses_detected_field_language(self) -> None:
        """What: Offer Afrikaans corrections based on surrounding field text in Auto mode.

        Why included: Engine detection must propagate to the actual correction menu.
        Where used: Single-line Afrikaans input regression check.
        Returns: None.
        """
        entry = tk.Entry(self.root)
        entry.insert(0, "welkomm ons bid saam in die kerk")
        with patch.object(tk.Menu, "tk_popup"):
            self.controller._popup(SimpleNamespace(widget=entry, x=0, y=0, x_root=10, y_root=10))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.root.update()
            if self.controller.menu.entrycget(0, "label") != "Checking spelling...":
                break
            time.sleep(0.01)
        self.assertEqual(self.controller._selected_language(entry, entry.get()), "af")
        labels = [self.controller.menu.entrycget(index, "label")
                  for index in range(self.controller.menu.index("end") + 1)
                  if self.controller.menu.type(index) == "command"]
        self.assertIn("welkom", labels)

    def test_highlights_without_changing_text_or_modified_flag(self) -> None:
        """What: Highlight an error without replacing text or altering its modified flag.

        Why included: Existing preview callbacks use that flag to detect real edits.
        Where used: Text-widget regression check.
        Returns: None.
        """
        widget = tk.Text(self.root)
        widget.insert("1.0", "welcome welkom helllo")
        widget.edit_modified(False)
        self.controller._check(widget)
        self.wait_for_errors(widget)
        self.assertEqual(widget.get("1.0", "end-1c"), "welcome welkom helllo")
        self.assertFalse(widget.edit_modified())
        self.assertEqual(tuple(map(str, widget.tag_ranges(SPELL_TAG))), ("1.8", "1.14", "1.15", "1.21"))

    def test_unicode_offsets(self) -> None:
        """What: Underline the correct word after non-BMP symbols and accented text.

        Why included: Python and Tk character offsets can differ for supplementary Unicode characters.
        Where used: Unicode editor regression check.
        Returns: None.
        """
        widget = tk.Text(self.root)
        widget.insert("1.0", "\U0001f600 welcome helllo")
        self.controller._check(widget)
        self.wait_for_errors(widget)
        ranges = widget.tag_ranges(SPELL_TAG)
        self.assertEqual(widget.get(ranges[0], ranges[1]), "helllo")

    def test_entry_correction_and_readonly_protection(self) -> None:
        """What: Correct a single-line field but refuse edits after it becomes read-only.

        Why included: Entries cannot use Text tags and require a safe replacement path.
        Where used: Entry/ttk protection regression check.
        Returns: None.
        """
        entry = ttk.Entry(self.root)
        entry.insert(0, "helllo church")
        self.controller.menu_context = (entry, "helllo church", 0, 6, "helllo")
        self.controller._replace("hello")
        self.assertEqual(entry.get(), "hello church")
        entry.configure(state="readonly")
        self.assertFalse(self.controller.editable(entry))
        self.controller.menu_context = (entry, "hello church", 0, 5, "hello")
        self.controller._replace("changed")
        self.assertEqual(entry.get(), "hello church")
        password = tk.Entry(self.root, show="*")
        self.assertFalse(self.controller.editable(password))
        numeric = tk.Entry(self.root, validate="key")
        self.assertFalse(self.controller.editable(numeric))

    def test_stale_results_and_popup_fields(self) -> None:
        """What: Ignore old snapshots and schedule checking for newly created popup fields.

        Why included: Asynchronous work and dynamic dialogs must behave like main-window fields.
        Where used: Stale-result and popup regression checks.
        Returns: None.
        """
        popup = tk.Toplevel(self.root)
        widget = tk.Text(popup)
        widget.insert("1.0", "welcome")
        self.controller.results.put(("errors", widget, "helllo", 0, [(0, 6)]))
        self.root.after_cancel(self.controller.poll_id)
        self.controller._poll()
        self.assertFalse(widget.tag_ranges(SPELL_TAG))
        widget.delete("1.0", "end")
        widget.insert("1.0", "helllo")
        self.controller._schedule(SimpleNamespace(widget=widget, type=tk.EventType.FocusIn))
        self.wait_for_errors(widget)
        popup.destroy()

    def test_text_correction_is_one_undo_step(self) -> None:
        """What: Replace a multiline editor word and undo the entire correction once.

        Why included: Spelling suggestions must not fragment normal editor undo history.
        Where used: Text correction regression check.
        Returns: None.
        """
        widget = tk.Text(self.root, undo=True)
        widget.insert("1.0", "helllo church")
        widget.edit_reset()
        self.controller.menu_context = (widget, "helllo church", 0, 6, "helllo")
        self.controller._replace("hello")
        self.assertEqual(widget.get("1.0", "end-1c"), "hello church")
        widget.edit_undo()
        self.assertEqual(widget.get("1.0", "end-1c"), "helllo church")

    def test_context_menu_suggestions(self) -> None:
        """What: Right-click a misspelt Entry word and receive asynchronous corrections.

        Why included: Single-line fields need the same correction workflow as multiline editors.
        Where used: Context-menu regression check.
        Returns: None.
        """
        entry = tk.Entry(self.root)
        entry.insert(0, "helllo")
        with patch.object(tk.Menu, "tk_popup"):
            handled = self.controller._popup(SimpleNamespace(widget=entry, x=0, y=0, x_root=10, y_root=10))
        self.assertEqual(handled, "break")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.root.update()
            if self.controller.menu.entrycget(0, "label") != "Checking spelling...":
                break
            time.sleep(0.01)
        labels = [self.controller.menu.entrycget(index, "label")
                  for index in range(self.controller.menu.index("end") + 1)
                  if self.controller.menu.type(index) == "command"]
        self.assertIn("hello", labels)

    def test_grammar_highlighting_and_correction(self) -> None:
        """What: Highlight and correct a grammar issue without changing valid spelling.

        Why included: Sentence issues need a separate tag and safe phrase-edit/undo behavior.
        Where used: Grammar controller regression check.
        Returns: None.
        """
        widget = tk.Text(self.root, undo=True)
        snapshot = "Jesus are here"
        widget.insert("1.0", snapshot)
        widget.edit_reset()
        widget.edit_modified(False)
        issue = GrammarIssue(6, 9, "Use is for a singular subject.", ("is",), "AGREEMENT")
        self.controller.results.put(("grammar", widget, snapshot, 0, [issue]))
        self.root.after_cancel(self.controller.poll_id)
        self.controller._poll()
        ranges = widget.tag_ranges(GRAMMAR_TAG)
        self.assertEqual(widget.get(ranges[0], ranges[1]), "are")
        self.assertFalse(widget.edit_modified())
        self.controller.menu_context = (widget, snapshot, 6, 9, "are")
        self.controller._replace_grammar(issue, "is")
        self.assertEqual(widget.get("1.0", "end-1c"), "Jesus is here")
        widget.edit_undo()
        self.assertEqual(widget.get("1.0", "end-1c"), snapshot)

    def test_grammar_entry_menu_and_stale_results(self) -> None:
        """What: Offer Entry grammar corrections and discard outdated sentence results.

        Why included: Single-line fields need grammar menus without receiving stale edits.
        Where used: Entry grammar regression check.
        Returns: None.
        """
        entry = tk.Entry(self.root)
        entry.insert(0, "Jesus are here")
        issue = GrammarIssue(6, 9, "Use is.", ("is",), "AGREEMENT")
        self.controller.grammar_cache[entry] = (entry.get(), [issue])
        self.controller.detected_languages[entry] = (entry.get(), "en")
        self.controller.menu_context = (entry, entry.get(), 6, 9, "are")
        self.controller.grammar_menu = tk.Menu(entry, tearoff=False)
        self.controller._fill_grammar_menu()
        self.assertEqual(self.controller.grammar_menu.entrycget(1, "label"), "is")
        entry.delete(0, "end")
        entry.insert(0, "Jesus is here")
        self.controller._replace_grammar(issue, "was")
        self.assertEqual(entry.get(), "Jesus is here")

    def test_manual_language_override_and_stale_policy(self) -> None:
        """What: Recheck ambiguous text after a manual language change and reject older-policy results.

        Why included: Short input cannot reliably be detected and pending English checks must not undo an Afrikaans override.
        Where used: Per-field language menu regression check.
        Returns: None.
        """
        widget = tk.Text(self.root)
        widget.insert("1.0", "gebed")
        self.controller._set_language(widget, "en")
        self.controller.executor.submit(lambda: None).result(timeout=10)
        self.root.after_cancel(self.controller.poll_id)
        self.controller._poll()
        self.assertTrue(widget.tag_ranges(SPELL_TAG))
        with patch.object(self.controller.grammar_engine, "check", return_value=[]) as grammar:
            self.controller._set_language(widget, "af")
            self.controller.executor.submit(lambda: None).result(timeout=10)
            self.controller.grammar_executor.submit(lambda: None).result(timeout=10)
            grammar.reset_mock()
            self.controller.results.put(("errors", widget, "gebed", 0, [(0, 5)], "en"))
            self.root.after_cancel(self.controller.poll_id)
            self.controller._poll()
            self.assertFalse(widget.tag_ranges(SPELL_TAG))
            self.assertEqual(self.controller._selected_language(widget, "gebed"), "af")
            grammar.assert_not_called()


class GrammarEngineTests(unittest.TestCase):
    """What: Exercise the actual bundled local grammar server.

    Why included: Mocked Tk tests alone cannot prove sentence-level detection works.
    Where used: The standard spelling/grammar regression runner.
    """

    @classmethod
    def setUpClass(cls) -> None:
        """What: Create one local engine shared by the grammar examples.

        Why included: Java startup is performed only once in this test class.
        Where used: unittest class initialization.
        Returns: None.
        """
        cls.engine = GrammarEngine()

    @classmethod
    def tearDownClass(cls) -> None:
        """What: Stop and reap the test's private LanguageTool process.

        Why included: Tests must not leave Java services behind.
        Where used: unittest class cleanup.
        Returns: None.
        """
        cls.engine.close()

    def test_real_grammar_examples(self) -> None:
        """What: Catch the requested agreement error and a standard English pronoun error.

        Why included: Both the supplementary proper-name rule and LanguageTool must work.
        Where used: Real local-engine regression checks.
        Returns: None.
        """
        bad = self.engine.check("Jesus are here")
        self.assertTrue(any("is" in issue.replacements for issue in bad))
        self.assertEqual(self.engine.check("Jesus is here"), [])
        self.assertEqual(self.engine.check("Jesus and his disciples are here."), [])
        self.assertTrue(any("is" in issue.replacements for issue in self.engine.check("He are here.")))

    def test_java_offset_conversion(self) -> None:
        """What: Convert UTF-16 offsets after supplementary Unicode characters.

        Why included: Java grammar spans and Python/Tk snapshots must refer to the same words.
        Where used: Grammar span conversion regression check.
        Returns: None.
        """
        self.assertEqual(_character_offset("\U0001f600 Jesus are here", 9), 8)


if __name__ == "__main__":
    unittest.main()
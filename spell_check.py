"""Offline bilingual spelling and private English grammar assistance for Tk fields."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from functools import lru_cache
from itertools import islice
import queue
import re
import tkinter as tk
from tkinter import ttk

from langid.langid import LanguageIdentifier, model
from spylls.hunspell import Dictionary
from spylls.hunspell import readers
from spylls.hunspell.readers.file_reader import FileReader

from app_paths import get_app_base_path
from grammar_check import GrammarEngine, GrammarIssue
from perf_logging import log_exception


WORD_PATTERN = re.compile(r"[^\W\d_]+(?:['\u2019][^\W\d_]+)*", re.UNICODE)
SPELL_TAG = "spellcheck_error"
GRAMMAR_TAG = "grammarcheck_error"


class _DictionaryReader(FileReader):
    """What: Close the previous Hunspell file handle when its declared encoding changes.

    Why included: The upstream FileReader replaces handles without closing them.
    Where used: SpellingEngine dictionary loading.
    """

    def reset_encoding(self, encoding: str) -> None:
        """What: Close the current handle before reopening in the dictionary's encoding.

        Why included: A SET encoding directive otherwise leaves the initial reader open.
        Where used: Hunspell affix parser.
        Returns: None.
        """
        self.io.close()
        super().reset_encoding(encoding)


def _load_dictionary(path: str) -> Dictionary:
    """What: Parse an affix/dictionary pair with explicit file-handle cleanup.

    Why included: Dictionary resource handles must be released even if parsing fails.
    Where used: SpellingEngine initialization.
    Returns: Hunspell dictionary with its inflection and suggestion rules.
    """
    aff_reader = _DictionaryReader(path + ".aff")
    try:
        aff, context = readers.read_aff(aff_reader)
    finally:
        aff_reader.io.close()
    dic_reader = _DictionaryReader(path + ".dic", encoding=context.encoding)
    try:
        dic = readers.read_dic(dic_reader, aff=aff, context=context)
    finally:
        dic_reader.io.close()
    return Dictionary(aff, dic)


class SpellingEngine:
    """What: Check words against offline English and Afrikaans Hunspell dictionaries.

    Why included: Proper affix rules handle inflections better than a flat word list.
    Where used: SpellCheckController's single background worker.
    """

    def __init__(self) -> None:
        """What: Load both bundled dictionaries and initialize session exemptions.

        Why included: Spelling must work without sending text to an online service.
        Where used: Lazily constructed on the spell-check worker thread.
        Returns: None.
        """
        directory = get_app_base_path() / "Resourses" / "spellcheck"
        self.dictionaries = [_load_dictionary(str(directory / name)) for name in ("en_ZA", "af_ZA")]
        self.identifier = LanguageIdentifier.from_modelstring(model, norm_probs=True)
        self.identifier.set_languages(["en", "af"])
        self.ignored: set[tuple[str, str]] = set()

    @lru_cache(maxsize=128)
    def language(self, text: str) -> str:
        """What: Infer the field language from dictionary evidence and a statistical fallback.

        Why included: A foreign word must not be accepted merely because another dictionary contains it.
        Where used: Automatic spelling-language selection and grammar routing.
        Returns: en or af; tied evidence uses the classifier and empty text defaults to English.
        """
        words = WORD_PATTERN.findall(text)
        if not words:
            return "en"
        english_only = 0
        afrikaans_only = 0
        for word in words:
            english = self.dictionaries[0].lookup(word.lower())
            afrikaans = self.dictionaries[1].lookup(word.lower())
            if english and not afrikaans:
                english_only += 1
            elif afrikaans and not english:
                afrikaans_only += 1
        if afrikaans_only > english_only:
            return "af"
        if english_only > afrikaans_only:
            return "en"
        detected, _confidence = self.identifier.classify(text)
        return detected

    @lru_cache(maxsize=4096)
    def known(self, word: str, language: str = "en") -> bool:
        """What: Accept a word only in the selected dictionary or its session exemptions.

        Why included: Afrikaans words should not silently pass in an English field, or vice versa.
        Where used: Text highlighting and context-menu construction.
        Returns: True for accepted words.
        """
        dictionary = self.dictionaries[0 if language == "en" else 1]
        return (language, word.casefold()) in self.ignored or dictionary.lookup(word)

    def errors(self, text: str, language: str | None = None) -> list[tuple[int, int]]:
        """What: Locate unrecognised words in an editable field's snapshot.

        Why included: Workers return offsets instead of touching Tk widget state.
        Where used: Debounced highlighting requests.
        Returns: Start/end character offsets for suspect words.
        """
        selected = language or self.language(text)
        return [(match.start(), match.end()) for match in WORD_PATTERN.finditer(text)
                if len(match.group()) > 1 and not self.known(match.group(), selected)]

    def suggestions(self, word: str, language: str = "en") -> list[str]:
        """What: Collect correction candidates from only the field's selected language.

        Why included: A correction menu must not offer words from the other dictionary.
        Where used: SpellCheckController's context menu.
        Returns: Up to eight distinct candidate words.
        """
        candidates: list[str] = []
        dictionary = self.dictionaries[0 if language == "en" else 1]
        for candidate in islice(dictionary.suggest(word), 8):
            if candidate not in candidates:
                candidates.append(candidate)
        return candidates[:8]

    def ignore(self, word: str, language: str = "en") -> None:
        """What: Exempt a word for the rest of the application session.

        Why included: Names and specialised ministry terms may not be in either dictionary.
        Where used: Ignore Word context-menu action.
        Returns: None; invalidates cached lookup results.
        """
        self.ignored.add((language, word.casefold()))
        self.known.cache_clear()


class SpellCheckController:
    """What: Add debounced highlighting and right-click corrections to Tk text inputs.

    Why included: One controller covers tabs and dynamically opened popups without replacing handlers.
    Where used: Installed once by main.build_ui().
    """

    def __init__(self, root: tk.Tk) -> None:
        """What: Register additive app-wide events and start a UI result queue.

        Why included: Dictionaries and suggestions run off-thread while all Tk calls stay on-thread.
        Where used: Main window initialization.
        Returns: None.
        """
        self.root = root
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="SpellCheck")
        self.grammar_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="GrammarCheck")
        self.grammar_engine = GrammarEngine()
        self.grammar_cache: dict[tk.Misc, tuple[str, list[GrammarIssue]]] = {}
        self.grammar_jobs: dict[tk.Misc, Future[None]] = {}
        self.grammar_failure: str | None = None
        self.grammar_menu: tk.Menu | None = None
        self.language_modes: dict[tk.Misc, str] = {}
        self.detected_languages: dict[tk.Misc, tuple[str, str]] = {}
        self.language_menu: tk.Menu | None = None
        self.engine: SpellingEngine | None = None
        self.results: queue.SimpleQueue = queue.SimpleQueue()
        self.pending: dict[tk.Misc, str] = {}
        self.closed = False
        self.menu: tk.Menu | None = None
        self.menu_token = 0
        for sequence in ("<FocusIn>", "<KeyRelease>", "<<Modified>>", "<<Paste>>", "<<Cut>>"):
            root.bind_all(sequence, self._schedule, add="+")
        root.bind_all("<Button-3>", self._popup, add="+")
        root.bind_all("<Destroy>", self._destroyed, add="+")
        self.poll_id = root.after(50, self._poll)

    @staticmethod
    def editable(widget: tk.Misc) -> bool:
        """What: Limit spelling assistance to writable Text, Entry, and Combobox widgets.

        Why included: Read-only output, password fields, and validated numeric fields must stay untouched.
        Where used: Event routing, result application, and correction safeguards.
        Returns: True when the widget is an eligible editable field.
        """
        if not isinstance(widget, (tk.Text, tk.Entry, ttk.Entry, ttk.Combobox)):
            return False
        try:
            if str(widget.cget("state")) != "normal":
                return False
            if not isinstance(widget, tk.Text):
                if str(widget.cget("show")) or str(widget.cget("validate")) != "none":
                    return False
            return True
        except tk.TclError:
            return False

    @staticmethod
    def text(widget: tk.Misc) -> str:
        """What: Read a field without Tk Text's implicit trailing newline.

        Why included: Worker offsets must match the actual editable content.
        Where used: Highlight snapshots and stale-result checks.
        Returns: Current field text.
        """
        return widget.get("1.0", "end-1c") if isinstance(widget, tk.Text) else widget.get()

    def _schedule(self, event) -> None:
        """What: Debounce checks after focus, typing, cut, or paste events.

        Why included: Existing modified-event handlers and text undo history must remain intact.
        Where used: App-wide additive Tk event bindings.
        Returns: None.
        """
        widget = event.widget
        if self.closed or not self.editable(widget):
            return
        if event.type == tk.EventType.VirtualEvent and self.root.focus_get() is not widget:
            return
        previous = self.pending.pop(widget, None)
        if previous:
            self.root.after_cancel(previous)
        self.pending[widget] = self.root.after(450, lambda: self._check(widget))

    def _check(self, widget: tk.Misc) -> None:
        """What: Capture current text and submit a worker lookup.

        Why included: Dictionary loading and large text checks must not freeze the interface.
        Where used: Debounce callback and post-correction refresh.
        Returns: None.
        """
        self.pending.pop(widget, None)
        if not self.closed and self.editable(widget):
            snapshot = self.text(widget)
            self._submit("errors", snapshot, widget, snapshot)

    def _selected_language(self, widget: tk.Misc, snapshot: str) -> str | None:
        """What: Resolve a manual language choice or a current automatic detection result.

        Why included: Menus and English grammar must use the same language as spelling.
        Where used: Grammar routing and language menu status.
        Returns: en/af, or None while automatic detection is pending.
        """
        mode = self.language_modes.get(widget, "auto")
        if mode != "auto":
            return mode
        cached = self.detected_languages.get(widget)
        return cached[1] if cached is not None and cached[0] == snapshot else None

    def _set_language(self, widget: tk.Misc, mode: str) -> None:
        """What: Set Auto, English, or Afrikaans for one field and invalidate old checks.

        Why included: Short names and ambiguous text need a manual language override.
        Where used: Spelling Language radio menu commands.
        Returns: None; rechecks the field without changing its content.
        """
        self.language_modes[widget] = mode
        self.detected_languages.pop(widget, None)
        self.grammar_cache.pop(widget, None)
        if isinstance(widget, tk.Text):
            widget.tag_remove(GRAMMAR_TAG, "1.0", "end")
        self.menu_token += 1
        if self.menu is not None:
            self.menu.destroy()
            self.menu = None
            self.grammar_menu = None
            self.language_menu = None
        self._check(widget)

    def _check_grammar(self, widget: tk.Misc, snapshot: str) -> None:
        """What: Queue a whole-field English grammar check on its own worker.

        Why included: Java startup must not hold up spelling checks or the Tk event loop.
        Where used: Debounced text edits and right-click grammar checks.
        Returns: None; results are marshalled through the existing UI queue.
        """
        if self.closed or not self.editable(widget) or self._selected_language(widget, snapshot) != "en":
            return
        mode = self.language_modes.get(widget, "auto")
        previous = self.grammar_jobs.pop(widget, None)
        if previous is not None:
            previous.cancel()

        def work() -> None:
            """What: Check the captured sentence and queue issues or an explicit status.

            Why included: Grammar failures should not remove spelling assistance or silently look successful.
            Where used: The serial grammar executor.
            Returns: None.
            """
            if self.closed:
                return
            try:
                issues = self.grammar_engine.check(snapshot)
                self.results.put(("grammar", widget, snapshot, 0, issues, mode))
            except Exception as error:
                log_exception("grammarcheck.worker", error)
                self.results.put(("grammar_failure", widget, snapshot, 0, str(error), mode))

        self.grammar_jobs[widget] = self.grammar_executor.submit(work)

    def _submit(self, operation: str, value: str, widget: tk.Misc, snapshot: str, token: int = 0) -> None:
        """What: Execute one dictionary operation and queue its result for Tk.

        Why included: No worker may call a Tk widget or schedule a Tk timer.
        Where used: Highlighting, suggestion, and ignore requests.
        Returns: None.
        """
        mode = self.language_modes.get(widget, "auto")

        def work() -> None:
            """What: Run the spelling operation on the serial worker and report failures.

            Why included: Serial execution keeps dictionary caches and ignored words consistent.
            Where used: Executor callback inside _submit().
            Returns: None.
            """
            try:
                if self.engine is None:
                    self.engine = SpellingEngine()
                language = self.engine.language(snapshot) if mode == "auto" else mode
                self.results.put(("language", widget, snapshot, token, language, mode))
                if operation == "errors":
                    result = self.engine.errors(value, language)
                elif operation == "suggestions":
                    result = [] if self.engine.known(value, language) else self.engine.suggestions(value, language)
                else:
                    self.engine.ignore(value, language)
                    result = self.engine.errors(snapshot, language)
                self.results.put((operation, widget, snapshot, token, result, mode))
            except Exception as error:
                log_exception("spellcheck.worker", error)
                self.results.put(("failure", widget, snapshot, token, [], mode))

        if not self.closed:
            self.executor.submit(work)

    def _poll(self) -> None:
        """What: Apply completed checks only when widgets still contain their captured text.

        Why included: Slow results must not underline or replace newer edits.
        Where used: Repeating main-thread timer.
        Returns: None.
        """
        try:
            while True:
                operation, widget, snapshot, token, result, *policy = self.results.get_nowait()
                if not self.editable(widget) or self.text(widget) != snapshot:
                    continue
                if policy and policy[0] != self.language_modes.get(widget, "auto"):
                    continue
                if operation == "language":
                    self.detected_languages[widget] = (snapshot, result)
                    if result == "af":
                        self.grammar_cache.pop(widget, None)
                        if isinstance(widget, tk.Text):
                            widget.tag_remove(GRAMMAR_TAG, "1.0", "end")
                    elif self.grammar_failure is None:
                        cached = self.grammar_cache.get(widget)
                        if cached is None or cached[0] != snapshot:
                            self._check_grammar(widget, snapshot)
                    self._fill_grammar_menu()
                    if self.language_menu is not None and self.language_menu.winfo_exists():
                        menu_widget, menu_snapshot, *_ = self.menu_context
                        if menu_widget is widget and menu_snapshot == snapshot:
                            self.language_menu.entryconfigure(0, label="Detected: " + ("English" if result == "en" else "Afrikaans"))
                elif operation == "grammar":
                    if self._selected_language(widget, snapshot) == "af":
                        continue
                    self.grammar_failure = None
                    self.grammar_cache[widget] = (snapshot, result)
                    if isinstance(widget, tk.Text):
                        widget.tag_configure(GRAMMAR_TAG, foreground="#175cd3", underline=True)
                        widget.tag_remove(GRAMMAR_TAG, "1.0", "end")
                        for issue in result:
                            widget.tag_add(GRAMMAR_TAG, f"1.0+{issue.start}c", f"1.0+{issue.end}c")
                    self._fill_grammar_menu()
                elif operation == "grammar_failure":
                    self.grammar_failure = result
                    self._fill_grammar_menu()
                elif operation in {"errors", "ignore"} and isinstance(widget, tk.Text):
                    widget.tag_configure(SPELL_TAG, foreground="#b42318", underline=True)
                    widget.tag_remove(SPELL_TAG, "1.0", "end")
                    for start, end in result:
                        widget.tag_add(SPELL_TAG, f"1.0+{start}c", f"1.0+{end}c")
                elif operation in {"suggestions", "failure"} and token == self.menu_token and self.menu is not None:
                    self._fill_menu(result, operation == "failure")
        except queue.Empty:
            pass
        if not self.closed:
            self.poll_id = self.root.after(50, self._poll)

    def _popup(self, event) -> str | None:
        """What: Offer standard editing actions and spelling/grammar corrections on right-click.

        Why included: Single-line and multi-line text fields need right-click Cut, Copy, Paste,
        Select All, Delete, Undo/Redo, and Clear alongside spelling/grammar suggestions.
        Where used: App-wide right-click binding, including popup fields.
        Returns: Tk break for handled fields, otherwise None.
        """
        widget = event.widget
        if self.closed or not isinstance(widget, (tk.Text, tk.Entry, ttk.Entry, ttk.Combobox)):
            return None
        try:
            widget_state = str(widget.cget("state"))
            is_password = not isinstance(widget, tk.Text) and bool(str(widget.cget("show") or ""))
        except tk.TclError:
            return None
        if widget_state == "disabled" or is_password:
            return None
        if isinstance(widget, ttk.Combobox) and widget_state == "readonly":
            return None
        is_editable = self.editable(widget)
        try:
            widget.focus_set()
        except tk.TclError:
            pass
        snapshot = self.text(widget)
        match = None
        if is_editable:
            try:
                if isinstance(widget, tk.Text):
                    index = widget.index(f"@{event.x},{event.y}")
                    offset = widget.count("1.0", index, "chars")
                    position = offset[0] if offset else 0
                else:
                    position = int(widget.index(f"@{event.x}"))
                match = next((m for m in WORD_PATTERN.finditer(snapshot)
                              if m.start() <= position < m.end()), None)
            except Exception:
                match = None
        if self.menu is not None:
            self.menu.destroy()
        self.menu_token += 1
        if match is not None:
            self.menu_context = (widget, snapshot, match.start(), match.end(), match.group())
        else:
            self.menu_context = (widget, snapshot, 0, 0, "")
        self.menu = tk.Menu(widget, tearoff=False)
        if match is not None:
            self.menu.add_command(label="Checking spelling...", state="disabled")
            self.menu.add_separator()
            self.menu.add_command(label="Ignore word (this session)", command=self._ignore)
            self.menu.add_separator()

        has_selection = self._has_selection(widget)
        has_clipboard = self._has_clipboard(widget)
        has_text = bool(snapshot)

        self.menu.add_command(
            label="Cut",
            state="normal" if (is_editable and has_selection) else "disabled",
            command=lambda w=widget: self._edit_action(w, "cut"),
        )
        self.menu.add_command(
            label="Copy",
            state="normal" if (has_selection or has_text) else "disabled",
            command=lambda w=widget: self._edit_action(w, "copy"),
        )
        self.menu.add_command(
            label="Paste",
            state="normal" if (is_editable and has_clipboard) else "disabled",
            command=lambda w=widget: self._edit_action(w, "paste"),
        )
        self.menu.add_command(
            label="Delete",
            state="normal" if (is_editable and has_selection) else "disabled",
            command=lambda w=widget: self._edit_action(w, "delete"),
        )
        self.menu.add_separator()
        self.menu.add_command(
            label="Select All",
            state="normal" if has_text else "disabled",
            command=lambda w=widget: self._edit_action(w, "select_all"),
        )
        self.menu.add_command(
            label="Clear",
            state="normal" if (is_editable and has_text) else "disabled",
            command=lambda w=widget: self._edit_action(w, "clear"),
        )
        if isinstance(widget, tk.Text) and is_editable:
            try:
                undo_enabled = bool(int(widget.cget("undo")))
            except Exception:
                undo_enabled = False
            if undo_enabled:
                self.menu.add_separator()
                self.menu.add_command(label="Undo", command=lambda w=widget: self._edit_action(w, "undo"))
                self.menu.add_command(label="Redo", command=lambda w=widget: self._edit_action(w, "redo"))

        if is_editable:
            self.menu.add_separator()
            self.language_menu = tk.Menu(self.menu, tearoff=False)
            selected = self._selected_language(widget, snapshot)
            self.language_menu.add_command(label="Detected: " + ({"en": "English", "af": "Afrikaans"}.get(selected, "checking...")), state="disabled")
            self.language_menu.add_separator()
            self.language_choice = tk.StringVar(master=widget, value=self.language_modes.get(widget, "auto"))
            for label, mode in (("Auto", "auto"), ("English", "en"), ("Afrikaans", "af")):
                self.language_menu.add_radiobutton(label=label, variable=self.language_choice, value=mode,
                                                  command=lambda choice=mode: self._set_language(widget, choice))
            self.menu.add_cascade(label="Spelling language", menu=self.language_menu)
            if match is not None:
                self.grammar_menu = tk.Menu(self.menu, tearoff=False)
                self.menu.add_cascade(label="English grammar", menu=self.grammar_menu)
                self._fill_grammar_menu()
                self._submit("suggestions", match.group(), widget, snapshot, self.menu_token)
            else:
                self.grammar_menu = None
        else:
            self.language_menu = None
            self.grammar_menu = None
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()
        return "break"

    @staticmethod
    def _has_selection(widget: tk.Misc) -> bool:
        try:
            if isinstance(widget, tk.Text):
                return bool(widget.tag_ranges("sel"))
            return bool(widget.selection_present())
        except Exception:
            return False

    @staticmethod
    def _has_clipboard(widget: tk.Misc) -> bool:
        try:
            return bool(widget.clipboard_get())
        except Exception:
            return False

    def _edit_action(self, widget: tk.Misc, action: str) -> None:
        """What: Execute a standard clipboard or selection command on a Text or Entry widget.

        Why included: Provides consistent right-click Cut, Copy, Paste, Delete, Select All, Clear, Undo, and Redo.
        Where used: Context-menu edit commands built in _popup().
        Returns: None; notifies modified/key listeners when editable content changes.
        """
        try:
            if action == "cut":
                if not self.editable(widget):
                    return
                widget.event_generate("<<Cut>>")
                if not isinstance(widget, tk.Text):
                    widget.event_generate("<KeyRelease>")
                else:
                    self._check(widget)
            elif action == "copy":
                if self._has_selection(widget):
                    widget.event_generate("<<Copy>>")
                else:
                    content = self.text(widget)
                    if content:
                        widget.clipboard_clear()
                        widget.clipboard_append(content)
            elif action == "paste":
                if not self.editable(widget):
                    return
                widget.event_generate("<<Paste>>")
                if not isinstance(widget, tk.Text):
                    widget.event_generate("<KeyRelease>")
                else:
                    self._check(widget)
            elif action == "delete":
                if not self.editable(widget) or not self._has_selection(widget):
                    return
                if isinstance(widget, tk.Text):
                    widget.delete("sel.first", "sel.last")
                    self._check(widget)
                else:
                    widget.delete("sel.first", "sel.last")
                    widget.event_generate("<KeyRelease>")
            elif action == "select_all":
                if isinstance(widget, tk.Text):
                    widget.tag_add("sel", "1.0", "end-1c")
                    widget.mark_set("insert", "end-1c")
                else:
                    widget.selection_range(0, "end")
                    widget.icursor("end")
            elif action == "clear":
                if not self.editable(widget):
                    return
                if isinstance(widget, tk.Text):
                    widget.delete("1.0", "end")
                    self._check(widget)
                else:
                    widget.delete(0, "end")
                    widget.event_generate("<KeyRelease>")
            elif action == "undo" and isinstance(widget, tk.Text):
                widget.edit_undo()
                self._check(widget)
            elif action == "redo" and isinstance(widget, tk.Text):
                widget.edit_redo()
                self._check(widget)
        except tk.TclError:
            pass

    def _fill_menu(self, suggestions: list[str], failed: bool = False) -> None:
        """What: Replace the loading menu item with correction commands.

        Why included: Suggestion generation can be expensive and must complete asynchronously.
        Where used: Result polling for the current context menu.
        Returns: None.
        """
        if self.menu is None:
            return
        self.menu.delete(0, 0)
        if not suggestions:
            self.menu.insert_command(0, label="Spell check unavailable" if failed else "No spelling suggestions", state="disabled")
        for index, suggestion in enumerate(suggestions):
            self.menu.insert_command(index, label=suggestion, command=lambda value=suggestion: self._replace(value))

    def _fill_grammar_menu(self) -> None:
        """What: Show grammar explanations and replacements for the clicked text span.

        Why included: Correctly spelled words can still be grammatically wrong in their sentence.
        Where used: Context-menu opening and asynchronous grammar results.
        Returns: None.
        """
        if self.grammar_menu is None or not self.grammar_menu.winfo_exists():
            return
        widget, snapshot, start, end, word = self.menu_context
        self.grammar_menu.delete(0, "end")
        selected = self._selected_language(widget, snapshot)
        if selected != "en":
            label = "Afrikaans grammar checking is not enabled" if selected == "af" else "Detecting text language..."
            self.grammar_menu.add_command(label=label, state="disabled")
            return
        cached = self.grammar_cache.get(widget)
        if self.grammar_failure is not None:
            self.grammar_menu.add_command(label=self.grammar_failure, state="disabled")
        elif cached is None or cached[0] != snapshot:
            self.grammar_menu.add_command(label="Checking English grammar...", state="disabled")
        else:
            issues = [issue for issue in cached[1] if issue.start < end and issue.end > start]
            if not issues:
                self.grammar_menu.add_command(label="No English grammar issues here", state="disabled")
            for issue in issues:
                self.grammar_menu.add_command(label=issue.message, state="disabled")
                for replacement in issue.replacements:
                    self.grammar_menu.add_command(
                        label=replacement or "Remove",
                        command=lambda issue=issue, value=replacement: self._replace_grammar(issue, value),
                    )
        self.grammar_menu.add_separator()
        self.grammar_menu.add_command(label="Recheck English grammar", command=lambda: self._check_grammar(widget, snapshot))

    def _replace_grammar(self, issue: GrammarIssue, replacement: str) -> None:
        """What: Apply an engine-provided phrase replacement using the existing safe edit path.

        Why included: Grammar fixes may cover a phrase rather than just one clicked word.
        Where used: English grammar menu commands.
        Returns: None; refuses changes when the checked snapshot is stale.
        """
        widget, snapshot, start, end, word = self.menu_context
        self.menu_context = (widget, snapshot, issue.start, issue.end, snapshot[issue.start:issue.end])
        self._replace(replacement, preserve_case=False)

    def _replace(self, replacement: str, preserve_case: bool = True) -> None:
        """What: Replace only the clicked word if the original snapshot is still current.

        Why included: Corrections must preserve surrounding text and refuse stale menu actions.
        Where used: Suggestion menu commands.
        Returns: None; existing text/variable bindings receive the normal edit.
        """
        widget, snapshot, start, end, word = self.menu_context
        if not self.editable(widget) or self.text(widget) != snapshot:
            return
        if preserve_case and word.istitle():
            replacement = replacement.capitalize()
        elif preserve_case and word.isupper():
            replacement = replacement.upper()
        if isinstance(widget, tk.Text):
            separators = widget.cget("autoseparators")
            widget.edit_separator()
            widget.configure(autoseparators=False)
            try:
                widget.delete(f"1.0+{start}c", f"1.0+{end}c")
                widget.insert(f"1.0+{start}c", replacement)
            finally:
                widget.edit_separator()
                widget.configure(autoseparators=separators)
            self._check(widget)
        else:
            widget.delete(start, end)
            widget.insert(start, replacement)
            widget.event_generate("<KeyRelease>")

    def _ignore(self) -> None:
        """What: Ignore the selected word for this session and refresh its field.

        Why included: Names and specialist terminology should be dismissible without automatic correction.
        Where used: Ignore Word menu command.
        Returns: None.
        """
        widget, snapshot, start, end, word = self.menu_context
        self._submit("ignore", word, widget, snapshot)

    def _destroyed(self, event) -> None:
        """What: Cancel field timers when widgets are destroyed and stop on root destruction.

        Why included: Closed popups must not leave timers retaining destroyed fields.
        Where used: Additive app-wide Destroy binding.
        Returns: None.
        """
        if event.widget is self.root:
            self.close()
        timer = self.pending.pop(event.widget, None)
        if timer:
            self.root.after_cancel(timer)
        self.grammar_cache.pop(event.widget, None)
        self.language_modes.pop(event.widget, None)
        self.detected_languages.pop(event.widget, None)
        job = self.grammar_jobs.pop(event.widget, None)
        if job is not None:
            job.cancel()

    def close(self) -> None:
        """What: Cancel spelling timers and pending worker operations.

        Why included: Application shutdown must not queue work against a destroyed Tk interpreter.
        Where used: Main shutdown and root Destroy handler.
        Returns: None.
        """
        if self.closed:
            return
        self.closed = True
        self.root.after_cancel(self.poll_id)
        for timer in self.pending.values():
            self.root.after_cancel(timer)
        self.pending.clear()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.grammar_engine.closed.set()
        for job in self.grammar_jobs.values():
            job.cancel()
        self.grammar_jobs.clear()
        self.grammar_executor.submit(self.grammar_engine.close)
        self.grammar_executor.shutdown(wait=False)
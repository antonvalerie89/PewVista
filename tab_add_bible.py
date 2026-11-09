"""Import Bible source files/URLs and keep associated provenance notes."""

from __future__ import annotations

import os
import shutil
from datetime import date
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from app_paths import get_app_base_path as default_get_app_base_path


SUPPORTED_BIBLE_IMPORT_EXTENSIONS = (".pdf", ".json", ".txt", ".usfm", ".sfm", ".zip", ".jar")

KNOWN_PROVIDER_NAMES = {
    "biblesa.co.za": "Bible Society of South Africa",
    "bibleshop.co.za": "Bible Society of South Africa",
    "cumbooks.co.za": "Christelike Uitgewers Maatskappy / CUM Books",
    "ebible.org": "eBible.org",
    "m.bibles.co.za": "Bible Society of South Africa",
    "openenglishbible.org": "OpenEnglishBible.org",
    "play.google.com": "Google Play",
    "wordpocket.org": "Wordproject / WordPocket",
    "wordproject.org": "Wordproject / WordPocket",
    "www.biblesa.co.za": "Bible Society of South Africa",
    "www.biblefactory.org": "City Bible Foundation / BibleFactory",
    "www.cumbooks.co.za": "Christelike Uitgewers Maatskappy / CUM Books",
    "www.ebible.org": "eBible.org",
    "www.wordpocket.org": "Wordproject / WordPocket",
    "www.wordproject.org": "Wordproject / WordPocket",
}


def build_bible_tab(tab_frame: tk.Frame, context: dict[str, object]) -> None:
    """Build Bible file import, source provenance, and license-note controls.

    What: Creates the Bibles library list, selected-file metadata/editor, source discoveries,
    file/URL import commands, and links to license/extracted folders.
    Why included: Imported source files need a stable library location and provenance records
    before reader or translation workflows can consume them.
    Where used: Add Bible notebook tab built by main.create_tabs_content().
    Returns: None; registers widgets and imports files beneath the app's Bibles directory.
    """
    tab_widgets = context["tab_widgets"]
    tab_labels = context["tab_labels"]
    tab_inputs = context.get("tab_inputs")
    apply_layout_style = context["apply_layout_style"]
    get_app_base_path = context.get("get_app_base_path")
    start_background_task = context.get("start_background_task")

    if callable(get_app_base_path):
        try:
            app_base_path = Path(get_app_base_path())
        except Exception:
            app_base_path = default_get_app_base_path()
    else:
        app_base_path = default_get_app_base_path()
    bibles_directory = app_base_path / "Bibles"
    licenses_directory = bibles_directory / "licenses"
    bibles_directory.mkdir(parents=True, exist_ok=True)
    licenses_directory.mkdir(parents=True, exist_ok=True)

    state: dict[str, object] = {
        "current_license_fields": None,
        "file_verification_states": {},
        "selected_file_path": None,
        "available_files": [],
        "discovered_versions": [],
        "selected_discovery_index": None,
    }

    for column_index, column_weight in enumerate((4, 3, 2)):
        tab_frame.columnconfigure(column_index, weight=column_weight)
    tab_frame.rowconfigure(0, weight=1)

    library_column = tk.Frame(tab_frame, bd=1, relief="solid", padx=12, pady=12)
    library_column.grid(row=0, column=0, sticky="nsew", padx=8, pady=12)
    library_column.columnconfigure(0, weight=1)
    library_column.rowconfigure(2, weight=1)

    library_title = tk.Label(library_column, text="Bible Files", justify="center", anchor="center")
    library_title.grid(row=0, column=0, sticky="ew", pady=(0, 8))

    action_row = tk.Frame(library_column)
    action_row.grid(row=1, column=0, sticky="ew", pady=(0, 8))
    action_row.columnconfigure(0, weight=1)
    action_row.columnconfigure(1, weight=1)
    action_row.columnconfigure(2, weight=1)
    action_row.columnconfigure(3, weight=1)

    import_button = tk.Button(action_row, text="Import Bible Files")
    import_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))

    refresh_button = tk.Button(action_row, text="Refresh")
    refresh_button.grid(row=0, column=1, sticky="ew", padx=4)

    import_url_button = tk.Button(action_row, text="Import From URL")
    import_url_button.grid(row=0, column=2, sticky="ew", padx=4)

    open_folder_button = tk.Button(action_row, text="Open Folder")
    open_folder_button.grid(row=0, column=3, sticky="ew", padx=(4, 0))

    files_wrapper = tk.Frame(library_column)
    files_wrapper.grid(row=2, column=0, sticky="nsew")
    files_wrapper.columnconfigure(0, weight=1)
    files_wrapper.rowconfigure(0, weight=1)

    files_listbox = tk.Listbox(files_wrapper, exportselection=False)
    files_listbox.grid(row=0, column=0, sticky="nsew")
    files_scrollbar = tk.Scrollbar(files_wrapper, orient="vertical", command=files_listbox.yview)
    files_scrollbar.grid(row=0, column=1, sticky="ns")
    files_listbox.configure(yscrollcommand=files_scrollbar.set)

    details_column = tk.Frame(tab_frame, bd=1, relief="solid", padx=12, pady=12)
    details_column.grid(row=0, column=1, sticky="nsew", padx=8, pady=12)
    details_column.columnconfigure(0, weight=1)

    details_title = tk.Label(details_column, text="Selected Bible", justify="center", anchor="center")
    details_title.grid(row=0, column=0, sticky="ew", pady=(0, 8))

    selected_name_label = tk.Label(details_column, text="No Bible selected", anchor="w", justify="left")
    selected_name_label.grid(row=1, column=0, sticky="ew", pady=4)

    selected_type_label = tk.Label(details_column, text="Type: -", anchor="w", justify="left")
    selected_type_label.grid(row=2, column=0, sticky="ew", pady=4)

    selected_size_label = tk.Label(details_column, text="Size: -", anchor="w", justify="left")
    selected_size_label.grid(row=3, column=0, sticky="ew", pady=4)

    selected_path_label = tk.Label(details_column, text="Path: -", anchor="w", justify="left", wraplength=280)
    selected_path_label.grid(row=4, column=0, sticky="ew", pady=4)

    selected_source_label = tk.Label(details_column, text="Source: -", anchor="w", justify="left", wraplength=280)
    selected_source_label.grid(row=5, column=0, sticky="ew", pady=4)

    source_provider_var = tk.StringVar(value="")
    source_provider_entry = tk.Entry(details_column, textvariable=source_provider_var)
    source_provider_entry.grid(row=6, column=0, sticky="ew", pady=(0, 4))

    selected_source_hint_label = tk.Label(details_column, text="Source Hint: -", anchor="w", justify="left", wraplength=280)
    selected_source_hint_label.grid(row=7, column=0, sticky="ew", pady=4)

    official_source_page_var = tk.StringVar(value="")
    official_source_page_entry = tk.Entry(details_column, textvariable=official_source_page_var)
    official_source_page_entry.grid(row=8, column=0, sticky="ew", pady=(0, 4))

    selected_license_label = tk.Label(details_column, text="License Record: -", anchor="w", justify="left", wraplength=280)
    selected_license_label.grid(row=9, column=0, sticky="ew", pady=4)

    selected_license_status_label = tk.Label(details_column, text="License Check: -", anchor="w", justify="left", wraplength=280)
    selected_license_status_label.grid(row=10, column=0, sticky="ew", pady=4)

    selected_extracted_label = tk.Label(details_column, text="Extracted Folder: -", anchor="w", justify="left", wraplength=280)
    selected_extracted_label.grid(row=11, column=0, sticky="ew", pady=4)

    source_verified_var = tk.BooleanVar(value=False)
    source_verified_checkbutton = tk.Checkbutton(details_column, text="Source Verified", variable=source_verified_var)
    source_verified_checkbutton.grid(row=12, column=0, sticky="w", pady=(4, 0))

    details_action_row = tk.Frame(details_column)
    details_action_row.grid(row=13, column=0, sticky="ew", pady=(8, 0))
    details_action_row.columnconfigure(0, weight=1)
    details_action_row.columnconfigure(1, weight=1)
    details_action_row.columnconfigure(2, weight=1)
    details_action_row.columnconfigure(3, weight=1)
    details_action_row.columnconfigure(4, weight=1)

    open_license_button = tk.Button(details_action_row, text="Open License Note", state="disabled")
    open_license_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))

    open_licenses_button = tk.Button(details_action_row, text="Open Licenses", state="normal")
    open_licenses_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))

    open_extracted_button = tk.Button(details_action_row, text="Open Extracted", state="disabled")
    open_extracted_button.grid(row=0, column=2, sticky="ew", padx=(4, 0))

    save_license_button = tk.Button(details_action_row, text="Save Status", state="disabled")
    save_license_button.grid(row=0, column=3, sticky="ew", padx=(4, 0))

    create_license_button = tk.Button(details_action_row, text="Create License Note", state="disabled")
    create_license_button.grid(row=0, column=4, sticky="ew", padx=(4, 0))

    notes_label = tk.Label(details_column, text="PDF, ZIP, and JAR Bible packs can be stored now and converted later for use inside the program. Keep source/license notes in Bibles/licenses.", anchor="w", justify="left", wraplength=280)
    notes_label.grid(row=14, column=0, sticky="ew", pady=(12, 0))

    help_column = tk.Frame(tab_frame, bd=1, relief="solid", padx=12, pady=12)
    help_column.grid(row=0, column=2, sticky="nsew", padx=8, pady=12)
    help_column.columnconfigure(0, weight=1)
    help_column.rowconfigure(3, weight=1)

    help_title = tk.Label(help_column, text="Next Step", justify="center", anchor="center")
    help_title.grid(row=0, column=0, sticky="ew", pady=(0, 8))

    help_body = tk.Label(
        help_column,
        text=(
            "1. Import Bible PDFs, ZIP packs, or converted files into the Bibles folder.\n\n"
            "JAR Bible packs from official mobile Bible downloads are also supported.\n\n"
            "2. Later we can add conversion into a verse format the program can search.\n\n"
            "3. Keep source and license records in Bibles/licenses for each Bible source."
        ),
        anchor="nw",
        justify="left",
        wraplength=220,
    )
    help_body.grid(row=1, column=0, sticky="nsew")

    discovery_title = tk.Label(help_column, text="Online Discoveries", justify="center", anchor="center")
    discovery_title.grid(row=2, column=0, sticky="ew", pady=(16, 8))

    discovery_wrapper = tk.Frame(help_column)
    discovery_wrapper.grid(row=3, column=0, sticky="nsew")
    discovery_wrapper.columnconfigure(0, weight=1)
    discovery_wrapper.rowconfigure(0, weight=1)

    discovery_listbox = tk.Listbox(discovery_wrapper, exportselection=False, height=8)
    discovery_listbox.grid(row=0, column=0, sticky="nsew")
    discovery_scrollbar = tk.Scrollbar(discovery_wrapper, orient="vertical", command=discovery_listbox.yview)
    discovery_scrollbar.grid(row=0, column=1, sticky="ns")
    discovery_listbox.configure(yscrollcommand=discovery_scrollbar.set)

    discovered_publisher_label = tk.Label(help_column, text="Publisher: -", anchor="w", justify="left", wraplength=220)
    discovered_publisher_label.grid(row=4, column=0, sticky="ew", pady=(8, 0))

    discovered_status_label = tk.Label(help_column, text="Status: -", anchor="w", justify="left", wraplength=220)
    discovered_status_label.grid(row=5, column=0, sticky="ew", pady=(4, 0))

    open_discoveries_button = tk.Button(help_column, text="Open Discovery Note")
    open_discoveries_button.grid(row=6, column=0, sticky="ew", pady=(8, 0))

    def format_file_size(size_bytes: int) -> str:
        """What: Format bytes as B, KB, or MB for selected-file details.

        Why included: Raw file sizes are difficult to scan in the metadata panel.
        Where used: update_selected_file_details() for the selected Bible source.
        Returns: Compact size string with one decimal for KB/MB.
        """
        if size_bytes < 1024:
            return f"{size_bytes} B"
        if size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        return f"{size_bytes / (1024 * 1024):.1f} MB"

    def get_discoveries_note_path() -> Path:
        """What: Resolve the shared note listing Bible versions found online.

        Why included: Discovery metadata belongs alongside source/license notes.
        Where used: Discovery list load and Open Discovery Note action.
        Returns: Path to licenses/Afrikaans versions discovered online.txt.
        """
        return licenses_directory / "Afrikaans versions discovered online.txt"

    def load_discovered_versions() -> list[dict[str, str]]:
        """What: Parse numbered version entries and publisher/status fields from the note.

        Why included: The discoveries panel reads a human-maintained plain-text index.
        Where used: refresh_discovered_versions() when populating the right-side list.
        Returns: Ordered discovery mappings; missing or unreadable notes yield an empty list.
        """
        note_path = get_discoveries_note_path()
        if not note_path.exists():
            return []
        versions: list[dict[str, str]] = []
        current: dict[str, str] | None = None
        try:
            for raw_line in note_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                if line[:2].isdigit() and ". " in line:
                    if current is not None:
                        versions.append(current)
                    current = {"name": line.split(". ", 1)[1], "publisher": "-", "status": "-"}
                    continue
                if current is None:
                    continue
                if line.startswith("- Publisher:"):
                    current["publisher"] = line.split(":", 1)[1].strip() or "-"
                elif line.startswith("- Local download status:"):
                    current["status"] = line.split(":", 1)[1].strip() or "-"
            if current is not None:
                versions.append(current)
        except Exception:
            return []
        return versions

    def update_discovery_details() -> None:
        """What: Show publisher/status for the selected discovery and enable its note action.

        Why included: The list alone does not expose provenance detail or selection validity.
        Where used: Discovery selection and list-refresh callbacks.
        Returns: None; resets labels/button when no valid discovery is selected.
        """
        discovered_versions = state.get("discovered_versions")
        selected_index = state.get("selected_discovery_index")
        if not isinstance(discovered_versions, list) or not isinstance(selected_index, int):
            discovered_publisher_label.configure(text="Publisher: -")
            discovered_status_label.configure(text="Status: -")
            open_discoveries_button.configure(state="disabled")
            return
        if not (0 <= selected_index < len(discovered_versions)):
            discovered_publisher_label.configure(text="Publisher: -")
            discovered_status_label.configure(text="Status: -")
            open_discoveries_button.configure(state="disabled")
            return
        selected_entry = discovered_versions[selected_index]
        discovered_publisher_label.configure(text=f"Publisher: {selected_entry.get('publisher', '-')}")
        discovered_status_label.configure(text=f"Status: {selected_entry.get('status', '-')}")
        open_discoveries_button.configure(state="normal")

    def refresh_discovered_versions() -> None:
        """What: Reload the discovery note, refill its Listbox, and select the first entry.

        Why included: File changes and tab startup need to synchronize visible discovery data.
        Where used: Initial setup and discovery-refresh workflows.
        Returns: None; resets selection/details when the note has no entries.
        """
        discovered_versions = load_discovered_versions()
        state["discovered_versions"] = discovered_versions
        discovery_listbox.delete(0, tk.END)
        for entry in discovered_versions:
            discovery_listbox.insert(tk.END, entry.get("name", "Unknown version"))
        if discovered_versions:
            discovery_listbox.selection_clear(0, tk.END)
            discovery_listbox.selection_set(0)
            discovery_listbox.activate(0)
            state["selected_discovery_index"] = 0
        else:
            state["selected_discovery_index"] = None
        update_discovery_details()

    def get_selected_file_path() -> Path | None:
        """What: Normalize the currently selected source path from tab state.

        Why included: Listbox selection and imports store paths as either Path or string.
        Where used: License, detail, folder, and file-selection actions.
        Returns: Selected Path, or None when state has no usable value.
        """
        selected_path = state.get("selected_file_path")
        if isinstance(selected_path, Path):
            return selected_path
        if isinstance(selected_path, str) and selected_path.strip():
            return Path(selected_path)
        return None

    def get_license_record_path(selected_path: Path | None) -> Path | None:
        """What: Map a Bible source file to its sibling-stem license-note path.

        Why included: Each imported source needs a predictable provenance record.
        Where used: License parsing, status display, creation, saving, and opening.
        Returns: licenses/<source-stem>.license.txt, or None without a source path.
        """
        if selected_path is None:
            return None
        return licenses_directory / f"{selected_path.stem}.license.txt"

    def guess_provider_name(raw_value: str) -> str:
        """What: Normalize a URL/domain and map known hosts to publisher names.

        Why included: Imported source URLs often identify a provider only by hostname.
        Where used: License-note parsing and automatic URL-import stubs.
        Returns: Known provider label, normalized hostname, or Unknown for blank input.
        """
        candidate = raw_value.strip()
        if not candidate:
            return "Unknown"
        if "://" in candidate:
            candidate = urllib_parse.urlparse(candidate).netloc or candidate
        candidate = candidate.strip().lower()
        return KNOWN_PROVIDER_NAMES.get(candidate, candidate.replace("www.", "").strip() or "Unknown")

    def parse_license_fields(license_path: Path | None) -> dict[str, object] | None:
        """What: Read source metadata and verification fields from a license note.

        Why included: The detail panel must edit existing provenance without discarding other lines.
        Where used: Selected-file detail updates and license-status persistence.
        Returns: Parsed fields plus original lines, or None when absent/unreadable.
        """
        if license_path is None or not license_path.exists():
            return None
        try:
            lines = license_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return None
        fields: dict[str, object] = {
            "lines": lines,
            "source_provider": "",
            "source_page": "",
            "official_source_page": "",
            "source_verified": False,
        }
        for line in lines:
            if line.startswith("Source provider:"):
                fields["source_provider"] = line.split(":", 1)[1].strip()
            elif line.startswith("Source page:"):
                fields["source_page"] = line.split(":", 1)[1].strip()
            elif line.startswith("Official source page:"):
                fields["official_source_page"] = line.split(":", 1)[1].strip()
            elif line.startswith("Source verified:"):
                value = line.split(":", 1)[1].strip().lower()
                fields["source_verified"] = value in {"yes", "true", "verified"}
        return fields

    def is_license_verified(license_path: Path | None) -> bool:
        """What: Check whether a matching license-note file exists.

        Why included: The UI treats a present note as the current verification artifact.
        Where used: License-status indicators and file-list verification state.
        Returns: True when the path exists; False for missing paths/files.
        """
        if license_path is None or not license_path.exists():
            return False
        return True

    def get_license_status(license_path: Path | None) -> tuple[str, str]:
        """What: Return the license-note presence code and user-facing status text.

        Why included: File rows and detail labels need consistent license status wording.
        Where used: refresh_bible_files() and update_selected_file_details().
        Returns: ('missing', message) or ('verified', message) based on note existence.
        """
        if license_path is None or not license_path.exists():
            return "missing", "Missing license note"
        return "verified", "Verified automatically from license note"

    def set_or_insert_line(lines: list[str], prefix: str, new_line: str, insert_after_prefix: str | None = None) -> list[str]:
        """What: Replace a prefixed metadata line or add it at a chosen location.

        Why included: Saving license fields should preserve unrelated note text and ordering.
        Where used: save_selected_license_status() when updating provider/verification fields.
        Returns: New list of lines; the input list is not mutated.
        """
        updated_lines = list(lines)
        for index, line in enumerate(updated_lines):
            if line.startswith(prefix):
                updated_lines[index] = new_line
                return updated_lines
        insert_index = len(updated_lines)
        if insert_after_prefix is not None:
            for index, line in enumerate(updated_lines):
                if line.startswith(insert_after_prefix):
                    insert_index = index + 1
                    break
        updated_lines.insert(insert_index, new_line)
        return updated_lines

    def get_extracted_directory_path(selected_path: Path | None) -> Path | None:
        """What: Resolve the expected extraction folder for a ZIP Bible pack.

        Why included: ZIP imports may expose contents in a sibling folder for inspection.
        Where used: Selected-file details and Open Extracted action.
        Returns: Bibles/<archive-stem> for ZIP files, otherwise None.
        """
        if selected_path is None or selected_path.suffix.lower() != ".zip":
            return None
        return bibles_directory / selected_path.stem

    def read_source_name(license_path: Path | None) -> str:
        """What: Determine the source label from provider metadata or its source URL.

        Why included: The selected-file panel should show known provenance when available.
        Where used: update_selected_file_details() for the Source label.
        Returns: Provider/domain label, Unknown, or '-' when no note exists.
        """
        parsed_fields = parse_license_fields(license_path)
        if not isinstance(parsed_fields, dict):
            return "-"
        source_provider = str(parsed_fields.get("source_provider") or "").strip()
        if source_provider:
            return source_provider
        source_page = str(parsed_fields.get("official_source_page") or parsed_fields.get("source_page") or "").strip()
        if source_page:
            return guess_provider_name(source_page)
        return "Unknown"

    def update_selected_file_details() -> None:
        """What: Populate file metadata, license fields, and action states for the selection.

        Why included: Selecting a source must reveal provenance and available follow-up actions.
        Where used: File selection, import completion, and list refresh.
        Returns: None; clears all details when the selected file is missing.
        """
        selected_path = get_selected_file_path()
        if selected_path is None or not selected_path.exists():
            state["current_license_fields"] = None
            selected_name_label.configure(text="No Bible selected")
            selected_type_label.configure(text="Type: -")
            selected_size_label.configure(text="Size: -")
            selected_path_label.configure(text="Path: -")
            selected_source_label.configure(text="Source: -")
            source_provider_var.set("")
            selected_source_hint_label.configure(text="Source Hint: -")
            official_source_page_var.set("")
            selected_license_label.configure(text="License Record: -")
            selected_license_status_label.configure(text="License Check: -")
            selected_extracted_label.configure(text="Extracted Folder: -")
            source_verified_var.set(False)
            open_license_button.configure(state="disabled")
            open_extracted_button.configure(state="disabled")
            save_license_button.configure(state="disabled")
            create_license_button.configure(state="disabled")
            return
        selected_name_label.configure(text=selected_path.name)
        selected_type_label.configure(text=f"Type: {selected_path.suffix.lower() or 'unknown'}")
        try:
            size_text = format_file_size(selected_path.stat().st_size)
        except Exception:
            size_text = "Unknown"
        selected_size_label.configure(text=f"Size: {size_text}")
        selected_path_label.configure(text=f"Path: {selected_path}")
        license_path = get_license_record_path(selected_path)
        parsed_fields = parse_license_fields(license_path)
        state["current_license_fields"] = parsed_fields
        selected_source_label.configure(text=f"Source: {read_source_name(license_path)}")
        license_status_code, license_status_text = get_license_status(license_path)
        source_hint_value = "-"
        if isinstance(parsed_fields, dict):
            source_provider_var.set(str(parsed_fields.get("source_provider") or ""))
            official_source_page_var.set(str(parsed_fields.get("official_source_page") or ""))
            source_hint_candidate = str(parsed_fields.get("official_source_page") or parsed_fields.get("source_page") or "").strip()
            if source_hint_candidate:
                source_hint_value = guess_provider_name(source_hint_candidate)
            source_verified_var.set(license_path is not None and license_path.exists())
        else:
            source_provider_var.set("")
            official_source_page_var.set("")
            source_verified_var.set(False)
        selected_source_hint_label.configure(text=f"Source Hint: {source_hint_value}")
        if license_path is not None and license_path.exists():
            selected_license_label.configure(text=f"License Record: {license_path.name}")
            selected_license_status_label.configure(text=f"License Check: {license_status_text}")
            open_license_button.configure(state="normal")
            save_license_button.configure(state="normal")
            create_license_button.configure(state="disabled")
        else:
            selected_license_label.configure(text="License Record: Missing")
            selected_license_status_label.configure(text=f"License Check: {license_status_text}")
            open_license_button.configure(state="disabled")
            save_license_button.configure(state="disabled")
            create_license_button.configure(state="normal")
        extracted_directory = get_extracted_directory_path(selected_path)
        if extracted_directory is not None and extracted_directory.exists() and extracted_directory.is_dir():
            selected_extracted_label.configure(text=f"Extracted Folder: {extracted_directory.name}")
            open_extracted_button.configure(state="normal")
        elif extracted_directory is not None:
            selected_extracted_label.configure(text="Extracted Folder: Not extracted yet")
            open_extracted_button.configure(state="disabled")
        else:
            # No extraction path is defined for this source, unlike a ZIP that simply has not been unpacked.
            selected_extracted_label.configure(text="Extracted Folder: -")
            open_extracted_button.configure(state="disabled")

    def refresh_bible_files(select_path: Path | None = None) -> None:
        """What: Rescan supported Bible files and update selection/license indicators.

        Why included: Imports and note edits must be reflected in the source library list.
        Where used: Import/refresh actions, license updates, and URL-import completion.
        Returns: None; optionally selects the supplied imported file.
        """
        available_files = sorted(
            [
                path
                for path in bibles_directory.iterdir()
                if path.is_file() and path.suffix.lower() in SUPPORTED_BIBLE_IMPORT_EXTENSIONS
            ],
            key=lambda item: item.name.casefold(),
        )
        state["available_files"] = available_files
        state["file_verification_states"] = {
            path: get_license_status(get_license_record_path(path))[0]
            for path in available_files
        }
        files_listbox.delete(0, tk.END)
        for path in available_files:
            verification_state = str(state["file_verification_states"].get(path) or "missing")
            if verification_state == "verified":
                display_name = path.name
            else:
                display_name = f"[Missing License] {path.name}"
            files_listbox.insert(tk.END, display_name)

        target_path = select_path if isinstance(select_path, Path) else get_selected_file_path()
        if target_path is not None:
            matching_index = next((index for index, path in enumerate(available_files) if path == target_path), -1)
        else:
            matching_index = 0 if available_files else -1
        if matching_index >= 0:
            files_listbox.selection_clear(0, tk.END)
            files_listbox.selection_set(matching_index)
            files_listbox.activate(matching_index)
            state["selected_file_path"] = available_files[matching_index]
        else:
            state["selected_file_path"] = None
        update_selected_file_details()

    def build_unique_destination(source_path: Path) -> Path:
        """What: Choose an unused destination filename for an imported Bible source.

        Why included: Imports must not overwrite an existing translation file.
        Where used: import_bible_files() when copying selected files into Bibles/.
        Returns: Original basename path or a numbered collision-free variant.
        """
        destination_path = bibles_directory / source_path.name
        if not destination_path.exists():
            return destination_path
        stem = source_path.stem
        suffix = source_path.suffix
        counter = 2
        # Increment until the source can be copied without overwriting a prior import.
        while True:
            candidate = bibles_directory / f"{stem} ({counter}){suffix}"
            if not candidate.exists():
                return candidate
            counter += 1

    def build_unique_destination_from_name(file_name: str) -> Path:
        """What: Sanitize a download filename and find a collision-free Bibles/ destination.

        Why included: URL paths and response headers can provide missing/conflicting names.
        Where used: URL-import worker before writing response bytes.
        Returns: Unique destination Path, using a PDF fallback for blank names.
        """
        cleaned_name = Path(file_name).name.strip()
        if not cleaned_name:
            cleaned_name = "downloaded_bible.pdf"
        destination_path = bibles_directory / cleaned_name
        if not destination_path.exists():
            return destination_path
        stem = destination_path.stem
        suffix = destination_path.suffix
        counter = 2
        # Download names use the same collision rule as local imports.
        while True:
            candidate = bibles_directory / f"{stem} ({counter}){suffix}"
            if not candidate.exists():
                return candidate
            counter += 1

    def ensure_license_stub_for_url_import(destination_path: Path, source_url: str) -> Path | None:
        """What: Create a provenance/license-note template after a successful URL import.

        Why included: Downloading a file does not verify its translation-specific license.
        Where used: URL-import success callback.
        Returns: Existing or newly created note Path; None when no note path is available.
        """
        license_path = get_license_record_path(destination_path)
        if license_path is None or license_path.exists():
            return license_path
        source_domain = urllib_parse.urlparse(source_url).netloc or "Unknown"
        guessed_provider = guess_provider_name(source_domain)
        license_text = (
            f"Bible file: {destination_path.name}\n"
            f"Source provider: {guessed_provider}\n"
            "Official source page: Paste the publisher or store page here after verification.\n"
            f"Source page: {source_url}\n"
            "Source verified: No\n"
            f"Verified download path used: {source_url}\n"
            f"Imported into workspace: {date.today().isoformat()}\n\n"
            "Status:\n"
            "- Downloaded by URL import from a user-supplied direct file link.\n"
            "- Exact translation-specific license text has not yet been verified.\n"
            "- Review the source page and update this note before redistributing converted text.\n\n"
            "Notes:\n"
            "- This stub was created automatically by the Bible tab after a successful URL import.\n"
            "- Replace the source provider with the formal publisher name once verified.\n"
        )
        license_path.write_text(license_text, encoding="utf-8")
        return license_path

    def create_license_stub_for_file(selected_path: Path) -> Path | None:
        """What: Create a basic provenance note for a locally imported source file.

        Why included: Local files still need a matching note for source and redistribution review.
        Where used: create_selected_license_record().
        Returns: Existing or newly created license-note Path, or None without a note path.
        """
        license_path = get_license_record_path(selected_path)
        if license_path is None or license_path.exists():
            return license_path
        license_text = (
            f"Bible file: {selected_path.name}\n"
            "Source provider: Unknown\n"
            "Official source page: Paste the publisher or store page here after verification.\n"
            "Source page: Add the source page or supplier here.\n"
            "Source verified: Yes\n"
            "Verified download path used: Added locally from an existing file import.\n"
            f"Imported into workspace: {date.today().isoformat()}\n\n"
            "Status:\n"
            "- This license note was created from the Bible tab for a local Bible file already in the workspace.\n"
            "- Fill in the exact source page and provider when they are known.\n\n"
            "Notes:\n"
            "- This file now counts as licensed in the program because a matching license note exists.\n"
            "- Re-check redistribution terms before shipping converted Bible text outside this workspace.\n"
        )
        license_path.write_text(license_text, encoding="utf-8")
        return license_path

    def save_selected_license_status() -> None:
        """What: Save edited provider, official-source URL, and verification status fields.

        Why included: License metadata should be editable without replacing the rest of the note.
        Where used: Save Status button command.
        Returns: None; refreshes selected-file details and reports write errors.
        """
        selected_path = get_selected_file_path()
        license_path = get_license_record_path(selected_path)
        parsed_fields = state.get("current_license_fields")
        if license_path is None or not license_path.exists() or not isinstance(parsed_fields, dict):
            messagebox.showinfo("Bible", "No license note is available to update.", parent=tab_frame.winfo_toplevel())
            return
        lines = list(parsed_fields.get("lines") or [])
        source_provider_line = f"Source provider: {source_provider_var.get().strip() or 'Unknown'}"
        official_source_page_value = official_source_page_var.get().strip() or "Paste the publisher or store page here after verification."
        official_source_page_line = f"Official source page: {official_source_page_value}"
        updated_lines = set_or_insert_line(lines, "Source provider:", source_provider_line)
        updated_lines = set_or_insert_line(updated_lines, "Official source page:", official_source_page_line, insert_after_prefix="Source provider:")
        verified_line = f"Source verified: {'Yes' if source_verified_var.get() else 'No'}"
        updated_lines = set_or_insert_line(updated_lines, "Source verified:", verified_line, insert_after_prefix="Official source page:")
        try:
            license_path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
        except Exception as error:
            messagebox.showerror("Bible", f"Unable to save the license note.\n\n{error}", parent=tab_frame.winfo_toplevel())
            return
        refresh_bible_files(selected_path)
        messagebox.showinfo("Bible", "License status saved.", parent=tab_frame.winfo_toplevel())

    def create_selected_license_record() -> None:
        """What: Create a license-note stub for the selected Bible file when one is missing.

        Why included: Users need a provenance record before marking source details verified.
        Where used: Create License Note button command.
        Returns: None; reports invalid selection, existing notes, and creation failures.
        """
        selected_path = get_selected_file_path()
        if selected_path is None or not selected_path.exists():
            messagebox.showinfo("Bible", "Select a Bible file first.", parent=tab_frame.winfo_toplevel())
            return
        license_path = get_license_record_path(selected_path)
        if license_path is not None and license_path.exists():
            messagebox.showinfo("Bible", "The selected Bible already has a license note.", parent=tab_frame.winfo_toplevel())
            return
        try:
            created_path = create_license_stub_for_file(selected_path)
        except Exception as error:
            messagebox.showerror("Bible", f"Unable to create the license note.\n\n{error}", parent=tab_frame.winfo_toplevel())
            return
        refresh_bible_files(selected_path)
        if created_path is None:
            messagebox.showinfo("Bible", "No license note was created.", parent=tab_frame.winfo_toplevel())
            return
        messagebox.showinfo("Bible", f"Created {created_path.name} for the selected Bible.", parent=tab_frame.winfo_toplevel())

    def infer_download_name(download_url: str, response_headers) -> str:
        """What: Derive a download filename from Content-Disposition or the URL path.

        Why included: Direct file URLs may not include a useful extension in the UI.
        Where used: URL-import worker before validating supported file types.
        Returns: Filename, defaulting to downloaded_bible.pdf when neither source provides one.
        """
        content_disposition = ""
        try:
            content_disposition = str(response_headers.get("Content-Disposition") or "")
        except Exception:
            content_disposition = ""
        if "filename=" in content_disposition:
            candidate_name = content_disposition.split("filename=", 1)[1].strip().strip('"')
            if candidate_name:
                return candidate_name
        parsed_url = urllib_parse.urlparse(download_url)
        candidate_name = Path(urllib_parse.unquote(parsed_url.path)).name
        if candidate_name:
            return candidate_name
        return "downloaded_bible.pdf"

    def set_download_controls_enabled(enabled: bool) -> None:
        """What: Enable or disable all controls that can start or refresh imports.

        Why included: Prevents overlapping URL/file imports from racing on library updates.
        Where used: Before and after URL-import worker completion/failure.
        Returns: None; configures the three import/refresh buttons.
        """
        state_value = "normal" if enabled else "disabled"
        import_button.configure(state=state_value)
        refresh_button.configure(state=state_value)
        import_url_button.configure(state=state_value)

    def download_bible_from_url() -> None:
        """What: Prompt for a direct Bible file URL and import it with a worker when available.

        Why included: Supports user-supplied online PDF/ZIP/JAR/JSON/text source files.
        Where used: Import From URL button command.
        Returns: None; validates extensions, creates a license stub, and refreshes the file list.
        """
        download_url = simpledialog.askstring(
            "Import Bible From URL",
            "Enter the direct Bible file URL:",
            parent=tab_frame.winfo_toplevel(),
        )
        if not isinstance(download_url, str) or not download_url.strip():
            return
        download_url = download_url.strip()

        def worker() -> tuple[Path, str]:
            """What: Download the URL response to a unique supported file in Bibles/.

            Why included: Network I/O and large files should not block the Tk event loop.
            Where used: Background worker passed to start_background_task().
            Returns: Destination path and original URL; raises for unsupported extensions.
            """
            if urllib_parse.urlparse(download_url).scheme.lower() not in {"http", "https"}:
                raise ValueError("Only http:// or https:// web addresses can be imported.")
            max_bytes = 500 * 1024 * 1024
            with urllib_request.urlopen(download_url, timeout=60) as response:
                file_name = infer_download_name(download_url, response.headers)
                destination_path = build_unique_destination_from_name(file_name)
                if destination_path.suffix.lower() not in SUPPORTED_BIBLE_IMPORT_EXTENSIONS:
                    raise ValueError(
                        f"Unsupported file type '{destination_path.suffix or 'unknown'}'. "
                        "URL imports currently support PDF, ZIP, JAR, JSON, TXT, USFM, and SFM files."
                    )
                written = 0
                try:
                    with destination_path.open("wb") as destination_file:
                        while True:
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > max_bytes:
                                raise ValueError("The download is larger than 500 MB and was stopped.")
                            destination_file.write(chunk)
                except BaseException:
                    destination_path.unlink(missing_ok=True)
                    raise
            return destination_path, download_url

        def handle_success(result: tuple[Path, str]) -> None:
            """What: Create provenance, refresh selection, and offer to open the new license note.

            Why included: Imported content should immediately appear with its source metadata.
            Where used: on_success callback for the URL-import worker.
            Returns: None; restores import controls and reports/open notes on Tk's thread.
            """
            destination_path, imported_url = result
            license_path = ensure_license_stub_for_url_import(destination_path, imported_url)
            set_download_controls_enabled(True)
            refresh_bible_files(destination_path)
            extra_message = ""
            if license_path is not None:
                extra_message = f"\n\nLicense note:\n{license_path.name}"
            should_open_license = False
            if license_path is not None:
                should_open_license = bool(
                    messagebox.askyesno(
                        "Bible",
                        (
                            f"Imported from URL into {destination_path.name}.\n\n"
                            f"Source URL:\n{imported_url}{extra_message}\n\n"
                            "Open the created license note now?"
                        ),
                        parent=tab_frame.winfo_toplevel(),
                    )
                )
            else:
                messagebox.showinfo(
                    "Bible",
                    f"Imported from URL into {destination_path.name}.\n\nSource URL:\n{imported_url}",
                    parent=tab_frame.winfo_toplevel(),
                )
            if should_open_license and license_path is not None:
                try:
                    os.startfile(str(license_path))
                except Exception as error:
                    messagebox.showerror(
                        "Bible",
                        f"Unable to open the created license note.\n\n{error}",
                        parent=tab_frame.winfo_toplevel(),
                    )

        def handle_error(error: Exception) -> None:
            """What: Re-enable import controls and show the failed URL-import error.

            Why included: Network/write failures must not leave import controls disabled.
            Where used: on_error callback for the URL-import worker and synchronous fallback.
            Returns: None; displays an error dialog.
            """
            set_download_controls_enabled(True)
            messagebox.showerror(
                "Bible",
                f"Unable to import the Bible from URL.\n\n{error}",
                parent=tab_frame.winfo_toplevel(),
            )

        set_download_controls_enabled(False)
        if callable(start_background_task):
            start_background_task("bible_url_import", worker, on_success=handle_success, on_error=handle_error)
            return
        try:
            handle_success(worker())
        except Exception as error:
            handle_error(error)

    def import_bible_files() -> None:
        """What: Select supported source files, copy them into Bibles/, and refresh the list.

        Why included: Local source packs must be staged in the app library before reading/import.
        Where used: Import Bible Files button command.
        Returns: None; skips unsupported extensions and reports imported/skipped files.
        """
        chosen_paths = filedialog.askopenfilenames(
            parent=tab_frame.winfo_toplevel(),
            title="Import Bible Files",
            filetypes=[
                ("Bible Files", "*.pdf *.zip *.jar *.json *.txt *.usfm *.sfm"),
                ("Bible Packs", "*.zip"),
                ("Java Bible Packs", "*.jar"),
                ("PDF Files", "*.pdf"),
                ("All Files", "*.*"),
            ],
        )
        if not chosen_paths:
            return
        imported_paths: list[Path] = []
        skipped_files: list[str] = []
        for raw_path in chosen_paths:
            source_path = Path(str(raw_path))
            if source_path.suffix.lower() not in SUPPORTED_BIBLE_IMPORT_EXTENSIONS:
                skipped_files.append(source_path.name)
                continue
            destination_path = build_unique_destination(source_path)
            shutil.copy2(source_path, destination_path)
            imported_paths.append(destination_path)
        refresh_bible_files(imported_paths[-1] if imported_paths else None)
        if imported_paths:
            message = f"Imported {len(imported_paths)} file(s) into {bibles_directory.name}."
            message += "\n\nImported files without a matching license note will show as [Missing License]."
            if skipped_files:
                message += "\n\nSkipped unsupported files: " + ", ".join(skipped_files)
            messagebox.showinfo("Bible", message, parent=tab_frame.winfo_toplevel())
            return
        if skipped_files:
            messagebox.showinfo("Bible", "No supported Bible files were imported.", parent=tab_frame.winfo_toplevel())

    def handle_file_selection(_event=None) -> None:
        """What: Resolve the selected Bible-file row and refresh its metadata panel.

        Why included: File selection drives license editing and folder actions.
        Where used: Files Listbox <<ListboxSelect>> binding.
        Returns: None; clears details when selection is absent or invalid.
        """
        selection = files_listbox.curselection()
        available_files = state.get("available_files")
        if not isinstance(available_files, list) or not selection:
            state["selected_file_path"] = None
            update_selected_file_details()
            return
        selected_index = int(selection[0])
        if 0 <= selected_index < len(available_files):
            state["selected_file_path"] = available_files[selected_index]
        else:
            state["selected_file_path"] = None
        update_selected_file_details()

    def handle_discovery_selection(_event=None) -> None:
        """What: Store the selected online-discovery row and show its publisher/status.

        Why included: Discovery details and note-opening controls depend on a valid row.
        Where used: Discovery Listbox <<ListboxSelect>> binding.
        Returns: None; clears selection details when no valid row is selected.
        """
        selection = discovery_listbox.curselection()
        discovered_versions = state.get("discovered_versions")
        if not isinstance(discovered_versions, list) or not selection:
            state["selected_discovery_index"] = None
            update_discovery_details()
            return
        selected_index = int(selection[0])
        if 0 <= selected_index < len(discovered_versions):
            state["selected_discovery_index"] = selected_index
        else:
            state["selected_discovery_index"] = None
        update_discovery_details()

    def open_bibles_folder() -> None:
        """What: Open the app's Bibles source directory in Windows Explorer.

        Why included: Users need direct access to imported files and extracted packs.
        Where used: Open Folder button command.
        Returns: None; reports OS launch failures in a dialog.
        """
        try:
            os.startfile(str(bibles_directory))
        except Exception as error:
            messagebox.showerror("Bible", f"Unable to open the Bibles folder.\n\n{error}", parent=tab_frame.winfo_toplevel())

    def open_selected_license_record() -> None:
        """What: Open the selected source's matching license note in the default editor.

        Why included: Provenance can be reviewed or supplemented outside the metadata fields.
        Where used: Open License Note button command.
        Returns: None; informs the user when no note exists and reports launch errors.
        """
        selected_path = get_selected_file_path()
        license_path = get_license_record_path(selected_path)
        if license_path is None or not license_path.exists():
            messagebox.showinfo("Bible", "No license note was found for the selected Bible.", parent=tab_frame.winfo_toplevel())
            return
        try:
            os.startfile(str(license_path))
        except Exception as error:
            messagebox.showerror("Bible", f"Unable to open the license note.\n\n{error}", parent=tab_frame.winfo_toplevel())

    def open_licenses_folder() -> None:
        """What: Open the shared Bibles/licenses directory.

        Why included: The user-maintained provenance notes need an accessible file location.
        Where used: Open Licenses button command.
        Returns: None; reports OS launch failures in a dialog.
        """
        try:
            os.startfile(str(licenses_directory))
        except Exception as error:
            messagebox.showerror("Bible", f"Unable to open the licenses folder.\n\n{error}", parent=tab_frame.winfo_toplevel())

    def open_extracted_folder() -> None:
        """What: Open the extracted sibling folder for the selected ZIP Bible pack.

        Why included: Users may inspect pack contents that are not yet converted by the app.
        Where used: Open Extracted button command.
        Returns: None; informs the user when no folder exists and reports launch errors.
        """
        selected_path = get_selected_file_path()
        extracted_directory = get_extracted_directory_path(selected_path)
        if extracted_directory is None or not extracted_directory.exists() or not extracted_directory.is_dir():
            messagebox.showinfo("Bible", "No extracted folder was found for the selected Bible pack.", parent=tab_frame.winfo_toplevel())
            return
        try:
            os.startfile(str(extracted_directory))
        except Exception as error:
            messagebox.showerror("Bible", f"Unable to open the extracted folder.\n\n{error}", parent=tab_frame.winfo_toplevel())

    def open_discoveries_note() -> None:
        """What: Open the text note listing online Bible-version discoveries.

        Why included: Discovery provenance/status is maintained in a human-readable note.
        Where used: Open Discovery Note button command.
        Returns: None; reports a missing note or OS launch failure.
        """
        note_path = get_discoveries_note_path()
        if not note_path.exists():
            messagebox.showinfo("Bible", "No discovery note was found.", parent=tab_frame.winfo_toplevel())
            return
        try:
            os.startfile(str(note_path))
        except Exception as error:
            messagebox.showerror("Bible", f"Unable to open the discovery note.\n\n{error}", parent=tab_frame.winfo_toplevel())

    import_button.configure(command=import_bible_files)
    refresh_button.configure(command=lambda: refresh_bible_files())
    import_url_button.configure(command=download_bible_from_url)
    open_folder_button.configure(command=open_bibles_folder)
    open_license_button.configure(command=open_selected_license_record)
    open_licenses_button.configure(command=open_licenses_folder)
    open_extracted_button.configure(command=open_extracted_folder)
    save_license_button.configure(command=save_selected_license_status, state="disabled")
    create_license_button.configure(command=create_selected_license_record, state="disabled")
    open_discoveries_button.configure(command=open_discoveries_note, state="disabled")
    files_listbox.bind("<<ListboxSelect>>", handle_file_selection, add="+")
    discovery_listbox.bind("<<ListboxSelect>>", handle_discovery_selection, add="+")

    widgets = [
        library_column,
        library_title,
        action_row,
        import_button,
        refresh_button,
        import_url_button,
        open_folder_button,
        files_wrapper,
        files_listbox,
        files_scrollbar,
        details_column,
        details_title,
        selected_name_label,
        selected_type_label,
        selected_size_label,
        selected_path_label,
        selected_source_label,
        source_provider_entry,
        selected_source_hint_label,
        official_source_page_entry,
        selected_license_label,
        selected_extracted_label,
        source_verified_checkbutton,
        details_action_row,
        open_license_button,
        open_licenses_button,
        open_extracted_button,
        save_license_button,
        create_license_button,
        notes_label,
        help_column,
        help_title,
        help_body,
        discovery_title,
        discovery_wrapper,
        discovery_listbox,
        discovery_scrollbar,
        discovered_publisher_label,
        discovered_status_label,
        open_discoveries_button,
    ]
    labels = [
        library_title,
        details_title,
        selected_name_label,
        selected_type_label,
        selected_size_label,
        selected_path_label,
        selected_source_label,
        selected_source_hint_label,
        selected_source_hint_label,
        selected_license_label,
        selected_extracted_label,
        notes_label,
        help_title,
        help_body,
        discovery_title,
        discovered_publisher_label,
        discovered_status_label,
    ]
    inputs = [import_button, refresh_button, import_url_button, open_folder_button, open_license_button, open_licenses_button, open_extracted_button, save_license_button, create_license_button, open_discoveries_button, files_listbox, discovery_listbox, source_verified_checkbutton, source_provider_entry, official_source_page_entry]

    tab_widgets.extend(widgets)
    tab_labels.extend(labels)
    if isinstance(tab_inputs, list):
        tab_inputs.extend(inputs)
    apply_layout_style(
        widgets,
        label_widgets=labels,
        input_widgets=inputs,
    )

    refresh_bible_files()
    refresh_discovered_versions()
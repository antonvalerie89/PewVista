"""Store member contact/birthday records and expose them to the member popup."""

from __future__ import annotations

import secure_db
from datetime import date, datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import messagebox
from tkinter import ttk

from app_paths import get_app_base_path, get_app_data_path


def get_members_db_path() -> Path:
    """Resolve the writable SQLite database path for member records.

    What: Places members.db under the application's Data directory.
    Why included: Member data follows the same source/frozen storage root as other app data.
    Where used: Schema setup and all member database operations.
    Returns: Absolute Path to the member database file.
    """
    path = get_app_data_path() / "members.db"
    secure_db.ensure_encrypted(path)
    return path


def ensure_members_schema() -> None:
    """Create the member table and its contact, birthdate, and status columns.

    What: Ensures the Data directory and members table exist without replacing records.
    Why included: New installs and upgraded app builds need a usable persistence schema.
    Where used: Before member reads and CRUD operations.
    Returns: None; commits schema creation to SQLite.
    """
    db_path = get_members_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with secure_db.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL DEFAULT '',
                surname TEXT NOT NULL DEFAULT '',
                phone_number TEXT NOT NULL DEFAULT '',
                address TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                date_of_birth TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        connection.commit()


def load_all_members() -> list[dict[str, object]]:
    """Load all member records ordered by surname, given name, and ID.

    What: Ensures schema, queries all contact and lifecycle fields, and converts rows
    to dictionaries for database-independent UI use.
    Why included: Popup filtering and birthday calculations consume a common record shape.
    Where used: Member popup list refresh and upcoming-birthday lookup.
    Returns: Ordered list of member mappings.
    """
    ensure_members_schema()
    with secure_db.connect(get_members_db_path()) as connection:
        connection.row_factory = secure_db.Row
        rows = connection.execute(
            """
            SELECT id, name, surname, phone_number, address, email, date_of_birth, created_at, updated_at, is_active
            FROM members
            ORDER BY LOWER(surname), LOWER(name), id
            """
        ).fetchall()
    results: list[dict[str, object]] = []
    for row in rows:
        results.append(
            {
                "id": row["id"],
                "name": row["name"],
                "surname": row["surname"],
                "phone_number": row["phone_number"],
                "address": row["address"],
                "email": row["email"],
                "date_of_birth": row["date_of_birth"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "is_active": row["is_active"],
            }
        )
    return results


def parse_member_date(value: str) -> date | None:
    """Parse a stored birthdate in the supported ISO or day-first formats.

    What: Tries ISO, numeric day-first, abbreviated-month, and full-month formats.
    Why included: Existing member records may use more than one date convention.
    Where used: get_upcoming_birthdays() when evaluating member birthdates.
    Returns: Parsed date, or None for empty/unsupported text.
    """
    raw_value = str(value or "").strip()
    if not raw_value:
        return None
    for date_format in ("%Y-%m-%d", "%d/%m/%Y", "%d %b %Y", "%d %B %Y"):
        try:
            # Try the established storage/display formats in order; one bad parse should not block later formats.
            if date_format == "%Y-%m-%d":
                return date.fromisoformat(raw_value)
            return datetime.strptime(raw_value, date_format).date()
        except Exception:
            continue
    return None


def get_upcoming_birthdays(reference_date: date, days_ahead: int = 7) -> list[dict[str, object]]:
    """Return active members whose next birthday falls within the date window.

    What: Computes each active member's next birthday, handles leap day as February 28
    in non-leap years, filters the inclusive date window, and sorts the results.
    Why included: Welcome content can announce birthdays coming up during a service week.
    Where used: Welcome-tab upcoming birthday content.
    Returns: Ordered mappings with birthday, name, and surname fields.
    """
    start_date = reference_date if isinstance(reference_date, date) else date.today()
    window_days = max(1, int(days_ahead))
    end_date = start_date + timedelta(days=window_days - 1)
    upcoming: list[dict[str, object]] = []

    for member in load_all_members():
        if not bool(member.get("is_active", 1)):
            # Inactive records stay stored but should not appear in service announcements.
            continue
        birth_date = parse_member_date(str(member.get("date_of_birth") or ""))
        if birth_date is None:
            # Ignore incomplete/legacy date values so one member cannot abort the whole list.
            continue

        try:
            next_birthday = date(start_date.year, birth_date.month, birth_date.day)
        except ValueError:
            if birth_date.month == 2 and birth_date.day == 29:
                # Use February 28 when the current year has no February 29.
                next_birthday = date(start_date.year, 2, 28)
            else:
                continue
        if next_birthday < start_date:
            try:
                next_birthday = date(start_date.year + 1, birth_date.month, birth_date.day)
            except ValueError:
                if birth_date.month == 2 and birth_date.day == 29:
                    # Apply the same leap-day policy after rolling into the next year.
                    next_birthday = date(start_date.year + 1, 2, 28)
                else:
                    continue

        if start_date <= next_birthday <= end_date:
            upcoming.append(
                {
                    "birthday": next_birthday,
                    "name": str(member.get("name") or "").strip(),
                    "surname": str(member.get("surname") or "").strip(),
                }
            )

    upcoming.sort(key=lambda item: (item["birthday"], str(item.get("surname") or "").casefold(), str(item.get("name") or "").casefold()))
    return upcoming


def find_duplicate_member_id(name: str, surname: str, date_of_birth: str, exclude_member_id: int | None = None) -> int | None:
    """Find a member with matching normalized name, surname, and birthdate.

    What: Queries trimmed, case-insensitive identity fields and optionally excludes
    the record being edited.
    Why included: Member create/update must prevent duplicate identity records.
    Where used: ensure_member_not_duplicated().
    Returns: Matching database ID, or None when required identity values are blank
    or no row matches.
    """
    ensure_members_schema()
    normalized_name = name.strip().lower()
    normalized_surname = surname.strip().lower()
    normalized_date_of_birth = date_of_birth.strip().lower()
    if not normalized_name or not normalized_surname or not normalized_date_of_birth:
        return None

    query = [
        """
        SELECT id
        FROM members
        WHERE LOWER(TRIM(name)) = ?
          AND LOWER(TRIM(surname)) = ?
          AND LOWER(TRIM(date_of_birth)) = ?
        """
    ]
    parameters: list[object] = [normalized_name, normalized_surname, normalized_date_of_birth]
    if exclude_member_id is not None:
        query.append("AND id != ?")
        parameters.append(int(exclude_member_id))
    query.append("LIMIT 1")

    with secure_db.connect(get_members_db_path()) as connection:
        row = connection.execute("\n".join(query), tuple(parameters)).fetchone()
    return int(row[0]) if row else None


def ensure_member_not_duplicated(name: str, surname: str, date_of_birth: str, exclude_member_id: int | None = None) -> None:
    """Reject a duplicate member identity before a database write.

    What: Uses find_duplicate_member_id() with an optional edit-row exclusion.
    Why included: Duplicate identity fields should not create parallel member records.
    Where used: add_member_record() and update_member_record().
    Returns: None; raises ValueError when another member matches.
    """
    duplicate_member_id = find_duplicate_member_id(name, surname, date_of_birth, exclude_member_id=exclude_member_id)
    if duplicate_member_id is not None:
        raise ValueError("A member with the same name, surname, and date of birth already exists.")


def validate_phone_number_value(phone_number: str) -> bool:
    """Check that a phone value contains only digits and an optional leading plus.

    What: Allows blank input, rejects multiple plus signs or a non-leading plus, and
    validates every remaining character as a digit.
    Why included: The popup validates phone text before accepting member edits.
    Where used: Tk key validation and ensure_valid_phone_number().
    Returns: True when valid, otherwise False.
    """
    cleaned_value = str(phone_number or "").strip()
    if not cleaned_value:
        return True
    if cleaned_value.count("+") > 1:
        return False
    if "+" in cleaned_value and not cleaned_value.startswith("+"):
        return False
    return all(character.isdigit() or character == "+" for character in cleaned_value)


def ensure_valid_phone_number(phone_number: str) -> str:
    """Normalize a phone number and raise for unsupported characters.

    What: Trims text and delegates format validation to validate_phone_number_value().
    Why included: Database CRUD APIs must enforce the same rule as the entry widget.
    Where used: add_member_record() and update_member_record().
    Returns: Trimmed phone number; raises ValueError when invalid.
    """
    normalized_phone_number = str(phone_number or "").strip()
    if not validate_phone_number_value(normalized_phone_number):
        raise ValueError("Phone number may contain only numbers and an optional leading +.")
    return normalized_phone_number


def add_member_record(name: str, surname: str, phone_number: str, address: str, email: str, date_of_birth: str) -> int:
    """Validate and insert a new active member record.

    What: Ensures schema, checks duplicate identity and phone format, writes all contact
    fields with matching timestamps, and marks the member active.
    Why included: The member manager needs one persistence boundary for validated inserts.
    Where used: Add Member action in open_members_popup().
    Returns: Newly inserted database ID; raises ValueError for invalid input.
    """
    ensure_members_schema()
    ensure_member_not_duplicated(name, surname, date_of_birth)
    phone_number = ensure_valid_phone_number(phone_number)
    timestamp = datetime.now().replace(microsecond=0).isoformat()
    with secure_db.connect(get_members_db_path()) as connection:
        cursor = connection.execute(
            """
            INSERT INTO members (name, surname, phone_number, address, email, date_of_birth, created_at, updated_at, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (name, surname, phone_number, address, email, date_of_birth, timestamp, timestamp, 1),
        )
        connection.commit()
        return int(cursor.lastrowid)


def update_member_record(member_id: int, name: str, surname: str, phone_number: str, address: str, email: str, date_of_birth: str) -> None:
    """Validate and update one member's identity and contact fields.

    What: Excludes the current ID from duplicate checks, validates phone text, and
    updates the record timestamp and editable fields.
    Why included: Edits need the same validation as inserts without self-matching.
    Where used: Update Member action in open_members_popup().
    Returns: None; raises ValueError when validation fails.
    """
    ensure_members_schema()
    ensure_member_not_duplicated(name, surname, date_of_birth, exclude_member_id=member_id)
    phone_number = ensure_valid_phone_number(phone_number)
    timestamp = datetime.now().replace(microsecond=0).isoformat()
    with secure_db.connect(get_members_db_path()) as connection:
        connection.execute(
            """
            UPDATE members
            SET name = ?, surname = ?, phone_number = ?, address = ?, email = ?, date_of_birth = ?, updated_at = ?
            WHERE id = ?
            """,
            (name, surname, phone_number, address, email, date_of_birth, timestamp, member_id),
        )
        connection.commit()


def update_member_active_status(member_id: int, is_active: bool) -> None:
    """Set a member's active flag and update its modification timestamp.

    What: Stores the boolean as SQLite 1/0 for the specified member ID.
    Why included: Inactive members remain stored but are omitted from birthday announcements.
    Where used: Status-dot click handler in the member popup.
    Returns: None; commits the update.
    """
    ensure_members_schema()
    timestamp = datetime.now().replace(microsecond=0).isoformat()
    with secure_db.connect(get_members_db_path()) as connection:
        connection.execute(
            """
            UPDATE members
            SET is_active = ?, updated_at = ?
            WHERE id = ?
            """,
            (1 if is_active else 0, timestamp, member_id),
        )
        connection.commit()


def delete_member_record(member_id: int) -> None:
    """Permanently delete one member row by its database ID.

    What: Ensures the table exists and deletes the matching ID.
    Why included: The member manager provides an explicit remove workflow.
    Where used: Confirmed Remove Member action in open_members_popup().
    Returns: None; commits the deletion.
    """
    ensure_members_schema()
    with secure_db.connect(get_members_db_path()) as connection:
        connection.execute("DELETE FROM members WHERE id = ?", (member_id,))
        connection.commit()


def open_members_popup(parent: tk.Misc, apply_layout_style, get_popup_theme=None, open_date_picker_callback=None) -> tk.Toplevel:
    """Build the member search, detail-edit, and CRUD popup.

    What: Creates the searchable member list, editor fields, active status controls,
    theme-aware styles, validation, and database actions.
    Why included: Member records are managed in a focused dialog separate from service tabs.
    Where used: Members command in the main menu via main.open_members_window().
    Returns: New Toplevel window for the caller's singleton lifecycle management.
    """
    popup = tk.Toplevel(parent)
    popup.title("Members")
    popup.minsize(980, 620)
    popup.transient(parent.winfo_toplevel())
    popup.columnconfigure(0, weight=1)
    popup.rowconfigure(0, weight=1)

    container = tk.Frame(popup, padx=12, pady=12)
    container.grid(row=0, column=0, sticky="nsew")
    container.columnconfigure(0, weight=3)
    container.columnconfigure(1, weight=2)
    container.rowconfigure(0, weight=1)

    popup_widgets: list[tk.Widget] = [container]
    popup_labels: list[tk.Widget] = []
    popup_inputs: list[tk.Widget] = []

    state: dict[str, object] = {
        "members": [],
        "filtered_member_ids": [],
        "selected_member_id": None,
    }

    search_text_var = tk.StringVar()
    name_var = tk.StringVar()
    surname_var = tk.StringVar()
    phone_number_var = tk.StringVar()
    address_var = tk.StringVar()
    email_var = tk.StringVar()
    date_of_birth_var = tk.StringVar()

    def resolve_popup_theme() -> dict[str, str]:
        """What: Read the injected theme or return a complete neutral fallback palette.

        Why included: The member popup is reusable without requiring main-window context.
        Where used: Treeview styling and popup-specific colors.
        Returns: Mapping of required window, panel, text, border, accent, menu, and tab colors.
        """
        if callable(get_popup_theme):
            try:
                theme = get_popup_theme()
                if isinstance(theme, dict):
                    return theme
            except Exception:
                pass
        return {
            "window_bg": "#eef1f5",
            "panel_bg": "#ffffff",
            "border": "#c8d0db",
            "text": "#16202a",
            "accent": "#255fa8",
            "menu_bg": "#dfeaf7",
            "menu_fg": "#0e2238",
            "tabs_bg": "#d7e3f3",
            "tabs_fg": "#16202a",
        }

    def validate_phone_number_input(proposed_value: str) -> bool:
        """What: Adapt phone validation to Tk's proposed-value validation callback.

        Why included: Invalid characters should be rejected as the user types.
        Where used: Registered as the Phone Number Entry validatecommand.
        Returns: True when the proposed entry text is allowed.
        """
        return validate_phone_number_value(proposed_value)

    phone_number_validator = popup.register(validate_phone_number_input)

    def open_birthdate_picker(_event=None) -> str | None:
        """What: Open the injected date picker for the member birthdate field.

        Why included: Birthdate input should use the app's shared calendar UI.
        Where used: Mouse-click and focus-in bindings on the read-only date entry.
        Returns: Tk "break" after opening; None when no picker callback was provided.
        """
        if not callable(open_date_picker_callback):
            return None
        open_date_picker_callback(popup, date_of_birth_var)
        return "break"

    def configure_members_treeview_style(treeview: ttk.Treeview) -> None:
        """What: Apply themed row, selection, and heading styles to the member list.

        Why included: ttk Treeview colors are controlled through named styles, not the
        ordinary widget palette callback.
        Where used: Immediately after constructing the Members Treeview.
        Returns: None; configures widget style and odd/even row tags.
        """
        theme = resolve_popup_theme()
        style = ttk.Style(treeview)
        style_name = "MembersPopup.Treeview"
        heading_style_name = "MembersPopup.Treeview.Heading"
        style.configure(
            style_name,
            background="#ffffff",
            fieldbackground="#ffffff",
            foreground=theme["text"],
            bordercolor=theme["border"],
            lightcolor=theme["border"],
            darkcolor=theme["border"],
            rowheight=26,
        )
        style.map(
            style_name,
            background=[("selected", theme["accent"])],
            foreground=[("selected", theme["menu_fg"])],
        )
        style.configure(
            heading_style_name,
            background=theme["menu_bg"],
            foreground=theme["menu_fg"],
            relief="flat",
        )
        style.map(
            heading_style_name,
            background=[("active", theme["accent"])],
            foreground=[("active", theme["menu_fg"])],
        )
        treeview.configure(style=style_name)
        treeview.tag_configure("oddrow", background=theme["panel_bg"], foreground=theme["text"])
        treeview.tag_configure("evenrow", background=theme["tabs_bg"], foreground=theme["text"])

    def create_status_indicator_image(color: str) -> tk.PhotoImage:
        """What: Draw a small circular status indicator in the requested color.

        Why included: Active state needs a compact visual marker in the list's tree column.
        Where used: Creates the active and inactive images stored in status_images.
        Returns: Tk PhotoImage retained for Treeview row rendering.
        """
        image = tk.PhotoImage(width=12, height=12)
        image.put("", to=(0, 0, 12, 12))
        circle_pixels = [
            (4, 0), (5, 0), (6, 0), (7, 0),
            (2, 1), (3, 1), (4, 1), (5, 1), (6, 1), (7, 1), (8, 1), (9, 1),
            (1, 2), (2, 2), (3, 2), (4, 2), (5, 2), (6, 2), (7, 2), (8, 2), (9, 2), (10, 2),
            (1, 3), (2, 3), (3, 3), (4, 3), (5, 3), (6, 3), (7, 3), (8, 3), (9, 3), (10, 3),
            (0, 4), (1, 4), (2, 4), (3, 4), (4, 4), (5, 4), (6, 4), (7, 4), (8, 4), (9, 4), (10, 4), (11, 4),
            (0, 5), (1, 5), (2, 5), (3, 5), (4, 5), (5, 5), (6, 5), (7, 5), (8, 5), (9, 5), (10, 5), (11, 5),
            (0, 6), (1, 6), (2, 6), (3, 6), (4, 6), (5, 6), (6, 6), (7, 6), (8, 6), (9, 6), (10, 6), (11, 6),
            (0, 7), (1, 7), (2, 7), (3, 7), (4, 7), (5, 7), (6, 7), (7, 7), (8, 7), (9, 7), (10, 7), (11, 7),
            (1, 8), (2, 8), (3, 8), (4, 8), (5, 8), (6, 8), (7, 8), (8, 8), (9, 8), (10, 8),
            (1, 9), (2, 9), (3, 9), (4, 9), (5, 9), (6, 9), (7, 9), (8, 9), (9, 9), (10, 9),
            (2, 10), (3, 10), (4, 10), (5, 10), (6, 10), (7, 10), (8, 10), (9, 10),
            (4, 11), (5, 11), (6, 11), (7, 11),
        ]
        for x_coord, y_coord in circle_pixels:
            image.put(color, (x_coord, y_coord))
        return image

    status_images = {
        "active": create_status_indicator_image("#1f7a1f"),
        "inactive": create_status_indicator_image("#b42318"),
    }

    list_frame = tk.Frame(container, bd=1, relief="solid", padx=12, pady=12)
    list_frame.grid(row=0, column=0, sticky="nsew", padx=(8, 10), pady=8)
    list_frame.columnconfigure(0, weight=1)
    list_frame.rowconfigure(2, weight=1)

    right_stack = tk.Frame(container)
    right_stack.grid(row=0, column=1, sticky="nsew", padx=(10, 8), pady=8)
    right_stack.columnconfigure(0, weight=1)
    right_stack.rowconfigure(0, weight=1)

    details_frame = tk.Frame(right_stack, bd=1, relief="solid", padx=12, pady=12)
    details_frame.grid(row=0, column=0, sticky="nsew")
    details_frame.columnconfigure(0, weight=1)
    details_frame.rowconfigure(1, weight=1)

    actions_frame = tk.Frame(right_stack, bd=1, relief="solid", padx=12, pady=12)
    actions_frame.grid(row=1, column=0, sticky="ew", pady=(12, 0))
    actions_frame.columnconfigure(0, weight=1)

    title = tk.Label(list_frame, text="Members List", justify="center", anchor="center")
    title.grid(row=0, column=0, sticky="ew", pady=(0, 8))
    search_entry = tk.Entry(list_frame, textvariable=search_text_var)
    search_entry.grid(row=1, column=0, sticky="ew", pady=(0, 8))
    list_host = tk.Frame(list_frame)
    list_host.grid(row=2, column=0, sticky="nsew")
    list_host.columnconfigure(0, weight=1)
    list_host.rowconfigure(0, weight=1)
    members_columns = ("name", "surname", "phone", "address", "email", "dob")
    members_list = ttk.Treeview(list_frame, columns=members_columns, show="tree headings", selectmode="browse")
    members_list.heading("#0", text="Status")
    members_list.heading("name", text="Name")
    members_list.heading("surname", text="Surname")
    members_list.heading("phone", text="Phone")
    members_list.heading("address", text="Address")
    members_list.heading("email", text="Email")
    members_list.heading("dob", text="Date Of Birth")
    members_list.column("#0", width=72, minwidth=72, stretch=False, anchor="center")
    members_list.column("name", width=135, anchor="w")
    members_list.column("surname", width=135, anchor="w")
    members_list.column("phone", width=125, anchor="w")
    members_list.column("address", width=210, anchor="w")
    members_list.column("email", width=210, anchor="w")
    members_list.column("dob", width=120, anchor="center")
    members_scrollbar = tk.Scrollbar(list_host, orient="vertical", command=members_list.yview)
    members_scrollbar.grid(row=0, column=1, sticky="ns")
    members_list.configure(yscrollcommand=members_scrollbar.set)
    members_list.grid(in_=list_host, row=0, column=0, sticky="nsew")
    configure_members_treeview_style(members_list)
    popup_widgets.extend([list_frame, title, search_entry, list_host, members_scrollbar])
    popup_labels.append(title)
    popup_inputs.extend([search_entry])

    title = tk.Label(details_frame, text="Member Details", justify="center", anchor="center")
    title.grid(row=0, column=0, sticky="ew", pady=(0, 8))
    form = tk.Frame(details_frame)
    form.grid(row=1, column=0, sticky="nsew")
    form.columnconfigure(1, weight=1)
    details_widgets: list[tk.Widget] = [form]
    details_labels: list[tk.Widget] = []
    details_inputs: list[tk.Widget] = []
    field_specs = [
        ("Name", name_var),
        ("Surname", surname_var),
        ("Phone Number", phone_number_var),
        ("Address", address_var),
        ("Email", email_var),
        ("Date Of Birth", date_of_birth_var),
    ]
    for row_index, (label_text, variable) in enumerate(field_specs):
        field_label = tk.Label(form, text=label_text)
        field_label.grid(row=row_index, column=0, sticky="w", padx=(0, 8), pady=4)
        if label_text == "Phone Number":
            field_entry = tk.Entry(form, textvariable=variable, validate="key", validatecommand=(phone_number_validator, "%P"))
        elif label_text == "Date Of Birth":
            field_entry = tk.Entry(form, textvariable=variable, state="readonly", readonlybackground="#ffffff")
            field_entry.bind("<Button-1>", open_birthdate_picker)
            field_entry.bind("<FocusIn>", open_birthdate_picker)
            try:
                field_entry.configure(cursor="hand2")
            except tk.TclError:
                pass
        else:
            field_entry = tk.Entry(form, textvariable=variable)
        field_entry.grid(row=row_index, column=1, sticky="ew", pady=4)
        details_widgets.extend([field_label, field_entry])
        details_labels.append(field_label)
        details_inputs.append(field_entry)
    popup_widgets.extend([right_stack, details_frame, title, *details_widgets])
    popup_labels.extend([title, *details_labels])
    popup_inputs.extend(details_inputs)

    title = tk.Label(actions_frame, text="Members Actions", justify="center", anchor="center")
    title.grid(row=0, column=0, sticky="ew", pady=(0, 8))
    controls = tk.Frame(actions_frame)
    controls.grid(row=1, column=0, sticky="ew")
    controls.columnconfigure(0, weight=1)
    update_button = tk.Button(controls, text="Update Member")
    update_button.grid(row=0, column=0, sticky="ew", pady=(0, 8))
    add_button = tk.Button(controls, text="Add Member")
    add_button.grid(row=1, column=0, sticky="ew", pady=(0, 8))
    clear_button = tk.Button(controls, text="New Member")
    clear_button.grid(row=2, column=0, sticky="ew", pady=(0, 8))
    remove_button = tk.Button(controls, text="Remove Member")
    remove_button.grid(row=3, column=0, sticky="ew", pady=(0, 8))
    status = tk.Label(controls, text="No member selected.", justify="left", anchor="w")
    status.grid(row=4, column=0, sticky="ew", pady=(8, 0))
    status_hint = tk.Label(controls, text="Click the status dot to switch Active/Inactive.", justify="left", anchor="w")
    status_hint.grid(row=5, column=0, sticky="ew", pady=(8, 0))
    popup_widgets.extend([actions_frame, title, controls, update_button, add_button, clear_button, remove_button, status, status_hint])
    popup_labels.extend([title, status, status_hint])
    popup_inputs.extend([update_button, add_button, clear_button, remove_button])

    apply_layout_style(popup_widgets, label_widgets=popup_labels, input_widgets=popup_inputs)

    def get_member_by_id(member_id: int | None) -> dict[str, object] | None:
        """What: Find the currently cached member mapping for a database ID.

        Why included: Form and selection callbacks need consistent access to refreshed data.
        Where used: List refresh, selection, active toggle, add, and update handlers.
        Returns: Matching member mapping, or None when ID is absent/not cached.
        """
        if member_id is None:
            return None
        for member in state["members"]:
            if int(member["id"]) == int(member_id):
                return member
        return None

    def clear_form(reset_selection: bool = True) -> None:
        """What: Clear all member editor variables and optionally clear list selection.

        Why included: New-member and delete workflows must not leave stale details visible.
        Where used: New Member button, successful deletion, and empty form initialization.
        Returns: None; updates editor variables, selection state, and status text.
        """
        if reset_selection:
            state["selected_member_id"] = None
            for selected_item in members_list.selection():
                members_list.selection_remove(selected_item)
        name_var.set("")
        surname_var.set("")
        phone_number_var.set("")
        address_var.set("")
        email_var.set("")
        date_of_birth_var.set("")
        status.configure(text="No member selected.")

    def populate_form(member: dict[str, object] | None) -> None:
        """What: Populate editable fields and status text from one member record.

        Why included: Selecting a row should load its current saved values for review/edit.
        Where used: List selection, add/update completion, and status changes.
        Returns: None; clears field values when member is None.
        """
        if member is None:
            clear_form(reset_selection=False)
            return
        name_var.set(str(member.get("name") or ""))
        surname_var.set(str(member.get("surname") or ""))
        phone_number_var.set(str(member.get("phone_number") or ""))
        address_var.set(str(member.get("address") or ""))
        email_var.set(str(member.get("email") or ""))
        date_of_birth_var.set(str(member.get("date_of_birth") or ""))
        current_status = "Active" if bool(member.get("is_active", 1)) else "Inactive"
        status.configure(text=f"Selected Member: {member.get('name') or ''} {member.get('surname') or ''} | Status: {current_status}".strip())

    def get_member_row_tag(row_index: int) -> str:
        """What: Choose the alternating row tag from a zero-based row index.

        Why included: Zebra striping improves scanning across the wide contact table.
        Where used: refresh_members_list() when inserting Treeview rows.
        Returns: evenrow for even indices, otherwise oddrow.
        """
        return "evenrow" if row_index % 2 == 0 else "oddrow"

    def refresh_members_list(*_args) -> None:
        """What: Reload records, apply the search filter, rebuild rows, and restore selection.

        Why included: Search changes and CRUD operations must keep the visible list in sync.
        Where used: Search StringVar trace, member actions, and initial popup setup.
        Returns: None; updates cached member IDs and Treeview rows in place.
        """
        members = load_all_members()
        state["members"] = members
        search_term = search_text_var.get().strip().lower()
        filtered = [
            member
            for member in members
            if not search_term
            or search_term in str(member.get("name") or "").lower()
            or search_term in str(member.get("surname") or "").lower()
            or search_term in str(member.get("phone_number") or "").lower()
            or search_term in str(member.get("address") or "").lower()
            or search_term in str(member.get("email") or "").lower()
        ]
        state["filtered_member_ids"] = [int(member["id"]) for member in filtered]
        for item_id in members_list.get_children():
            members_list.delete(item_id)
        for row_index, member in enumerate(filtered):
            member_id = int(member["id"])
            status_key = "active" if bool(member.get("is_active", 1)) else "inactive"
            members_list.insert(
                "",
                "end",
                iid=str(member_id),
                text="o",
                image=status_images[status_key],
                values=(
                    str(member.get("name") or ""),
                    str(member.get("surname") or ""),
                    str(member.get("phone_number") or ""),
                    str(member.get("address") or ""),
                    str(member.get("email") or ""),
                    str(member.get("date_of_birth") or ""),
                ),
                tags=(get_member_row_tag(row_index),),
            )

        selected_member = get_member_by_id(state.get("selected_member_id"))
        if selected_member is None:
            return
        selected_id = int(selected_member["id"])
        if selected_id not in state["filtered_member_ids"]:
            return
        member_iid = str(selected_id)
        if members_list.exists(member_iid):
            members_list.selection_set(member_iid)
            members_list.focus(member_iid)
            members_list.see(member_iid)

    def select_member_from_list(_event=None) -> None:
        """What: Resolve the selected Treeview ID and populate its member details.

        Why included: Row selection is the entry point for editing and status actions.
        Where used: Members Treeview <<TreeviewSelect>> binding.
        Returns: None; ignores empty or non-integer selections.
        """
        selection = members_list.selection()
        if not selection:
            return
        try:
            member_id = int(selection[0])
        except (TypeError, ValueError):
            return
        state["selected_member_id"] = member_id
        populate_form(get_member_by_id(member_id))

    def toggle_member_status_from_list(_event=None):
        """What: Toggle Active/Inactive when the status column is clicked.

        Why included: Status changes should be available directly in the member list.
        Where used: Members Treeview <Button-1> binding.
        Returns: "break" when the status column handled the click; otherwise None.
        """
        row_id = members_list.identify_row(_event.y) if _event is not None else ""
        column_id = members_list.identify_column(_event.x) if _event is not None else ""
        if not row_id or column_id != "#0":
            return None
        try:
            member_id = int(row_id)
        except (TypeError, ValueError):
            return None
        member = get_member_by_id(member_id)
        if member is None:
            return "break"
        next_status = not bool(member.get("is_active", 1))
        update_member_active_status(member_id, next_status)
        state["selected_member_id"] = member_id
        refresh_members_list()
        updated_member = get_member_by_id(member_id)
        populate_form(updated_member)
        status.configure(text=f"Member status changed to {'Active' if next_status else 'Inactive'}.")
        return "break"

    def add_member() -> None:
        """What: Validate editor fields, insert a member, and refresh/select the new row.

        Why included: The Add Member control owns validation feedback and form synchronization.
        Where used: Add Member button command.
        Returns: None; shows a warning on invalid input and updates status on success.
        """
        name = name_var.get().strip()
        surname = surname_var.get().strip()
        if not name and not surname:
            messagebox.showwarning("Members", "Enter at least a name or surname before adding a member.", parent=popup)
            return
        try:
            member_id = add_member_record(name, surname, phone_number_var.get().strip(), address_var.get().strip(), email_var.get().strip(), date_of_birth_var.get().strip())
        except ValueError as exc:
            messagebox.showwarning("Members", str(exc), parent=popup)
            return
        state["selected_member_id"] = member_id
        refresh_members_list()
        populate_form(get_member_by_id(member_id))
        status.configure(text="Member added.")

    def update_member() -> None:
        """What: Validate the selected record's edited fields and save the changes.

        Why included: Prevents updates without a selected row or with invalid identity data.
        Where used: Update Member button command.
        Returns: None; reports validation issues or refreshes the updated selection.
        """
        member_id = state.get("selected_member_id")
        if member_id is None:
            messagebox.showinfo("Members", "Select a member before updating.", parent=popup)
            return
        name = name_var.get().strip()
        surname = surname_var.get().strip()
        if not name and not surname:
            messagebox.showwarning("Members", "Enter at least a name or surname before updating a member.", parent=popup)
            return
        try:
            update_member_record(int(member_id), name, surname, phone_number_var.get().strip(), address_var.get().strip(), email_var.get().strip(), date_of_birth_var.get().strip())
        except ValueError as exc:
            messagebox.showwarning("Members", str(exc), parent=popup)
            return
        refresh_members_list()
        populate_form(get_member_by_id(member_id))
        status.configure(text="Member updated.")

    def remove_member() -> None:
        """What: Confirm permanent deletion of the selected member and refresh the list.

        Why included: Removal is destructive and must be explicitly confirmed by the user.
        Where used: Remove Member button command.
        Returns: None; leaves state unchanged if no record is selected or confirmed.
        """
        member_id = state.get("selected_member_id")
        if member_id is None:
            messagebox.showinfo("Members", "Select a member before removing.", parent=popup)
            return
        member = get_member_by_id(member_id)
        member_name = " ".join(part for part in [str(member.get("name") or "") if member else "", str(member.get("surname") or "") if member else ""] if part).strip() or "this member"
        if not messagebox.askyesno("Members", f"Remove '{member_name}'?", parent=popup):
            return
        delete_member_record(int(member_id))
        clear_form(reset_selection=True)
        refresh_members_list()
        status.configure(text="Member removed.")

    search_text_var.trace_add("write", refresh_members_list)
    members_list.bind("<Button-1>", toggle_member_status_from_list, add="+")
    members_list.bind("<<TreeviewSelect>>", select_member_from_list)
    add_button.configure(command=add_member)
    update_button.configure(command=update_member)
    remove_button.configure(command=remove_member)
    clear_button.configure(command=lambda: clear_form(reset_selection=True))

    clear_form(reset_selection=True)
    refresh_members_list()
    popup.focus_force()
    return popup

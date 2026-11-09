"""Publisher tool: create PewVista license codes for church PCs with a simple window.

Uses the private key in "%USERPROFILE%\\PewVista Signing"; never give this tool or the key to customers.
"""

from __future__ import annotations

import re
import shutil
import tkinter as tk
import zipfile
from datetime import date, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import licensing

PROJECT_ROOT = Path(__file__).resolve().parent
RECORDS_PATH = PROJECT_ROOT / "license_records.txt"
DURATIONS = {"1 month (31 days)": 31, "3 months (92 days)": 92, "6 months (183 days)": 183, "1 year (366 days)": 366}
RESTORE_BAT = '@echo off\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0restore_pewvista_backup.ps1"\r\n'
README_TEXT = """PEWVISTA BACKUP - {today}
Keep this on a USB drive and a second place. Never share it or put it on GitHub.

TO RESTORE: double-click "Restore PewVista.bat" in this folder.
It puts back your signing keys, grammar checker, and license records.
(On a new PC, download the project from GitHub to C:\\PewVista first:
 https://github.com/antonvalerie89/PewVista)

Contents:
- PewVista Signing    your private keys (license codes and update signing) - MOST IMPORTANT
- grammar.zip         grammar checker needed to build PewVista
- license_records.txt your list of license codes issued (when present)

Your church's own songs and members: in PewVista use Data > Export Databases...
and keep that ZIP next to this backup.
"""


def make_backup(parent_directory: Path) -> Path:
    """What: Create a dated, self-restoring backup folder of everything not on GitHub.

    Why included: Losing the signing keys would stop license codes and updates for every church.
    Where used: The Back Up Now button.
    Returns: The backup folder path.
    """
    keys = licensing.default_signing_key_path().parent
    if not keys.is_dir():
        raise ValueError(f"Your signing keys were not found at {keys}.")
    target = Path(parent_directory) / f"PewVista Backup {date.today().isoformat()}"
    staging = target.with_name(target.name + ".partial")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    shutil.copytree(keys, staging / "PewVista Signing")
    grammar = PROJECT_ROOT / "Resourses" / "grammar"
    if grammar.is_dir():
        with zipfile.ZipFile(staging / "grammar.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for path in grammar.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(grammar))
    if RECORDS_PATH.is_file():
        shutil.copy2(RECORDS_PATH, staging / "license_records.txt")
    shutil.copy2(PROJECT_ROOT / "scripts" / "restore_pewvista_backup.ps1", staging / "restore_pewvista_backup.ps1")
    (staging / "Restore PewVista.bat").write_bytes(RESTORE_BAT.encode("ascii"))
    (staging / "README - How to restore.txt").write_text(README_TEXT.format(today=date.today().strftime("%d %B %Y")), encoding="utf-8")
    # Merge into an existing same-day folder: replace only this tool's files and never delete
    # anything else (the publisher may have added a database export there).
    target.mkdir(parents=True, exist_ok=True)
    for item in staging.rglob("*"):
        if item.is_file():
            destination = target / item.relative_to(staging)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, destination)
    shutil.rmtree(staging, ignore_errors=True)
    return target


def create_code(pc_id: str, church: str, days: int) -> tuple[str, date]:
    """What: Validate the inputs, sign a license code, and record it.

    Why included: Keeps the window logic small and testable.
    Where used: The Create Code button.
    Returns: The code and the date it expires.
    """
    pc_id = pc_id.strip().lower()
    church = church.strip()
    if not re.fullmatch(r"[0-9a-f]{32}", pc_id):
        raise ValueError("The PC ID must be the 32 letters and numbers shown in the church's License window.")
    if not church:
        raise ValueError("Enter the church name.")
    key_path = licensing.default_signing_key_path()
    if not key_path.is_file():
        # Never create a new key here: codes from a new key are rejected by every installed PewVista.
        raise ValueError(f"Your signing key was not found at:\n{key_path}\n\nRestore the 'PewVista Signing' folder from your backup first. "
                         "Do not create a new key; codes made with a new key will not work for churches.")
    key = licensing.load_or_create_signing_key(key_path)
    if licensing.public_key_hex(key) not in licensing.TRUSTED_LICENSE_KEYS:
        raise ValueError("The signing key found does not match PewVista. Restore the original 'PewVista Signing' folder from your backup.")
    code = licensing.issue_manual_token(pc_id, church, days, private_key=key)
    expires = date.today() + timedelta(days=days)
    with RECORDS_PATH.open("a", encoding="utf-8") as records:
        records.write(f"{date.today().isoformat()}\t{church}\t{pc_id}\t{days} days\tvalid until {expires.isoformat()}\n")
    return code, expires


def main() -> None:
    root = tk.Tk()
    root.title("PewVista - Create License Code")
    icon = Path(__file__).resolve().parent / "Resourses" / "PewVista.ico"
    if icon.is_file():
        try:
            root.iconbitmap(str(icon))
        except tk.TclError:
            pass
    root.resizable(False, False)
    body = ttk.Frame(root, padding=16)
    body.pack(fill="both", expand=True)

    ttk.Label(body, text="PC ID (from the church's Help > License and Subscription > Copy ID):").grid(row=0, column=0, columnspan=2, sticky="w")
    pc_value = tk.StringVar()
    pc_entry = ttk.Entry(body, textvariable=pc_value, width=60)
    pc_entry.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 10))
    ttk.Label(body, text="Church name:").grid(row=2, column=0, columnspan=2, sticky="w")
    church_value = tk.StringVar()
    ttk.Entry(body, textvariable=church_value, width=60).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(4, 10))
    ttk.Label(body, text="License length:").grid(row=4, column=0, sticky="w")
    duration_value = tk.StringVar(value="1 month (31 days)")
    ttk.Combobox(body, textvariable=duration_value, values=list(DURATIONS), state="readonly", width=24).grid(row=4, column=1, sticky="w")

    ttk.Label(body, text="License code to send to the church:").grid(row=6, column=0, columnspan=2, sticky="w", pady=(14, 0))
    output = tk.Text(body, width=60, height=6, wrap="char", state="disabled")
    output.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(4, 0))
    status_value = tk.StringVar()
    ttk.Label(body, textvariable=status_value, foreground="#1a7f37").grid(row=8, column=0, columnspan=2, sticky="w", pady=(6, 0))

    def show_code(text: str) -> None:
        output.configure(state="normal")
        output.delete("1.0", "end")
        output.insert("1.0", text)
        output.configure(state="disabled")

    def copy_code() -> None:
        text = output.get("1.0", "end").strip()
        if text:
            root.clipboard_clear()
            root.clipboard_append(text)
            status_value.set("Copied. Paste it into your email or WhatsApp with Ctrl+V.")

    def on_create() -> None:
        try:
            code, expires = create_code(pc_value.get(), church_value.get(), DURATIONS[duration_value.get()])
        except (ValueError, licensing.LicenseError, OSError) as error:
            messagebox.showerror("Create License Code", str(error), parent=root)
            return
        show_code(code)
        copy_code()
        status_value.set(f"Code created and copied. Valid until {expires:%d %B %Y}. Saved to license_records.txt.")

    def open_records() -> None:
        if RECORDS_PATH.is_file():
            import os
            os.startfile(RECORDS_PATH)
        else:
            messagebox.showinfo("License Records", "No codes have been created yet.", parent=root)

    buttons = ttk.Frame(body)
    buttons.grid(row=5, column=0, columnspan=2, sticky="w", pady=(12, 0))
    ttk.Button(buttons, text="Create Code", command=on_create).pack(side="left")
    ttk.Button(buttons, text="Copy Code", command=copy_code).pack(side="left", padx=(8, 0))
    ttk.Button(buttons, text="View Records", command=open_records).pack(side="left", padx=(8, 0))

    def back_up_now() -> None:
        folder = filedialog.askdirectory(parent=root, title="Choose where to save the backup (for example your USB drive)")
        if not folder:
            return
        root.configure(cursor="watch")
        root.update()
        try:
            target = make_backup(Path(folder))
        except (ValueError, OSError) as error:
            messagebox.showerror("Back Up Now", str(error), parent=root)
            return
        finally:
            root.configure(cursor="")
        messagebox.showinfo("Back Up Now", f"Backup saved to:\n{target}\n\nTo restore on any PC, double-click 'Restore PewVista.bat' in that folder.", parent=root)

    ttk.Button(buttons, text="Back Up Now", command=back_up_now).pack(side="left", padx=(8, 0))

    ttk.Separator(body).grid(row=9, column=0, columnspan=2, sticky="ew", pady=(16, 10))
    ttk.Label(body, text="Subscription price shown to churches (app, Terms and Conditions, manual):").grid(row=10, column=0, columnspan=2, sticky="w")
    price_value = tk.StringVar(value=licensing.get_price_text())
    price_row = ttk.Frame(body)
    price_row.grid(row=11, column=0, columnspan=2, sticky="ew", pady=(4, 0))
    ttk.Entry(price_row, textvariable=price_value, width=40).pack(side="left")

    def save_price() -> None:
        try:
            licensing.save_price_text(price_value.get())
        except (ValueError, OSError) as error:
            messagebox.showerror("Subscription Price", str(error), parent=root)
            return
        price_value.set(licensing.get_price_text())
        messagebox.showinfo("Subscription Price",
            f"Price saved: {licensing.get_price_text()}\n\nRun update_windows_app.bat to build a new installer and update with this price. "
            "Your Terms promise existing churches 30 days' notice before a price change applies to them.", parent=root)

    ttk.Button(price_row, text="Save Price", command=save_price).pack(side="left", padx=(8, 0))
    ttk.Label(body, text="Also change the amount in your PayPal plan when PayPal is set up.", foreground="#666666").grid(row=12, column=0, columnspan=2, sticky="w", pady=(4, 0))
    pc_entry.focus_set()
    root.mainloop()


if __name__ == "__main__":
    main()

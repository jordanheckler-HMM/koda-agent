"""
Apple native tool integrations for Koda.
Uses osascript (AppleScript) via subprocess — no third-party dependencies.
All functions are designed to be registered as Antigravity agent tools.
"""
import subprocess
import logging
import sys as _sys
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("koda.apple_tools")

_IS_MACOS = _sys.platform == "darwin"


def _osascript(script: str, timeout: int = 12) -> str:
    """Run an AppleScript and return stdout, or an error string."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout
        )
        if result.returncode != 0:
            err = result.stderr.strip()
            logger.warning(f"osascript error: {err}")
            return f"AppleScript error: {err}"
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return "Error: AppleScript timed out after waiting for the app."
    except Exception as e:
        return f"Error running AppleScript: {e}"


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

def list_reminders(list_name: str = "Reminders") -> str:
    """List all incomplete reminders in a named Reminders list.

    Args:
        list_name: Name of the list to read. Common values: 'Reminders', 'Groceries', 'Orion'.
    """
    if not _IS_MACOS:
        return "This tool requires macOS."
    script = f'''
tell application "Reminders"
    set output to ""
    try
        set theList to list "{list_name}"
        set incomplete to (reminders of theList whose completed is false)
        if (count of incomplete) is 0 then
            return "No incomplete reminders."
        end if
        repeat with r in incomplete
            set dStr to ""
            try
                set dStr to " [due: " & ((due date of r) as string) & "]"
            end try
            set output to output & "• " & (name of r) & dStr & "\\n"
        end repeat
    on error errMsg
        set output to "Error: " & errMsg
    end try
    return output
end tell
'''
    raw = _osascript(script)
    if raw.startswith("Error:") or raw.startswith("AppleScript error:"):
        return raw
    return f"**{list_name}** reminders:\n{raw}" if raw else f"No incomplete reminders in '{list_name}'."


def add_reminder(title: str, list_name: str = "Reminders", notes: str = "", due_date: str = "") -> str:
    """Add a new reminder to a Reminders list.

    Args:
        title: The reminder text.
        list_name: Which list to add to. Defaults to 'Reminders'.
        notes: Optional additional notes for the reminder.
        due_date: Optional due date, e.g. 'tomorrow', '2026-05-28 10:00', 'Friday at 9am'.
    """
    if not _IS_MACOS:
        return "This tool requires macOS."
    notes_line = f'set body of newReminder to "{_esc(notes)}"' if notes else ""
    due_line = ""
    if due_date:
        due_line = f'set due date of newReminder to date "{_esc(due_date)}"'

    script = f'''
tell application "Reminders"
    try
        set theList to list "{_esc(list_name)}"
        set newReminder to make new reminder at end of reminders of theList
        set name of newReminder to "{_esc(title)}"
        {notes_line}
        {due_line}
        return "Added: " & name of newReminder
    on error errMsg
        return "Error: " & errMsg
    end try
end tell
'''
    return _osascript(script)


def complete_reminder(title: str, list_name: str = "Reminders") -> str:
    """Mark a reminder as complete by its exact title.

    Args:
        title: Exact title of the reminder to complete.
        list_name: Which list to look in. Defaults to 'Reminders'.
    """
    if not _IS_MACOS:
        return "This tool requires macOS."
    script = f'''
tell application "Reminders"
    try
        set theList to list "{_esc(list_name)}"
        set target to first reminder of theList whose name is "{_esc(title)}" and completed is false
        set completed of target to true
        return "Completed: " & name of target
    on error errMsg
        return "Error: " & errMsg
    end try
end tell
'''
    return _osascript(script)


def list_reminder_lists() -> str:
    """List all available Reminders lists."""
    if not _IS_MACOS:
        return "This tool requires macOS."
    script = '''
tell application "Reminders"
    set names to {}
    repeat with l in lists
        set end of names to name of l
    end repeat
    return names as string
end tell
'''
    raw = _osascript(script)
    return f"Reminder lists: {raw}"


# ---------------------------------------------------------------------------
# Apple Notes
# ---------------------------------------------------------------------------

def get_notes(search_term: str = "", max_results: int = 10) -> str:
    """Search Apple Notes by title or body content.

    Args:
        search_term: Text to search for. Leave empty to list recent notes.
        max_results: Maximum number of notes to return (default 10).
    """
    if not _IS_MACOS:
        return "This tool requires macOS."
    if search_term:
        filter_clause = f'whose name contains "{_esc(search_term)}" or body contains "{_esc(search_term)}"'
    else:
        filter_clause = ""

    script = f'''
tell application "Notes"
    set output to ""
    try
        set allNotes to notes {filter_clause}
        set cnt to count of allNotes
        if cnt is 0 then return "No notes found."
        set limit to {max_results}
        if cnt < limit then set limit to cnt
        repeat with i from 1 to limit
            set n to item i of allNotes
            set output to output & "• " & name of n & "\\n"
        end repeat
        if cnt > {max_results} then
            set output to output & "  (+" & (cnt - {max_results}) & " more)\\n"
        end if
    on error errMsg
        return "Error: " & errMsg
    end try
    return output
end tell
'''
    raw = _osascript(script)
    label = f"Notes matching '{search_term}'" if search_term else "Recent Notes"
    return f"**{label}**:\n{raw}" if raw and not raw.startswith("Error") else raw


def read_note(title: str) -> str:
    """Read the full body of an Apple Note by its title.

    Args:
        title: Exact or partial title of the note to read.
    """
    if not _IS_MACOS:
        return "This tool requires macOS."
    script = f'''
tell application "Notes"
    try
        set target to first note whose name contains "{_esc(title)}"
        return name of target & "\\n---\\n" & body of target
    on error errMsg
        return "Error: " & errMsg
    end try
end tell
'''
    return _osascript(script)


def create_note(title: str, body: str, folder: str = "") -> str:
    """Create a new Apple Note.

    Args:
        title: Title for the new note.
        body: Body text of the note (plain text or HTML).
        folder: Optional folder name to place the note in.
    """
    if not _IS_MACOS:
        return "This tool requires macOS."
    if folder:
        location = f'folder "{_esc(folder)}" of default account'
    else:
        location = "default account"

    script = f'''
tell application "Notes"
    try
        set newNote to make new note at {location} with properties {{name:"{_esc(title)}", body:"{_esc(body)}"}}
        return "Note created: " & name of newNote
    on error errMsg
        return "Error: " & errMsg
    end try
end tell
'''
    return _osascript(script)


def append_to_note(title: str, text: str) -> str:
    """Append text to an existing Apple Note by title.

    Args:
        title: Exact or partial title of the note to update.
        text: Text to append.
    """
    if not _IS_MACOS:
        return "This tool requires macOS."
    script = f'''
tell application "Notes"
    try
        set target to first note whose name contains "{_esc(title)}"
        set body of target to body of target & "\\n{_esc(text)}"
        return "Appended to: " & name of target
    on error errMsg
        return "Error: " & errMsg
    end try
end tell
'''
    return _osascript(script)


# ---------------------------------------------------------------------------
# iMessage
# ---------------------------------------------------------------------------

def send_imessage(recipient: str, message: str) -> str:
    """Send an iMessage to a contact name or phone number.

    Args:
        recipient: Contact display name (e.g. 'Mom') or phone number (e.g. '+16185551234').
        message: The text message to send.
    """
    if not _IS_MACOS:
        return "This tool requires macOS."
    script = f'''
tell application "Messages"
    try
        set targetService to 1st service whose service type = iMessage
        set targetBuddy to buddy "{_esc(recipient)}" of targetService
        send "{_esc(message)}" to targetBuddy
        return "Sent to {_esc(recipient)}: {_esc(message[:60])}"
    on error errMsg
        return "Error sending iMessage: " & errMsg
    end try
end tell
'''
    return _osascript(script)


def get_recent_messages(contact: str, count: int = 10) -> str:
    """Get recent iMessages from a contact.

    Args:
        contact: Contact display name or number.
        count: How many recent messages to retrieve (default 10).
    """
    if not _IS_MACOS:
        return "This tool requires macOS."
    script = f'''
tell application "Messages"
    try
        set output to ""
        set targetChat to first chat whose name contains "{_esc(contact)}"
        set msgs to messages of targetChat
        set total to count of msgs
        set startIdx to total - {count} + 1
        if startIdx < 1 then set startIdx to 1
        repeat with i from startIdx to total
            set m to item i of msgs
            set sender to "Me"
            try
                if direction of m is incoming then
                    set sender to handle of (sender of m)
                end if
            end try
            set output to output & sender & ": " & content of m & "\\n"
        end repeat
        return output
    on error errMsg
        return "Error: " & errMsg
    end try
end tell
'''
    raw = _osascript(script)
    return f"**Messages with {contact}**:\n{raw}" if raw and not raw.startswith("Error") else raw


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

def list_calendar_events(days_ahead: int = 7, calendar_name: str = "") -> str:
    """List upcoming calendar events.

    Args:
        days_ahead: How many days into the future to look (default 7).
        calendar_name: Optional calendar name to filter by. Leave empty for all calendars.
    """
    if not _IS_MACOS:
        return "This tool requires macOS."
    cal_filter = f'whose name is "{_esc(calendar_name)}"' if calendar_name else ""

    script = f'''
tell application "Calendar"
    set output to ""
    set startDate to current date
    set endDate to startDate + ({days_ahead} * days)
    try
        set allCals to every calendar {cal_filter}
        repeat with cal in allCals
            set calEvents to (every event of cal whose start date >= startDate and start date <= endDate)
            repeat with e in calEvents
                set output to output & (start date of e as string) & " — " & summary of e & " [" & name of cal & "]\\n"
            end repeat
        end repeat
    on error errMsg
        set output to "Error: " & errMsg
    end try
    if output is "" then return "No events in the next {days_ahead} days."
    return output
end tell
'''
    raw = _osascript(script)
    return f"**Upcoming {days_ahead}-day calendar**:\n{raw}" if raw and not raw.startswith("Error") else raw


def create_calendar_event(
    title: str,
    start_datetime: str,
    end_datetime: str,
    calendar_name: str = "",
    notes: str = "",
    location: str = "",
) -> str:
    """Create a new calendar event.

    Args:
        title: Event title/summary.
        start_datetime: Start time, e.g. '5/28/2026 10:00:00 AM' or 'tomorrow at 2pm'.
        end_datetime: End time in the same format.
        calendar_name: Calendar to add to. Leave empty for default.
        notes: Optional event notes/description.
        location: Optional location.
    """
    if not _IS_MACOS:
        return "This tool requires macOS."
    cal_clause = f'calendar "{_esc(calendar_name)}"' if calendar_name else "default calendar"
    notes_line = f'set description of newEvent to "{_esc(notes)}"' if notes else ""
    loc_line = f'set location of newEvent to "{_esc(location)}"' if location else ""

    script = f'''
tell application "Calendar"
    try
        set startDate to date "{_esc(start_datetime)}"
        set endDate to date "{_esc(end_datetime)}"
        set targetCal to {cal_clause}
        set newEvent to make new event at end of events of targetCal with properties {{summary:"{_esc(title)}", start date:startDate, end date:endDate}}
        {notes_line}
        {loc_line}
        return "Event created: " & summary of newEvent & " on " & (start date of newEvent as string)
    on error errMsg
        return "Error: " & errMsg
    end try
end tell
'''
    return _osascript(script)




# ---------------------------------------------------------------------------
# Apple Contacts
# ---------------------------------------------------------------------------

def search_contacts(query: str) -> str:
    """Search Apple Contacts by name, phone number, or email address.

    Args:
        query: Name, phone number, or email to search for.
    """
    if not _IS_MACOS:
        return "Apple Contacts is only available on macOS."
    script = f'''
tell application "Contacts"
    set output to ""
    try
        set matchedPeople to every person whose name contains "{_esc(query)}"
        if (count of matchedPeople) is 0 then
            set matchedPeople to {{}}
            repeat with p in every person
                try
                    set phones to value of phones of p
                    repeat with ph in phones
                        if ph contains "{_esc(query)}" then
                            set end of matchedPeople to p
                            exit repeat
                        end if
                    end repeat
                end try
                try
                    set emails to value of emails of p
                    repeat with em in emails
                        if em contains "{_esc(query)}" then
                            if matchedPeople does not contain p then
                                set end of matchedPeople to p
                            end if
                            exit repeat
                        end if
                    end repeat
                end try
            end repeat
        end if
        if (count of matchedPeople) is 0 then return "No contacts found matching: {_esc(query)}"
        repeat with p in matchedPeople
            set pName to name of p
            set phoneStr to ""
            try
                set phoneList to value of phones of p
                if (count of phoneList) > 0 then set phoneStr to " | " & item 1 of phoneList
            end try
            set emailStr to ""
            try
                set emailList to value of emails of p
                if (count of emailList) > 0 then set emailStr to " | " & item 1 of emailList
            end try
            set output to output & "• " & pName & phoneStr & emailStr & "\\n"
        end repeat
    on error errMsg
        return "Error: " & errMsg
    end try
    return output
end tell
'''
    raw = _osascript(script)
    if raw.startswith("Error") or raw.startswith("AppleScript error"):
        return raw
    return f"**Contacts matching '{query}'**:\n{raw}" if raw else f"No contacts found matching: {query}"


def get_contact(name: str) -> str:
    """Get full contact details for a person from Apple Contacts.

    Args:
        name: Full or partial name of the contact.
    """
    if not _IS_MACOS:
        return "Apple Contacts is only available on macOS."
    script = f'''
tell application "Contacts"
    try
        set p to first person whose name contains "{_esc(name)}"
        set output to "Name: " & name of p & "\\n"
        try
            set org to organization of p
            if org is not missing value and org is not "" then
                set output to output & "Organization: " & org & "\\n"
            end if
        end try
        try
            set phoneList to phones of p
            if (count of phoneList) > 0 then
                repeat with ph in phoneList
                    set output to output & "Phone (" & label of ph & "): " & value of ph & "\\n"
                end repeat
            end if
        end try
        try
            set emailList to emails of p
            if (count of emailList) > 0 then
                repeat with em in emailList
                    set output to output & "Email (" & label of em & "): " & value of em & "\\n"
                end repeat
            end if
        end try
        try
            set addrList to addresses of p
            if (count of addrList) > 0 then
                set addr to item 1 of addrList
                set addrStr to ""
                try
                    set addrStr to street of addr
                end try
                try
                    set addrStr to addrStr & ", " & city of addr
                end try
                try
                    set addrStr to addrStr & ", " & state of addr
                end try
                try
                    set addrStr to addrStr & " " & zip of addr
                end try
                set output to output & "Address: " & addrStr & "\\n"
            end if
        end try
        return output
    on error errMsg
        return "Error: " & errMsg
    end try
end tell
'''
    raw = _osascript(script)
    if raw.startswith("Error") or raw.startswith("AppleScript error"):
        return raw
    return f"**Contact: {name}**\n{raw}" if raw else f"No contact found for: {name}"


def add_contact(name: str, phone: str = "", email: str = "") -> str:
    """Add a new contact to Apple Contacts.

    Args:
        name: Full name for the new contact.
        phone: Phone number (optional).
        email: Email address (optional).
    """
    if not _IS_MACOS:
        return "Apple Contacts is only available on macOS."
    phone_block = ""
    if phone:
        phone_block = f'make new phone at end of phones of newPerson with properties {{label:"mobile", value:"{_esc(phone)}"}}'
    email_block = ""
    if email:
        email_block = f'make new email at end of emails of newPerson with properties {{label:"home", value:"{_esc(email)}"}}'

    script = f'''
tell application "Contacts"
    try
        set newPerson to make new person with properties {{first name:"{_esc(name)}"}}
        {phone_block}
        {email_block}
        save
        return "Contact added: " & name of newPerson
    on error errMsg
        return "Error: " & errMsg
    end try
end tell
'''
    return _osascript(script)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _esc(s: str) -> str:
    """Escape a string for safe embedding in AppleScript double-quoted strings."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "")

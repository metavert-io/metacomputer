"""Test D: LibreOffice Writer document, heading style, save, reopen from Thunar.

Writer is driven almost entirely through accessibility: AT-SPI text insertion
into the document paragraph (confirmed by read-back), the native menu path
Styles > Heading 1, File > Save As..., the GTK file chooser's Name field
(set_value), File > Exit LibreOffice. Thunar is switched to its List View
(accessible table cells) and the file is opened with a double-click on its
cell. The result is verified in the reopened document's accessibility tree
and, independently, in the saved file's content.xml.
"""
import os
import time

from common import (find_elements, wait_element, odt_paragraphs, prep_close, retry, state, token, wait_gone,
                    wait_title, wait_window)
from harness import Step, guest
from labconfig import GUEST_HOME

TITLE = "LibreOffice Writer"
TASK = ("Open Writer through the UI, enter the sentence, apply a heading style, save as cua-test.odt, "
        "close LibreOffice, reopen the document through the file manager, verify content and formatting")
TEXT = "This document was created autonomously by Claude Code operating an Ubuntu Linux desktop using Cua."
DOCS = os.path.join(GUEST_HOME, "Documents")
FILE = "cua-test.odt"


def open_writer(t):
    t.call("launch_app", {"name": "libreoffice", "additional_arguments": ["--writer", "--norestore"]})
    return wait_window(t, "libreoffice", title_part="LibreOffice Writer", timeout=60, min_width=600)


def save_as(t, pid, wid, path):
    """File > Save As..., set the name field to an absolute path, commit."""
    r = None
    for label in ("Save As...", "Save As…"):
        r = t.call("invoke_menu", {"pid": pid, "window_id": wid, "path": ["File", label]}, ok_required=False)
        if not r.is_error:
            break
    if r.is_error:
        raise Step(f"File > Save As failed: {r.text[:200]}")
    dlg = None
    for _ in range(20):
        ws = t.call("list_windows", {"pid": pid}).structured.get("windows", [])
        dlg = next((x for x in ws if x["title"] in ("Save", "Save As")), None)
        if dlg:
            break
        time.sleep(0.5)
    if not dlg:
        raise Step("the Save dialog did not open")
    ds = t.shot("save dialog", pid, dlg["window_id"])
    r = t.call("set_value", {"pid": pid, "window_id": dlg["window_id"], "element_token": token(ds, r'text "[^"]*"'), "value": path})
    t.check("file name field set (read-back)", "read back" in r.text and path in r.text, r.text[:160])
    time.sleep(1.5)
    # GTK commits the dialog when the name field is activated; press Save only if it is still open.
    ws = t.call("list_windows", {"pid": pid}).structured.get("windows", [])
    if any(x["window_id"] == dlg["window_id"] for x in ws):
        ds = state(t, pid, dlg["window_id"], query="Save")
        t.call("click", {"pid": pid, "window_id": dlg["window_id"], "element_token": token(ds, r'push button "_?Save"')})


def exit_libreoffice(t, pid, wid):
    t.call("invoke_menu", {"pid": pid, "window_id": wid, "path": ["File", "Exit LibreOffice"]})
    return wait_gone(t, "libreoffice", timeout=30)


def open_in_thunar(t, folder, name):
    t.call("launch_app", {"name": "thunar", "additional_arguments": [folder]})
    w = wait_window(t, "thunar", title_part=folder.rstrip("/").split("/")[-1])
    pid, wid = w["pid"], w["window_id"]
    st = state(t, pid, wid, query="List View")
    t.call("click", {"pid": pid, "window_id": wid, "element_token": token(st, r'push button "List View"')})
    time.sleep(1)

    def find_cell():
        s = state(t, pid, wid, query=name)
        return token(s, rf'table cell "{name}"')
    retry(t, f"{name} in Thunar", find_cell, attempts=3,
          recover=lambda: t.call("press_key", {"pid": pid, "window_id": wid, "key": "F5"}, ok_required=False))
    shot = t.shot("thunar list view", pid, wid)          # the last read: its tokens are the current ones
    t.call("double_click", {"pid": pid, "window_id": wid, "element_token": token(shot, rf'table cell "{name}"')})
    return pid, wid


def run(t):
    prep_close("soffice", "oosplash")
    guest(f"mkdir -p {DOCS}; rm -f {DOCS}/{FILE} {DOCS}/.~lock.{FILE}#")

    w = open_writer(t)
    pid, wid = w["pid"], w["window_id"]
    t.shot("writer opened", pid, wid)
    st = wait_element(t, pid, wid, r'paragraph ""')  # document published to AT-SPI

    # 1. the sentence, inserted into the document paragraph through AT-SPI
    r = t.call("type_text", {"pid": pid, "window_id": wid, "element_token": token(st, r'paragraph ""'), "text": TEXT})
    t.check("text inserted (AT-SPI read-back confirmed)", r.structured.get("effect") == "confirmed",
            r.text[:160])

    # 2. heading style through the Styles menu
    t.call("invoke_menu", {"pid": pid, "window_id": wid, "path": ["Styles", "Heading 1"]})
    time.sleep(1)
    hs = find_elements(state(t, pid, wid, query="created autonomously", fmt="elements"), role="heading")
    t.check("first line is now a heading (AT-SPI role)", hs and hs[0].get("label") == TEXT,
            hs and f"{hs[0].get('role')}: {hs[0].get('label')}")
    t.shot("heading applied", pid, wid)

    # 3. save as cua-test.odt
    save_as(t, pid, wid, f"{DOCS}/{FILE}")
    title = wait_title(t, pid, FILE, timeout=20)
    t.check("saved (window title names the file)", FILE in title, title)

    # 4. close LibreOffice
    t.check("LibreOffice closed (File > Exit)", exit_libreoffice(t, pid, wid), "no LibreOffice window remains")

    # 5. reopen through the file manager
    tpid, twid = open_in_thunar(t, DOCS, FILE)
    w = wait_window(t, "libreoffice", title_part=FILE, timeout=60, min_width=600)
    pid, wid = w["pid"], w["window_id"]
    time.sleep(1.5)
    st = state(t, pid, wid, query="created autonomously", fmt="elements", timeout_ms=5000)
    hs = find_elements(st, role="heading")
    t.check("reopened document: heading with the exact text (AT-SPI)", hs and hs[0].get("label") == TEXT,
            hs and hs[0].get("label"))
    t.shot("reopened from thunar", pid, wid)

    # 6. independent check of the file itself
    odt = odt_paragraphs(f"{DOCS}/{FILE}")
    t.evidence["odt"] = odt
    first = odt["paras"][0] if odt["paras"] else None
    t.check("file: ODF text, first paragraph is <text:h> Heading 1 with the sentence",
            odt["mimetype"] == "application/vnd.oasis.opendocument.text" and first
            and first[0] == "h" and first[1] in ("Heading_20_1", "Heading 1") and first[2] == TEXT, first)

    # 7. tidy up: close Writer and the Thunar window
    exit_libreoffice(t, pid, wid)
    t.call("hotkey", {"pid": tpid, "window_id": twid, "keys": ["ctrl", "w"], "delivery_mode": "foreground"}, ok_required=False)
    t.shot("final desktop")
    return True

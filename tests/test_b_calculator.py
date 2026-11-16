"""Test B: GNOME Calculator, 137 x 29 = 3973, entered through the GUI.

Buttons are pressed through their accessibility actions (element tokens from
get_window_state). The result is read back from the display's accessible
value, checked again by the driver's verify_state predicate, and captured in
a window screenshot. The calculator is closed with its own Close button.
"""
from common import find_elements, prep_close, state, token, wait_element, wait_gone, wait_window

TITLE = "Calculator"
TASK = "Launch the calculator through GUI control, enter 137 × 29, read the result and verify it is 3973, close it"


def run(t):
    prep_close("gnome-calculator")
    t.call("launch_app", {"name": "gnome-calculator"})
    w = wait_window(t, "calc", title_part="Calculator")
    pid, wid = w["pid"], w["window_id"]
    t.shot("calculator opened", pid, wid)
    st = wait_element(t, pid, wid, r'button "="')  # tree published (cold starts lag the window)

    for key in ["1", "3", "7", "×", "2", "9", "="]:
        t.call("click", {"pid": pid, "window_id": wid, "element_token": token(st, f'button "{key}"')})
    t.milestone("pressed 1 3 7 × 2 9 = through AT-SPI button actions")

    el = state(t, pid, wid, fmt="elements", query="text")
    boxes = find_elements(el, role="text box")
    values = [b.get("value") or b.get("label") for b in boxes]
    t.evidence["display_values"] = values
    t.check("display reads 3973 (accessible value)", "3973" in values, values)

    v = t.call("verify_state", {"pid": pid, "window_id": wid, "include_screenshot": False,
                                "expect": [{"element": {"selector": {"role": "text box", "label_contains": "3973"}, "exists": True}}]},
               ok_required=False)
    t.evidence["verify_state"] = v.structured or v.text[:300]
    verdict = (v.structured or {}).get("verdict") or (v.structured or {}).get("status") or v.text
    t.check("driver verify_state predicate satisfied", "satisfied" in str(verdict).lower() and "unsatisfied" not in str(verdict).lower(),
            str(verdict)[:200])
    fin = t.shot("result 3973", pid, wid)  # visual evidence: the window image shows "137×29 = 3973"

    t.call("click", {"pid": pid, "window_id": wid, "element_token": token(fin, 'button "Close"')})
    t.check("calculator closed via its Close button", wait_gone(t, "calc"), "no Calculator window remains")
    t.shot("final desktop")
    return True

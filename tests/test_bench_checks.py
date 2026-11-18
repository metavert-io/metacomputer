#!/usr/bin/env python3
"""The benchmark's document checks (bench/guest/checklib.py), on made-up ODF files. No VM.

    python3 tests/test_bench_checks.py

bold_runs must report what shows in bold, the way Writer displays it: LibreOffice sometimes stores
"only one word bold" as a bold paragraph style with the rest of the text in a not-bold span, which
an earlier version counted as the whole paragraph bold.
"""
import os
import sys
import tempfile
import zipfile

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(LAB, "bench", "guest"))
import checklib  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


HEAD = ('<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
        'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0">')


def style(name, family, weight=None):
    props = f'<style:text-properties fo:font-weight="{weight}"/>' if weight else ""
    return f'<style:style style:name="{name}" style:family="{family}">{props}</style:style>'


def odt(td, name, styles, body):
    path = os.path.join(td, name)
    xml = (HEAD + "<office:automatic-styles>" + "".join(styles) + "</office:automatic-styles>"
           + "<office:body><office:text>"
           + '<text:h text:style-name="H1" text:outline-level="1">Quarterly Garden Report</text:h>'
           + body + "</office:text></office:body></office:document-content>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        z.writestr("content.xml", xml)
    return path


def main():
    td = tempfile.mkdtemp(prefix="mcomp-bench-checks-")
    runs = lambda p: [t.strip() for t in checklib.bold_runs(p)]  # noqa: E731 (as the task's check does)

    p = odt(td, "para-bold-rest-normal.odt", [style("P1", "paragraph", "bold"), style("T1", "text", "normal")],
            '<text:p text:style-name="P1">Tomatoes<text:span text:style-name="T1"> did well this season.</text:span></text:p>')
    check("a bold paragraph with the rest in a not-bold span: only the bold part", runs(p) == ["Tomatoes"], runs(p))

    p = odt(td, "span-bold.odt", [style("P2", "paragraph"), style("T2", "text", "bold")],
            '<text:p text:style-name="P2"><text:span text:style-name="T2">Tomatoes</text:span> did well this season.</text:p>')
    check("a bold span in a plain paragraph", runs(p) == ["Tomatoes"], runs(p))

    p = odt(td, "para-bold.odt", [style("P1", "paragraph", "bold")],
            '<text:p text:style-name="P1">Tomatoes did well this season.</text:p>')
    check("a whole bold paragraph is still reported whole", runs(p) == ["Tomatoes did well this season."], runs(p))

    p = odt(td, "plain.odt", [style("P2", "paragraph")], '<text:p text:style-name="P2">Tomatoes did well this season.</text:p>')
    check("nothing bold: no runs", runs(p) == [], runs(p))

    p = odt(td, "nested.odt", [style("P2", "paragraph"), style("T2", "text", "bold"), style("T3", "text", "normal")],
            '<text:p text:style-name="P2"><text:span text:style-name="T2">Tomatoes<text:span text:style-name="T3"> did</text:span></text:span> well.</text:p>')
    check("a not-bold span inside a bold span", runs(p) == ["Tomatoes"], runs(p))

    p = odt(td, "numeric.odt", [style("P2", "paragraph"), style("T2", "text", "700"), style("T4", "text", "400")],
            '<text:p text:style-name="P2"><text:span text:style-name="T2">Tomatoes</text:span>'
            '<text:span text:style-name="T4"> did well</text:span> this season.</text:p>')
    check("numeric weights: 700 is bold, 400 isn't", runs(p) == ["Tomatoes"], runs(p))

    p = odt(td, "two-runs.odt", [style("P2", "paragraph"), style("T2", "text", "bold")],
            '<text:p text:style-name="P2"><text:span text:style-name="T2">Tomatoes</text:span> and '
            '<text:span text:style-name="T2">peppers</text:span><text:span text:style-name="T2"> too</text:span>.</text:p>')
    check("separate bold runs stay separate; touching bold spans join", runs(p) == ["Tomatoes", "peppers too"], runs(p))

    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())

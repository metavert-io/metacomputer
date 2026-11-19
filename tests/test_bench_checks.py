#!/usr/bin/env python3
"""The benchmark's document checks (bench/guest/checklib.py), on made-up ODF and OOXML files, and the
lessons task set's definitions and checks (bench/tasks-lessons/). No VM.

    python3 tests/test_bench_checks.py

bold_runs must report what shows in bold, the way Writer displays it: LibreOffice sometimes stores
"only one word bold" as a bold paragraph style with the rest of the text in a not-bold span, which
an earlier version counted as the whole paragraph bold.

Each lessons-set task's own check runs here too, in a temporary home folder: once on the state the task
starts from (it must fail) and once on a made-up correct outcome (it must pass).
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUEST = os.path.join(LAB, "bench", "guest")
sys.path.insert(0, GUEST)
sys.path.insert(0, os.path.join(LAB, "bench"))
sys.path.insert(0, os.path.join(LAB, "lib"))
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

    readers(td)
    lessons_set()
    lessons_checks()
    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed'}")
    return 1 if FAILS else 0


# --- made-up spreadsheets ---------------------------------------------------------------------------
ODS_HEAD = ('<?xml version="1.0" encoding="UTF-8"?><office:document-content '
            'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
            'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
            'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
            'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
            'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
            'xmlns:xlink="http://www.w3.org/1999/xlink"><office:body><office:spreadsheet>')


def ods_cell(v):
    if isinstance(v, tuple):              # (formula, value)
        return (f'<table:table-cell table:formula="{v[0]}" office:value-type="float" office:value="{v[1]}">'
                f'<text:p>{v[1]}</text:p></table:table-cell>')
    if isinstance(v, (int, float)):
        return f'<table:table-cell office:value-type="float" office:value="{v}"><text:p>{v}</text:p></table:table-cell>'
    if v == "":
        return "<table:table-cell/>"
    return f'<table:table-cell office:value-type="string"><text:p>{v}</text:p></table:table-cell>'


def ods(path, sheets, shapes="", members=None):
    """sheets: [(name, rows)]; shapes: XML put at the start of the first sheet; members: other zip members."""
    xml = ODS_HEAD
    for i, (name, rows) in enumerate(sheets):
        xml += f'<table:table table:name="{name}">' + (shapes if i == 0 else "")
        xml += "".join("<table:table-row>" + "".join(ods_cell(c) for c in r) + "</table:table-row>" for r in rows)
        xml += "</table:table>"
    xml += "</office:spreadsheet></office:body></office:document-content>"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
        z.writestr("content.xml", xml)
        for k, v in (members or {}).items():
            z.writestr(k, v)
    return path


CHART = ('<?xml version="1.0" encoding="UTF-8"?><office:document-content '
         'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
         'xmlns:chart="urn:oasis:names:tc:opendocument:xmlns:chart:1.0" '
         'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"><office:body><office:chart>'
         '<chart:chart chart:class="chart:bar"><chart:plot-area table:cell-range-address="Sheet1.$A$1:.$B$13">'
         '<chart:series chart:values-cell-range-address="Sheet1.B2:Sheet1.B13"/></chart:plot-area>'
         '</chart:chart></office:chart></office:body></office:document-content>')


def chart_frame(w, h):
    # As LibreOffice writes it in an en-US setup: the frame's size in inches
    return (f'<table:shapes><draw:frame draw:name="Object 1" svg:width="{w}" svg:height="{h}">'
            '<draw:object draw:notify-on-update-of-ranges="Sheet1.A1:Sheet1.B13" xlink:href="./Object 1"/>'
            '</draw:frame></table:shapes>')


def xlsx(path, rows):
    """A minimal Office Open XML workbook: strings shared, numbers as values, one inline string."""
    shared, sx = [], []
    for r_i, r in enumerate(rows, 1):
        cells = []
        for c_i, v in enumerate(r):
            ref = "ABCDEFGH"[c_i] + str(r_i)
            if isinstance(v, (int, float)):
                cells.append(f'<c r="{ref}"><v>{v}</v></c>')
            elif v.startswith("inline:"):
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{v[7:]}</t></is></c>')
            else:
                shared.append(v)
                cells.append(f'<c r="{ref}" t="s"><v>{len(shared) - 1}</v></c>')
        sx.append(f'<row r="{r_i}">' + "".join(cells) + "</row>")
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/workbook.xml", f"<workbook {ns}/>")
        z.writestr("xl/sharedStrings.xml", f"<sst {ns}>" + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml", f"<worksheet {ns}><sheetData>" + "".join(sx) + "</sheetData></worksheet>")
    return path


def readers(td):
    print("readers for the lessons set: sheets by name, chart frame sizes, lengths, .xlsx")
    p = ods(os.path.join(td, "two.ods"), [("Data", [["a", 1]]), ("Summary", [["Total", ("of:=SUM([$Data.B1])", 1)]])])
    rows, name = checklib.ods_rows(p, "Summary")
    check("ods_rows by sheet name", name == "Summary" and checklib.cell(rows, "B1")["formula"] == "of:=SUM([$Data.B1])",
          (name, rows))
    check("ods_sheet_names in order", checklib.ods_sheet_names(p) == ["Data", "Summary"])
    try:
        checklib.ods_rows(p, "Nope")
        missing = False
    except KeyError:
        missing = True
    check("a sheet name that isn't there raises KeyError", missing)
    check("length_cm: in, cm, mm, pt; nonsense is None",
          checklib.length_cm("6.2992in") == 16.0 and checklib.length_cm("9cm") == 9.0
          and checklib.length_cm("160mm") == 16.0 and abs(checklib.length_cm("72pt") - 2.54) < 1e-6
          and checklib.length_cm("wide") is None and checklib.length_cm(None) is None)
    p = ods(os.path.join(td, "chart.ods"), [("Sheet1", [["Month", "Rainfall (mm)"]])],
            chart_frame("6.2992in", "3.5433in"), {"Object 1/content.xml": CHART})
    fr = checklib.ods_chart_frames(p)
    check("ods_chart_frames: class, ranges and the frame's size in cm", len(fr) == 1 and fr[0]["class"] == "chart:bar"
          and fr[0]["width_cm"] == 16.0 and fr[0]["height_cm"] == 9.0 and "B2:Sheet1.B13" in fr[0]["ranges"], fr)
    p = ods(os.path.join(td, "broken.ods"), [("Sheet1", [["x"]])], chart_frame("16cm", "9cm"),
            {"Object 1/content.xml": CHART[:200]})
    fr = checklib.ods_chart_frames(p)
    check("a chart object that doesn't parse is listed as unreadable, not an exception",
          len(fr) == 1 and fr[0]["class"] == "unreadable" and fr[0]["width_cm"] == 16.0, fr)
    p = xlsx(os.path.join(td, "o.xlsx"), [["Order", "Total"], ["A-1", 186.4], ["inline:B-2", 3]])
    check("xlsx_rows: shared strings, numbers, inline strings", checklib.xlsx_rows(p)
          == [["Order", "Total"], ["A-1", "186.4"], ["B-2", "3"]], checklib.xlsx_rows(p))
    check("zip_names: members of a zip, [] for anything else", "xl/workbook.xml" in checklib.zip_names(p)
          and checklib.zip_names(os.path.join(td, "missing.xlsx")) == [] and checklib.zip_names(__file__) == [])


# --- the lessons set ----------------------------------------------------------------------------------
def lessons_set():
    print("the lessons task set: definitions")
    import bench
    import learn
    import learn_mine
    import report
    std, les = bench.load_set("standard"), bench.load_set("lessons")
    ids = [t["id"] for t in std + les]
    check("6-10 tasks; task ids unique across the sets", 6 <= len(les) <= 10 and len(ids) == len(set(ids)), ids)
    patterns = set(learn_mine.LABELS) - {"other"} | set(report.BEHAVIOUR)
    for t in les:
        tid = t["id"]
        need = {"id", "title", "apps", "route", "prompt", "check", "targets", "lessons"}
        check(f"{tid}: has {', '.join(sorted(need))}", need <= set(t), sorted(need - set(t)))
        check(f"{tid}: targets are patterns learn's classifier counts", set(t["targets"]) <= patterns,
              set(t["targets"]) - patterns)
        check(f"{tid}: lessons are built-in lesson ids", all(x in learn.TEMPLATES for x in t["lessons"]), t["lessons"])
        check(f"{tid}: says where its pattern was seen (evidence), or what quirk it controls for",
              bool(t.get("evidence") if t["targets"] else t.get("quirk")))
        check(f"{tid}: route and timeout", t["route"] in ("ax", "shell", "visual", "mixed")
              and 300 <= int(t.get("timeout_s", 600)) <= 900)
        for f in t.get("files", []):
            check(f"{tid}: fixture {f} exists", os.path.exists(os.path.join(bench.FIX, "files", f)))
        for page in re.findall(r"http://localhost:8000/(\S+?)[\s.,]*(?:\s|$)", t["prompt"]):
            check(f"{tid}: fixture page {page} exists", os.path.isfile(os.path.join(bench.FIX, "site", page)), page)
        try:
            compile(bench.CHECK_HEAD + t["check"] + "\nC.report()\n", tid, "exec")
            ok = True
        except SyntaxError as e:
            ok = e
        check(f"{tid}: its check compiles", ok is True, ok)
        check(f"{tid}: the prompt names no lesson, quirk or failure class (the tasks are ordinary work)",
              not re.search(r"autocorrect|screenshot|lesson|no_frame|edit mode|accessib", t["prompt"], re.I), t["prompt"])
    controls = [t["id"] for t in les if not t["targets"]]
    check("one control task: a pattern no built-in lesson answers", controls == ["calc-arrows"], controls)
    srv = open(os.path.join(GUEST, "server.py")).read()
    check("the fixture server takes the new endpoints", '"/book"' in srv and '"/extend"' in srv)


def run_check(task, home):
    script = f"import sys\nsys.path.insert(0, {GUEST!r})\nfrom checklib import *\nC = Checks()\n" + task["check"] + "\nC.report()\n"
    r = subprocess.run([sys.executable, "-"], input=script, capture_output=True, text=True,
                       env=dict(os.environ, HOME=home), timeout=60)
    try:
        return json.loads(r.stdout.strip().split("\n")[-1])
    except (ValueError, IndexError):
        return {"pass": False, "score": 0, "error": r.stderr[-300:]}


def log_requests(home, recs):
    d = os.path.join(home, ".cache", "bench-srv")
    os.makedirs(d, exist_ok=True)
    ua = "Mozilla/5.0 (X11; Linux aarch64; rv:133.0) Gecko/20100101 Firefox/133.0"
    with open(os.path.join(d, "requests.jsonl"), "a") as f:
        for path, form, query in recs:
            f.write(json.dumps({"t": 1, "method": "POST" if form else "GET", "path": path, "query": query,
                                "form": form, "referer": "", "agent": ua}) + "\n")


def lessons_checks():
    print("the lessons task set: each check fails on the starting state and passes on a correct outcome")
    import bench
    tasks = {t["id"]: t for t in bench.load_set("lessons")}
    soup = open(os.path.join(bench.FIX, "files", "recipes", "soup.txt")).read()
    regions = [("North", 8402), ("South", 6842), ("East", 9828)]
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun"]
    rain = [78, 61, 55, 47, 52, 40, 38, 44, 59, 83, 91, 86]
    orders = [["A-3101", "Brightwater Cafe", 12, 186.4], ["A-3102", "Hollis and Gray", 3, 54.75],
              ["A-3103", "Juniper Studio", 7, 129.1], ["A-3104", "Northfield Bakery", 20, 342],
              ["A-3105", "Quayside Books", 5, 88.25]]
    pairs = [("Lisbon", "Porto"), ("Lyon", "Geneva"), ("Leeds", "York"), ("Turin", "Milan"), ("Ghent", "Bruges")]

    def start(tid, home):
        b = os.path.join(home, "bench")
        os.makedirs(os.path.join(b, "out"), exist_ok=True)
        os.makedirs(os.path.join(home, ".cache", "bench-srv"), exist_ok=True)
        open(os.path.join(home, ".cache", "bench-srv", "requests.jsonl"), "w").close()
        if tid == "mousepad-replace":
            os.makedirs(os.path.join(b, "recipes"), exist_ok=True)
            open(os.path.join(b, "recipes", "soup.txt"), "w").write(soup)
        elif tid == "calc-arrows":
            ods(os.path.join(b, "sheets", "routes.ods"), [("routes", [["From", "To", "Route"]] + [[x, y] for x, y in pairs])])
        elif tid == "calc-cross-sheet":
            ods(os.path.join(b, "sheets", "regions.ods"),
                [(n, [["Month", "Sales"]] + [[m, 1] for m in months]) for n, _ in regions]
                + [("Summary", [["Region", "Total"]] + [[n] for n, _ in regions])])
        elif tid == "calc-chart-size":
            ods(os.path.join(b, "sheets", "rainfall.ods"),
                [("Sheet1", [["Month", "Rainfall (mm)"]] + [[m, v] for m, v in zip(months * 2, rain)])])
        elif tid == "calc-save-xlsx":
            ods(os.path.join(b, "sheets", "orders.ods"), [("orders", [["Order", "Customer", "Items", "Total"]] + orders)])

    def finish(tid, home):
        b = os.path.join(home, "bench")
        if tid == "ff-seat-map":
            log_requests(home, [("/book", {"seats": ["3B,5D"], "passenger": ["Rosa Lind"]}, {})])
        elif tid == "ff-renew-loans":
            log_requests(home, [("/extend", {}, {"loan": ["L-2207"]}), ("/extend", {}, {"loan": ["L-2231"]})])
        elif tid == "ff-article-links":
            log_requests(home, [("/answer", {}, {"year": ["1874"], "keeper": ["Edda Marsh"]})])
        elif tid == "mousepad-replace":
            open(os.path.join(b, "recipes", "soup.txt"), "w").write(
                soup.replace("tablespoon", "tbsp").replace("teaspoon", "tsp"))
        elif tid == "calc-arrows":
            ods(os.path.join(b, "sheets", "routes.ods"),
                [("routes", [["From", "To", "Route"]] + [[x, y, f"{x} -> {y}"] for x, y in pairs])])
        elif tid == "calc-cross-sheet":
            ods(os.path.join(b, "sheets", "regions.ods"),
                [(n, [["Month", "Sales"]] + [[m, 1] for m in months]) for n, _ in regions]
                + [("Summary", [["Region", "Total"]] + [[n, (f"of:=SUM([${n}.B2:.B7])", v)] for n, v in regions])])
        elif tid == "calc-chart-size":
            ods(os.path.join(b, "sheets", "rainfall.ods"),
                [("Sheet1", [["Month", "Rainfall (mm)"]] + [[m, v] for m, v in zip(months * 2, rain)])],
                chart_frame("6.2992in", "3.5433in"), {"Object 1/content.xml": CHART})
        elif tid == "calc-save-xlsx":
            xlsx(os.path.join(b, "out", "orders.xlsx"), [["Order", "Customer", "Items", "Total"]] + orders)

    for tid, t in sorted(tasks.items()):
        home = tempfile.mkdtemp(prefix=f"mcomp-lx-{tid}-")
        start(tid, home)
        before = run_check(t, home)
        finish(tid, home)
        after = run_check(t, home)
        check(f"{tid}: fails on the starting state, passes on a correct outcome",
              not before["pass"] and after["pass"] and after["score"] == 1.0,
              (before.get("score"), after.get("score"), [c["name"] for c in after.get("checks", []) if not c["ok"]],
               after.get("error")))
    # the near misses each task is about
    home = tempfile.mkdtemp(prefix="mcomp-lx-near-")
    start("calc-arrows", home)
    ods(os.path.join(home, "bench", "sheets", "routes.ods"),
        [("routes", [["From", "To", "Route"]] + [[x, y, f"{x} → {y}"] for x, y in pairs])])
    r = run_check(tasks["calc-arrows"], home)
    check("calc-arrows: an AutoCorrected arrow fails", not r["pass"], r)
    start("calc-save-xlsx", home)
    import shutil
    shutil.copy(os.path.join(home, "bench", "sheets", "orders.ods"), os.path.join(home, "bench", "out", "orders.xlsx"))
    r = run_check(tasks["calc-save-xlsx"], home)
    check("calc-save-xlsx: an .ods saved under an .xlsx name fails", not r["pass"]
          and not [c for c in r["checks"] if c["name"].startswith("in Excel format")][0]["ok"], r)
    start("calc-chart-size", home)
    ods(os.path.join(home, "bench", "sheets", "rainfall.ods"),
        [("Sheet1", [["Month", "Rainfall (mm)"]] + [[m, v] for m, v in zip(months * 2, rain)])],
        chart_frame("6.2992in", "4in"), {"Object 1/content.xml": CHART})
    r = run_check(tasks["calc-chart-size"], home)
    check("calc-chart-size: 16 cm wide but the wrong height fails", not r["pass"], r)
    start("ff-seat-map", home)
    log_requests(home, [("/book", {"seats": ["3B,5D,4C"], "passenger": ["Rosa Lind"]}, {})])
    r = run_check(tasks["ff-seat-map"], home)
    check("ff-seat-map: an extra seat fails", not r["pass"], r)
    start("ff-renew-loans", home)
    log_requests(home, [("/extend", {}, {"loan": ["L-2207"]}), ("/extend", {}, {"loan": ["L-2231"]}),
                        ("/extend", {}, {"loan": ["L-2194"]})])
    r = run_check(tasks["ff-renew-loans"], home)
    check("ff-renew-loans: also renewing Kwame Osei's book fails", not r["pass"], r)


if __name__ == "__main__":
    sys.exit(main())

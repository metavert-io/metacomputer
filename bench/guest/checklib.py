"""Helpers for the benchmark's checks. Copied into the VM (/tmp/bench-check/)
only AFTER the agent's session has ended, and run there with the VM's own
python3 (3.12, standard library only). Checks read real state: files on disk,
the fixture server's request log, app settings, the live display.

A check script ends with `report([...])`, which prints one JSON line:
  {"pass": bool, "score": 0..1, "checks": [{"name", "weight", "ok", "detail"}]}
"""
import csv
import io
import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET
import zipfile

HOME = os.path.expanduser("~")
BENCH = os.path.join(HOME, "bench")
SERVER_LOG = os.path.join(HOME, ".cache", "bench-srv", "requests.jsonl")

NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "fo": "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "xlink": "http://www.w3.org/1999/xlink",
    "chart": "urn:oasis:names:tc:opendocument:xmlns:chart:1.0",
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "svg": "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0",
}


def q(tag):
    p, t = tag.split(":")
    return "{%s}%s" % (NS[p], t)


class Checks:
    def __init__(self):
        self.items = []

    def add(self, name, weight, ok, detail=""):
        self.items.append({"name": name, "weight": weight, "ok": bool(ok), "detail": str(detail)[:200]})
        return bool(ok)

    def report(self, require_all=True):
        total = sum(i["weight"] for i in self.items) or 1
        score = sum(i["weight"] for i in self.items if i["ok"]) / total
        passed = all(i["ok"] for i in self.items) if require_all else score >= 0.999
        print(json.dumps({"pass": passed, "score": round(score, 3), "checks": self.items}))


def path(p):
    return os.path.expanduser(p)


def read(p, default=None):
    try:
        with open(path(p), encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def num(s):
    """'1,234.50' / '$1234.5' / ' 1 234,5 ' -> float (None if not a number)."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = re.sub(r"[^\d.,\-−]", "", str(s)).replace("−", "-")
    if t.count(",") and t.count("."):
        t = t.replace(",", "")
    elif t.count(",") == 1 and len(t.split(",")[1]) != 3:
        t = t.replace(",", ".")
    else:
        t = t.replace(",", "")
    try:
        return float(t)
    except ValueError:
        return None


# --- the fixture server's log ---------------------------------------------------------
def requests_log():
    out = []
    for line in (read(SERVER_LOG, "") or "").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def field(rec, name):
    v = (rec.get("form") or {}).get(name) or (rec.get("query") or {}).get(name)
    return v[-1] if v else None


# --- ODF ------------------------------------------------------------------------------
def odf_xml(p, member="content.xml"):
    with zipfile.ZipFile(path(p)) as z:
        return ET.fromstring(z.read(member))


def text_of(el):
    """Text of an ODF element, with spaces/tabs/line breaks expanded."""
    parts = [el.text or ""]
    for c in el:
        if c.tag == q("text:s"):
            parts.append(" " * int(c.get(q("text:c"), "1")))
        elif c.tag in (q("text:tab"),):
            parts.append("\t")
        elif c.tag == q("text:line-break"):
            parts.append("\n")
        else:
            parts.append(text_of(c))
        parts.append(c.tail or "")
    return "".join(parts)


def odt_blocks(p):
    """[(kind 'h'|'p', outline_level or None, style_name, text, element)] in document order."""
    root = odf_xml(p)
    body = root.find(q("office:body")).find(q("office:text"))
    out = []
    for el in body.iter():
        if el.tag == q("text:h"):
            out.append(("h", el.get(q("text:outline-level")), el.get(q("text:style-name")), text_of(el), el))
        elif el.tag == q("text:p"):
            out.append(("p", None, el.get(q("text:style-name")), text_of(el), el))
    return out, root


def auto_styles(root):
    """automatic style name -> (parent style, text properties dict)."""
    out = {}
    auto = root.find(q("office:automatic-styles"))
    for st in (auto if auto is not None else []):
        props = {}
        for tp in st.findall(q("style:text-properties")):
            props.update({k.split("}")[1]: v for k, v in tp.attrib.items()})
        out[st.get(q("style:name"))] = (st.get(q("style:parent-style-name")), props)
    return out


def bold_runs(p):
    """Texts that show in bold in an .odt, as runs of consecutive bold text (whitespace-only runs left out).

    Bold is worked out the way Writer displays it: a paragraph's automatic style sets the starting weight,
    and each span's style can turn bold on or off for its text ("normal" turns it off). So a paragraph
    styled bold with the rest of its text in a not-bold span counts only the part left bold."""
    blocks, root = odt_blocks(p)
    styles = auto_styles(root)

    def weight(name):
        w = styles.get(name, (None, {}))[1].get("font-weight")
        if w is None:
            return None
        return w == "bold" or (w.isdigit() and int(w) >= 600)

    out = []
    for kind, _, sname, text, el in blocks:
        segs = []

        def walk(e, bold):
            w = weight(e.get(q("text:style-name")))
            b = bold if w is None else w
            segs.append((e.text or "", b))
            for c in e:
                if c.tag == q("text:s"):
                    segs.append((" " * int(c.get(q("text:c"), "1")), b))
                elif c.tag == q("text:tab"):
                    segs.append(("\t", b))
                elif c.tag == q("text:line-break"):
                    segs.append(("\n", b))
                elif c.tag in (q("text:span"), q("text:a")):
                    walk(c, b)
                else:
                    segs.append((text_of(c), b))
                segs.append((c.tail or "", b))

        walk(el, False)
        run = []
        for t, b in [(t, b) for t, b in segs if t] + [(" ", False)]:
            if b:
                run.append(t)
            elif run:
                if "".join(run).strip():
                    out.append("".join(run))
                run = []
    return out


def ods_sheet_names(p):
    root = odf_xml(p)
    return [t.get(q("table:name")) for t in
            root.find(q("office:body")).find(q("office:spreadsheet")).findall(q("table:table"))]


def ods_rows(p, sheet=0, max_rows=500, max_cols=40):
    """A sheet of an .ods (by index, or by name) as rows of dicts {text, value, formula, type}, and its name.
    A sheet name that isn't there raises KeyError."""
    root = odf_xml(p)
    tables = root.find(q("office:body")).find(q("office:spreadsheet")).findall(q("table:table"))
    if isinstance(sheet, str):
        named = [x for x in tables if x.get(q("table:name")) == sheet]
        if not named:
            raise KeyError(f"no sheet {sheet!r}")
        t = named[0]
    else:
        t = tables[sheet]
    rows = []
    for row in t.iter(q("table:table-row")):
        rrep = min(int(row.get(q("table:number-rows-repeated"), "1")), max_rows)
        cells = []
        for c in row:
            if c.tag not in (q("table:table-cell"), q("table:covered-table-cell")):
                continue
            crep = min(int(c.get(q("table:number-columns-repeated"), "1")), max_cols)
            vt = c.get(q("office:value-type"))
            val = c.get(q("office:value")) or c.get(q("office:date-value")) or c.get(q("office:boolean-value"))
            txt = "\n".join(text_of(x) for x in c.findall(q("text:p")))
            cell = {"text": txt, "value": val, "formula": c.get(q("table:formula")), "type": vt}
            cells.extend([cell] * crep)
            if len(cells) >= max_cols:
                break
        while cells and not (cells[-1]["text"] or cells[-1]["value"] or cells[-1]["formula"]):
            cells.pop()
        rows.extend([cells] * rrep)
        if len(rows) >= max_rows:
            break
    while rows and not rows[-1]:
        rows.pop()
    return rows, t.get(q("table:name"))


def cell(rows, ref):
    """'B3' -> cell dict (or an empty one)."""
    m = re.match(r"([A-Z]+)(\d+)$", ref)
    col = 0
    for ch in m.group(1):
        col = col * 26 + ord(ch) - 64
    r, c = int(m.group(2)) - 1, col - 1
    if r < len(rows) and c < len(rows[r]):
        return rows[r][c]
    return {"text": "", "value": None, "formula": None, "type": None}


def cval(c):
    v = c.get("value")
    if v is not None:
        try:
            return float(v)
        except ValueError:
            return v
    return num(c.get("text")) if num(c.get("text")) is not None else c.get("text")


def ods_charts(p):
    """[(object folder, chart class, ranges)] for the charts embedded in an .ods."""
    out = []
    with zipfile.ZipFile(path(p)) as z:
        names = set(z.namelist())
        root = ET.fromstring(z.read("content.xml"))
        for obj in root.iter(q("draw:object")):
            href = (obj.get(q("xlink:href")) or "").lstrip("./")
            member = href + "/content.xml"
            if member not in names:
                continue
            cr = ET.fromstring(z.read(member))
            ch = cr.find(".//" + q("chart:chart"))
            if ch is None:
                continue
            pa = cr.find(".//" + q("chart:plot-area"))
            ranges = " ".join(filter(None, [obj.get(q("draw:notify-on-update-of-ranges")),
                                            pa.get(q("table:cell-range-address")) if pa is not None else None]))
            out.append((href, ch.get(q("chart:class")), ranges))
    return out


UNITS_CM = {"cm": 1.0, "mm": 0.1, "in": 2.54, "pt": 2.54 / 72, "pc": 2.54 / 6, "px": 2.54 / 96}


def length_cm(s):
    """An ODF length ('16cm', '6.2992in', '160mm', '453.5pt') in centimetres, or None."""
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*(cm|mm|in|pt|pc|px)\s*$", str(s or ""))
    return round(float(m.group(1)) * UNITS_CM[m.group(2)], 4) if m else None


def ods_chart_frames(p):
    """The charts embedded in an .ods with the size of the frame that holds each, as dicts
    {object, class, ranges, width_cm, height_cm} (a size the file doesn't give is None). An embedded object
    whose XML can't be parsed is listed with class "unreadable" rather than failing the whole check."""
    out = []
    with zipfile.ZipFile(path(p)) as z:
        names = set(z.namelist())
        root = ET.fromstring(z.read("content.xml"))
        for frame in root.iter(q("draw:frame")):
            for obj in frame.findall(q("draw:object")):
                href = (obj.get(q("xlink:href")) or "").lstrip("./")
                member = href + "/content.xml"
                if member not in names:
                    continue
                try:
                    cr = ET.fromstring(z.read(member))
                except ET.ParseError:
                    cr = None
                ch = cr.find(".//" + q("chart:chart")) if cr is not None else None
                if cr is not None and ch is None:
                    continue
                pa = cr.find(".//" + q("chart:plot-area")) if cr is not None else None
                series = [s.get(q("chart:values-cell-range-address")) for s in cr.iter(q("chart:series"))] \
                    if cr is not None else []
                ranges = " ".join(filter(None, [obj.get(q("draw:notify-on-update-of-ranges")),
                                                pa.get(q("table:cell-range-address")) if pa is not None else None]
                                         + series))
                out.append({"object": href, "class": ch.get(q("chart:class")) if ch is not None else "unreadable",
                            "ranges": ranges,
                            "width_cm": length_cm(frame.get(q("svg:width"))),
                            "height_cm": length_cm(frame.get(q("svg:height")))})
    return out


def zip_names(p):
    """The members of a zip file (an ODF or OOXML document), or [] when it isn't one."""
    try:
        with zipfile.ZipFile(path(p)) as z:
            return z.namelist()
    except (OSError, zipfile.BadZipFile):
        return []


def _col_index(ref):
    col = 0
    for ch in re.match(r"([A-Z]+)", ref).group(1):
        col = col * 26 + ord(ch) - 64
    return col - 1


def xlsx_rows(p, sheet=1):
    """A sheet of an .xlsx (Office Open XML; sheet N in workbook order, from 1) as rows of cell texts: shared and
    inline strings as text, numbers as LibreOffice or Excel wrote them. No formulas, no formatting."""
    with zipfile.ZipFile(path(p)) as z:
        names = set(z.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(q("x:si")):
                shared.append("".join(t.text or "" for t in si.iter(q("x:t"))))
        member = f"xl/worksheets/sheet{sheet}.xml"
        root = ET.fromstring(z.read(member))
    rows = []
    for r in root.iter(q("x:row")):
        cells = []
        for c in r.findall(q("x:c")):
            i = _col_index(c.get("r") or "A")
            while len(cells) < i:
                cells.append("")
            t, v = c.get("t"), c.find(q("x:v"))
            if t == "s" and v is not None:
                cells.append(shared[int(v.text)])
            elif t == "inlineStr":
                cells.append("".join(x.text or "" for x in c.iter(q("x:t"))))
            else:
                cells.append(v.text if v is not None and v.text is not None else "")
        rows.append(cells)
    return rows


def docx_text(p):
    with zipfile.ZipFile(path(p)) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    paras = []
    for para in root.iter(q("w:p")):
        paras.append("".join(t.text or "" for t in para.iter(q("w:t"))))
    return "\n".join(paras)


def is_zip(p):
    try:
        return zipfile.is_zipfile(path(p))
    except OSError:
        return False


# --- the live desktop ---------------------------------------------------------------
def session_env():
    env = dict(os.environ)
    f = "/run/user/%d/metacomputer/session.env" % os.getuid()
    for line in (read(f, "") or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            env[k.replace("export ", "").strip()] = v.strip().strip("'\"")
    return env


def run(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=session_env())
        return r.returncode, r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def gsetting(schema, key):
    rc, out = run(["gsettings", "get", schema, key])
    return out if rc == 0 else None


def windows(cls):
    rc, out = run(["xdotool", "search", "--onlyvisible", "--class", cls])
    return out.split() if rc == 0 and out else []


def copy_from_window(wid, keys="ctrl+c"):
    """Focus a window, send the copy shortcut and return the clipboard text."""
    run(["xclip", "-selection", "clipboard", "-i", "/dev/null"])
    run(["xdotool", "windowactivate", "--sync", wid])
    run(["xdotool", "key", "--clearmodifiers", keys])
    rc, out = run(["xclip", "-o", "-selection", "clipboard"])
    return out if rc == 0 else ""


def csv_rows(p):
    t = read(p)
    if t is None:
        return None
    return list(csv.reader(io.StringIO(t.lstrip("﻿"))))

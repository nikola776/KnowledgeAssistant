"""
Stage 1d: work out where each screenshot sits in its article.

Knowing an image exists is not enough to surface it in an answer. A retrieval
system needs to know that this screenshot illustrates *this* step, so an answer
to "how do I calibrate a scanner" can show the 3D Calibrate button rather than a
photo of the calibration tip.

Two pieces:

1. Each image's position on the page, read from the PDF content stream. An image
   XObject is drawn into the unit square, so its page position comes from the
   current transformation matrix at the `Do`. Reading only the nearest `cm` is not
   enough: browser print drivers wrap the page in an outer scale (these files draw
   in CSS pixels at roughly 0.24 scale) and nest content inside Form XObjects, so
   the full q/Q matrix stack has to be tracked and forms recursed into.

2. The body line immediately above that edge. Xpdf's -marginb ignores text in the
   bottom margin of a page, so extracting with `-marginb <image top>` returns
   exactly the text above the image, and its last line is the anchor.

That second trick means no text-coordinate parsing is needed, which keeps this
dependency-free like the rest of the pipeline.

Run:  python pipeline/anchor_images.py   (after extract_images.py)
"""

import json
import os
import re
import subprocess
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INTAKE = os.path.join(ROOT, "00-intake")
OUT = os.path.join(ROOT, "01-ocr")

PDFTOTEXT = r"C:\Program Files\Git\mingw64\bin\pdftotext.exe"
# Must match BODY_X in normalise.py: anchor text is matched against body lines by
# exact string, so the two extracts have to be cropped identically.
BODY_MARGIN_R = 135  # 595 - 460

OBJ = re.compile(rb"(\d+)\s+0\s+obj\b(.*?)\bendobj", re.S)
CHROME = re.compile(
    r"^\s*(IT Helpdesk\s+Home Requests Solutions"
    r"|Feedback Keywords Associations"
    r"|Was this solution helpful\?.*"
    r"|Comments.*"
    r"|Keywords|Associations|Linked Solutions|Attachments?)\s*$",
    re.IGNORECASE,
)

# Content-stream tokens. Strings and hex strings are matched so that a "Q" or
# "cm" inside drawn text is never mistaken for an operator.
TOKEN = re.compile(rb"""
      (?P<num>[-+]?(?:\d+\.\d*|\.\d+|\d+))
    | /(?P<name>[^\s/\[\]<>(){}%]*)
    | (?P<hex><[0-9A-Fa-f\s]*>)
    | (?P<open><<)
    | (?P<close>>>)
    | (?P<arr>[\[\]])
    | (?P<op>[A-Za-z*'"][A-Za-z0-9*'"]*)
""", re.X)

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def concat(m, ctm):
    """PDF matrix concatenation: new CTM = m x ctm."""
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = ctm
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def unit_square_bounds(ctm):
    """An image fills the unit square; map its corners through the CTM."""
    a, b, c, d, e, f = ctm
    ys = [f, b + f, d + f, b + d + f]
    xs = [e, a + e, c + e, a + c + e]
    return min(xs), min(ys), max(xs), max(ys)


def skip_string(buf, i):
    """Advance past a ( ... ) literal string, honouring escapes and nesting."""
    depth, i = 1, i + 1
    while i < len(buf) and depth:
        ch = buf[i:i + 1]
        if ch == b"\\":
            i += 2
            continue
        if ch == b"(":
            depth += 1
        elif ch == b")":
            depth -= 1
        i += 1
    return i


def walk(stream, xobjs, objs, ctm, hits, depth=0):
    """Collect (objnum, y_top) for every image drawn, following Form XObjects."""
    if depth > 6:
        return
    stack, operands, i = [], [], 0
    n = len(stream)
    while i < n:
        ch = stream[i:i + 1]
        if ch == b"(":
            i = skip_string(stream, i)
            operands.append(None)
            continue
        if ch in b" \t\r\n\f\x00":
            i += 1
            continue
        if ch == b"%":
            nl = stream.find(b"\n", i)
            i = n if nl < 0 else nl + 1
            continue

        m = TOKEN.match(stream, i)
        if not m:
            i += 1
            continue
        i = m.end()

        if m.lastgroup == "num":
            operands.append(float(m.group("num")))
            continue
        if m.lastgroup == "name":
            operands.append("/" + m.group("name").decode("latin-1"))
            continue
        if m.lastgroup != "op":
            operands.append(None)
            continue

        op = m.group("op")
        if op == b"q":
            stack.append(ctm)
        elif op == b"Q":
            ctm = stack.pop() if stack else IDENTITY
        elif op == b"cm":
            nums = [x for x in operands[-6:] if isinstance(x, float)]
            if len(nums) == 6:
                ctm = concat(tuple(nums), ctm)
        elif op == b"BI":
            # inline image: skip to the matching EI
            j = stream.find(b"EI", i)
            i = n if j < 0 else j + 2
        elif op == b"Do":
            name = next((o for o in reversed(operands)
                         if isinstance(o, str) and o.startswith("/")), None)
            if name:
                objnum = xobjs.get(name[1:])
                if objnum is not None:
                    body = objs.get(objnum, b"")
                    head = body.split(b"stream", 1)[0]
                    if re.search(rb"/Subtype\s*/Form", head):
                        sub_ctm = ctm
                        fm = re.search(
                            rb"/Matrix\s*\[\s*([\d.\-\s]+?)\]", head)
                        if fm:
                            vals = [float(v) for v in fm.group(1).split()]
                            if len(vals) == 6:
                                sub_ctm = concat(tuple(vals), ctm)
                        res = resolve(objs, head, b"Resources")
                        sub_xo = {nm.decode(): int(o) for nm, o in re.findall(
                            rb"/(\w+)\s+(\d+)\s+0\s+R",
                            resolve(objs, res, b"XObject") if res else b"")}
                        walk(inflate_stream(body), sub_xo or xobjs, objs,
                             sub_ctm, hits, depth + 1)
                    elif re.search(rb"/Subtype\s*/Image", head) or b"/Image" in head:
                        _, _, _, y_top = unit_square_bounds(ctm)
                        hits.append((objnum, y_top))
        operands = []


def objects(data):
    out = {}
    for m in OBJ.finditer(data):
        out[int(m.group(1))] = m.group(2)
    return out


def inflate_stream(body):
    m = re.search(rb"stream\r?\n", body)
    if not m:
        return b""
    raw = body[m.end():]
    end = raw.rfind(b"endstream")
    if end > 0:
        raw = raw[:end]
    if b"/FlateDecode" in body.split(b"stream", 1)[0]:
        try:
            return zlib.decompress(raw)
        except zlib.error:
            try:
                return zlib.decompressobj().decompress(raw)
            except zlib.error:
                return b""
    return raw


def resolve(objs, body, key):
    """Return the dict text for /key, following one level of indirection."""
    m = re.search(rb"/" + key + rb"\s+(\d+)\s+0\s+R", body)
    if m:
        return objs.get(int(m.group(1)), b"")
    m = re.search(rb"/" + key + rb"\s*<<", body)
    if not m:
        return b""
    # balance the nested dictionary
    start = m.end() - 2
    depth, i = 0, start
    while i < len(body) - 1:
        if body[i:i + 2] == b"<<":
            depth += 1
            i += 2
            continue
        if body[i:i + 2] == b">>":
            depth -= 1
            i += 2
            if depth == 0:
                return body[start:i]
            continue
        i += 1
    return body[start:start + 4000]


def content_of(objs, page_body):
    m = re.search(rb"/Contents\s+(\d+)\s+0\s+R", page_body)
    if m:
        return inflate_stream(objs.get(int(m.group(1)), b""))
    m = re.search(rb"/Contents\s*\[([^\]]*)\]", page_body)
    if m:
        parts = re.findall(rb"(\d+)\s+0\s+R", m.group(1))
        return b"".join(inflate_stream(objs.get(int(p), b"")) for p in parts)
    return b""


def xobject_map(objs, page_body):
    res = resolve(objs, page_body, b"Resources")
    xo = resolve(objs, res, b"XObject") if res else b""
    return {n.decode(): int(o) for n, o in re.findall(rb"/(\w+)\s+(\d+)\s+0\s+R", xo)}


def text_above(pdf_path, page, y_top):
    """Body-column text lying above y_top on the given page."""
    cmd = [PDFTOTEXT, "-layout", "-nopgbrk", "-enc", "UTF-8",
           "-f", str(page), "-l", str(page),
           "-marginr", str(BODY_MARGIN_R), "-marginb", str(int(y_top)),
           pdf_path, "-"]
    res = subprocess.run(cmd, capture_output=True)
    return res.stdout.decode("utf-8", errors="replace")


def last_real_line(text):
    """The article line immediately above an image, or None at the article top."""
    lines = []
    for l in text.splitlines():
        s = l.strip()
        if not s or CHROME.match(s):
            continue
        if s.startswith("HOW TO:") or re.match(r"^Topic:", s):
            continue  # page title and topic label, not article prose
        lines.append(s)
    return lines[-1] if lines else None


def main():
    manifest_path = os.path.join(OUT, "images.json")
    if not os.path.exists(manifest_path):
        raise SystemExit("run pipeline/extract_images.py first")
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)

    kept_objs = {}
    for row in manifest:
        kept_objs.setdefault(row["doc_id"], {})[row["obj"]] = row

    pdfs = sorted(
        os.path.join(dp, f)
        for dp, _, fs in os.walk(INTAKE)
        for f in fs if f.lower().endswith(".pdf")
    )

    anchors = []
    for path in pdfs:
        m = re.match(r"#(\d+)", os.path.basename(path))
        doc_id = f"sol-{m.group(1)}" if m else None
        wanted = kept_objs.get(doc_id)
        if not wanted:
            continue

        data = open(path, "rb").read()
        objs = objects(data)

        # Page order. These files are 1-2 pages and produced by a browser print
        # driver, which emits pages in order, so object number ordering holds.
        page_nums = sorted(
            n for n, b in objs.items()
            if re.search(rb"/Type\s*/Page\b", b.split(b"stream", 1)[0])
            and not re.search(rb"/Type\s*/Pages\b", b.split(b"stream", 1)[0])
        )

        found = []
        for page_index, pnum in enumerate(page_nums, start=1):
            page_body = objs[pnum]
            names = xobject_map(objs, page_body)
            if not names:
                continue
            hits = []
            walk(content_of(objs, page_body), names, objs, IDENTITY, hits)
            for objnum, y_top in hits:
                if objnum in wanted:
                    found.append((page_index, y_top, objnum))

        # top of page down, page by page
        found.sort(key=lambda t: (t[0], -t[1]))

        for order, (page, y_top, objnum) in enumerate(found):
            txt = text_above(path, page, y_top)
            anchor = last_real_line(txt)
            row = wanted[objnum]
            anchors.append({
                "doc_id": doc_id,
                "obj": objnum,
                "image": row["image"],
                "width": row["width"],
                "height": row["height"],
                "page": page,
                "y_top": round(y_top, 1),
                "order": order,
                "anchor_text": anchor,
            })

        placed = len(found)
        total = len(wanted)
        flag = "" if placed == total else f"  <-- {total - placed} unplaced"
        print(f"{doc_id:9s} placed {placed}/{total}{flag}")

    with open(os.path.join(OUT, "anchors.json"), "w", encoding="utf-8") as fh:
        json.dump(anchors, fh, indent=2, ensure_ascii=False)

    noanchor = [a for a in anchors if not a["anchor_text"]]
    print(f"\n{len(anchors)} images anchored -> 01-ocr/anchors.json")
    print(f"{len(noanchor)} with no preceding body line (top of page)")

    print("\n--- sample: sol-403 (calibrate a scanner) ---")
    for a in [x for x in anchors if x["doc_id"] == "sol-403"]:
        print(f"  p{a['page']} y={a['y_top']:6.1f}  {os.path.basename(a['image']):24s} "
              f"<- {(a['anchor_text'] or '(top of page)')[:66]}")


if __name__ == "__main__":
    main()

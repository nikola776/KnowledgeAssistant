"""
Stage 1: PDF -> normalised markdown + metadata sidecar + chunks.jsonl

Zero third-party dependencies. Uses Xpdf's pdftotext (shipped with Git for Windows)
which lives under Program Files, so ThreatLocker permits it.

The saved PDFs are print-to-PDF of ServiceDesk Plus Solutions pages: A4 portrait,
595x842pt, with a two-column layout. The body is left of x=385 and a metadata
sidebar is right of it. pdftotext -layout interleaves the two, so each page is
extracted twice with margin crops and recombined.

Run:  python pipeline/normalise.py
"""

import json
import os
import re
import subprocess
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INTAKE = os.path.join(ROOT, "00-intake")
OUT = os.path.join(ROOT, "01-normalised")

PDFTOTEXT = r"C:\Program Files\Git\mingw64\bin\pdftotext.exe"

# Column geometry in points on a 595pt-wide page.
#
# Xpdf discards a text run unless it falls entirely inside the kept region, so the
# two bounds below are asymmetric on purpose. Body prose runs out to about 440pt;
# the sidebar's runs start at about 455pt. Cropping the body at 385 (the visually
# obvious gutter) silently truncated every wide body line, dropping words like
# "display correctly" mid-sentence while the loss check stayed at zero, because
# the missing words landed in the sidebar extract instead.
PAGE_W = 595.0
BODY_X = 460.0      # keep body runs ending before here; no sidebar run fits inside
SIDEBAR_X = 450.0   # keep runs starting after here; all sidebar labels do

# Portal chrome that carries no information.
CHROME = re.compile(
    r"^\s*(IT Helpdesk\s+Home Requests Solutions"
    r"|Feedback Keywords Associations"
    r"|Was this solution helpful\?.*"
    r"|Comments"
    r"|Comments for this solution have been disabled\."
    r"|Keywords"
    r"|Associations"
    r"|Linked Solutions"
    r"|Attachments?)\s*$",
    re.IGNORECASE,
)

CHUNK_TARGET = 1200
CHUNK_MAX = 1800
CHUNK_OVERLAP = 150

OCR_JSON = os.path.join(ROOT, "01-ocr", "ocr.json")
ANCHOR_JSON = os.path.join(ROOT, "01-ocr", "anchors.json")

# Below this, an image's OCR output is a stray word off a product photo rather
# than readable UI text. Product photos are common in this corpus.
OCR_MIN_CHARS = 25


def load_ocr():
    """(doc_id, image basename) -> OCR text, from pipeline/ocr.ps1 output."""
    if not os.path.exists(OCR_JSON):
        return {}
    with open(OCR_JSON, encoding="utf-8-sig") as fh:
        rows = json.load(fh)
    if isinstance(rows, dict):
        rows = [rows]
    out = {}
    for r in rows:
        text = (r.get("text") or "").strip()
        if len(text) < OCR_MIN_CHARS:
            continue
        out[(r["doc_id"], r.get("image", ""))] = text
    return out


def load_anchors():
    """doc_id -> [anchor rows in reading order], from pipeline/anchor_images.py."""
    if not os.path.exists(ANCHOR_JSON):
        return {}
    with open(ANCHOR_JSON, encoding="utf-8") as fh:
        rows = json.load(fh)
    out = defaultdict(list)
    for r in rows:
        out[r["doc_id"]].append(r)
    for k in out:
        out[k].sort(key=lambda r: r["order"])
    return out


def image_block(row, ocr_text, indent=""):
    """A screenshot rendered inline: viewable link plus its machine-read text.

    The link is relative to 01-normalised/ so the markdown previews correctly in
    Obsidian or an editor; chunk metadata carries the repo-relative path instead.
    """
    rel = "../" + row["image"]
    name = os.path.basename(row["image"])
    lines = [f"{indent}![{name}]({rel})"]
    if ocr_text:
        lines.append("")
        lines.append(f"{indent}> Screenshot text (OCR, layout order may differ):")
        for l in ocr_text.splitlines():
            if l.strip():
                lines.append(f"{indent}> {l.strip()}")
    return "\n".join(lines)


def illustrate(body, rows, ocr_map, doc_id):
    """Insert each screenshot into the prose directly beneath the line it illustrates.

    Anchors are consumed in reading order, so repeated anchor text (several steps
    that all say "Press Calibrate") still resolves to the right screenshot.
    """
    pending = list(rows)
    out, placed = [], []

    for line in body.splitlines():
        out.append(line)
        stripped = line.strip()
        if not stripped or not pending:
            continue
        # match the earliest unconsumed anchor for this exact line
        for idx, row in enumerate(pending):
            if (row.get("anchor_text") or "").strip() == stripped:
                ocr_text = ocr_map.get((doc_id, os.path.basename(row["image"])), "")
                out.append("")
                out.append(image_block(row, ocr_text))
                out.append("")
                placed.append(row)
                del pending[:idx + 1]
                break

    unplaced = [r for r in pending]
    if unplaced:
        out.append("")
        out.append("## Further screenshots")
        out.append("")
        out.append("Present in the article but not tied to a specific step.")
        out.append("")
        for row in unplaced:
            ocr_text = ocr_map.get((doc_id, os.path.basename(row["image"])), "")
            out.append(image_block(row, ocr_text))
            out.append("")

    return "\n".join(out), placed, unplaced


def run_pdftotext(path, *extra):
    """Extract text, returning stdout as str. '-' sends output to stdout."""
    cmd = [PDFTOTEXT, "-layout", "-nopgbrk", "-enc", "UTF-8", *extra, path, "-"]
    res = subprocess.run(cmd, capture_output=True)
    if res.returncode != 0 and not res.stdout:
        raise RuntimeError(f"pdftotext failed on {path}: {res.stderr.decode(errors='replace')}")
    return res.stdout.decode("utf-8", errors="replace")


def count_images(path):
    with open(path, "rb") as fh:
        return len(re.findall(rb"/Subtype\s*/Image", fh.read()))


def parse_sidebar(text):
    """Pull structured fields out of the right-hand metadata column."""
    meta = {}
    m = re.search(r"Solution ID\s+(\d+)", text)
    if m:
        meta["solution_id"] = int(m.group(1))
    m = re.search(r"Views\s+(\d+)", text)
    if m:
        meta["views"] = int(m.group(1))

    # "Created By\n<Name> on Jan 27, 2025 12:26 PM"
    for label, key in (("Created By", "created"), ("Last Updated By", "updated")):
        m = re.search(
            label + r"\s*\n\s*(.+?)\s+on\s+([A-Z][a-z]{2} \d{1,2}, \d{4})",
            text,
        )
        if m:
            meta[key + "_by"] = m.group(1).strip()
            meta[key] = normalise_date(m.group(2))
    return meta


MONTHS = {m: i + 1 for i, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split())}


def normalise_date(s):
    m = re.match(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4})", s)
    if not m:
        return s
    return f"{m.group(3)}-{MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"


def parse_body(text):
    """Strip chrome, then split off title and topic from the article body."""
    lines = [l.rstrip() for l in text.splitlines()]
    lines = [l for l in lines if not CHROME.match(l)]

    title, topic, body = None, None, []
    for line in lines:
        s = line.strip()
        if not s:
            if body:
                body.append("")
            continue
        if title is None:
            title = s
            continue
        m = re.match(r"^Topic:\s*(.+)$", s)
        if m and topic is None:
            topic = m.group(1).strip()
            continue
        body.append(line)

    # collapse runs of blank lines, drop leading/trailing blanks
    out, blank = [], False
    for line in body:
        if line.strip():
            out.append(line)
            blank = False
        elif out and not blank:
            out.append("")
            blank = True
    while out and not out[-1].strip():
        out.pop()
    return title, topic, "\n".join(out)


def detect_scanner(text):
    """TRIOS generation / Unite version mentioned, for pre-filtering retrieval."""
    tags = set()
    for pat, tag in (
        (r"\bTRIOS\s*3\b", "TRIOS 3"),
        (r"\bTRIOS\s*4\b", "TRIOS 4"),
        (r"\bTRIOS\s*5\b", "TRIOS 5"),
        (r"\bUnite\s*3\b", "Unite 3"),
        (r"\bUnite\b", "Unite"),
        (r"\bCommunicate\b", "Communicate"),
    ):
        if re.search(pat, text, re.IGNORECASE):
            tags.add(tag)
    if {"TRIOS 3", "TRIOS 4", "TRIOS 5"} & tags:
        tags.discard("TRIOS")
    return sorted(tags)


def find_references(body, own_title):
    """Other Solutions articles referenced by title in the prose."""
    refs = set()
    for m in re.finditer(r"HOW TO:\s*([^\n]+)", body):
        t = m.group(1).strip().rstrip(".")
        t = re.sub(r"\s{2,}.*$", "", t)  # drop trailing layout gutter text
        if t and t.lower() != (own_title or "").lower().replace("how to:", "").strip():
            refs.add(t)
    return sorted(refs)


# ---------------------------------------------------------------- redaction

EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
PHONE = re.compile(r"\b(?:0\d{3}[\s-]?\d{3}[\s-]?\d{3,4}|\+44\s?\d{2,4}[\s\d]{6,10})\b")
NHS = re.compile(r"\b\d{3}[\s-]?\d{3}[\s-]?\d{4}\b")
PRACTICE = re.compile(
    r"\b([A-Z][\w'&-]+(?:\s+[A-Z][\w'&-]+){0,3}\s+"
    r"(?:Dental (?:Practice|Surgery|Care|Centre)|Surgery|Orthodontics))\b"
)


def redact(text, names):
    """Remove PII. Returns (text, list of what was hit)."""
    hits = []

    def sub(pattern, repl, label, s):
        found = pattern.findall(s)
        if found:
            hits.append(f"{label}x{len(found)}")
        return pattern.sub(repl, s)

    for name in sorted(names, key=len, reverse=True):
        if not name:
            continue
        pat = re.compile(r"\b" + re.escape(name) + r"\b")
        if pat.search(text):
            hits.append("name")
            text = pat.sub("[NAME]", text)

    text = sub(EMAIL, "[EMAIL]", "email", text)
    text = sub(PHONE, "[PHONE]", "phone", text)
    text = sub(NHS, "[ID]", "nhs?", text)
    text = sub(PRACTICE, "[PRACTICE]", "practice?", text)
    return text, sorted(set(hits))


# ---------------------------------------------------------------- chunking


def chunk_article(doc, body):
    """Short articles stay whole; long ones split on paragraph boundaries.

    Support procedures lose meaning when their steps are split, so the bias is
    strongly toward keeping an article in one piece.
    """
    header = f"{doc['title']}\nTopic: {doc.get('topic') or 'unknown'}\n\n"

    if len(body) <= CHUNK_MAX:
        return [header + body]

    paras = [p for p in body.split("\n\n") if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if cur and len(cur) + len(p) + 2 > CHUNK_TARGET:
            chunks.append(cur)
            tail = cur[-CHUNK_OVERLAP:]
            cur = tail + "\n\n" + p
        else:
            cur = (cur + "\n\n" + p) if cur else p
    if cur.strip():
        chunks.append(cur)
    return [header + c for c in chunks]


# ---------------------------------------------------------------- main


def main():
    if not os.path.exists(PDFTOTEXT):
        sys.exit(f"pdftotext not found at {PDFTOTEXT}")
    os.makedirs(OUT, exist_ok=True)

    pdfs = []
    for dirpath, _, files in os.walk(INTAKE):
        for f in sorted(files):
            if f.lower().endswith(".pdf"):
                pdfs.append(os.path.join(dirpath, f))
    if not pdfs:
        sys.exit(f"no PDFs under {INTAKE}")

    # --- pass 1: extract, and collect colleague names for redaction
    raw = []
    names = set()
    for path in sorted(pdfs):
        full = run_pdftotext(path)
        body_txt = run_pdftotext(path, "-marginr", str(int(PAGE_W - BODY_X)))
        side_txt = run_pdftotext(path, "-marginl", str(int(SIDEBAR_X)))
        meta = parse_sidebar(side_txt)
        names.update(v for k, v in meta.items() if k.endswith("_by"))
        raw.append((path, full, body_txt, side_txt, meta))

    ocr_map = load_ocr()
    anchor_map = load_anchors()
    if not ocr_map:
        print("no 01-ocr/ocr.json; run pipeline/ocr.ps1 to add screenshot text")
    if not anchor_map:
        print("no 01-ocr/anchors.json; run pipeline/anchor_images.py to place images")
    if ocr_map or anchor_map:
        print(f"screenshots: OCR text for {len(ocr_map)} images, "
              f"positions for {sum(len(v) for v in anchor_map.values())} images "
              f"across {len(anchor_map)} articles")

    # Screenshots of the portal and of Unite show account names and email
    # addresses, so OCR text is redacted once here before it is placed anywhere.
    ocr_hits_by_doc = defaultdict(set)
    for key, text in list(ocr_map.items()):
        clean, hits_ = redact(text, names)
        ocr_map[key] = clean
        if hits_:
            ocr_hits_by_doc[key[0]].update(hits_)
    print()

    # --- pass 2: parse, redact, write
    docs, chunks, report, bundle = [], [], [], []
    for path, full, body_txt, side_txt, meta in raw:
        fname = os.path.basename(path)
        title, topic, body = parse_body(body_txt)

        sid = meta.get("solution_id")
        if sid is None:
            m = re.match(r"#(\d+)", fname)
            sid = int(m.group(1)) if m else None
        doc_id = f"sol-{sid}" if sid else os.path.splitext(fname)[0]

        refs = find_references(body, title)

        body, hits = redact(body, names)
        hits = sorted(set(hits) | ocr_hits_by_doc.get(doc_id, set()))

        img_rows = anchor_map.get(doc_id, [])
        body_full, placed, unplaced = illustrate(body, img_rows, ocr_map, doc_id)
        ocr_chars = sum(
            len(ocr_map.get((doc_id, os.path.basename(r["image"])), ""))
            for r in img_rows
        )

        doc = {
            "doc_id": doc_id,
            "source_type": "kb",
            "source_file": os.path.relpath(path, ROOT),
            "source_url": (
                f"https://myservicedesk.mydentist.co.uk/ui/solutions/{sid}"
                if sid else None
            ),
            "title": title,
            "topic": topic,
            "topic_path": f"Clinical Systems/Imaging/IOS Devices/{topic or 'unknown'}",
            "scanner": detect_scanner(title + "\n" + body),
            "views": meta.get("views"),
            "created": meta.get("created"),
            "updated": meta.get("updated"),
            "references": refs,
            "redacted": hits,
            "body_chars": len(body),
            "ocr_chars": ocr_chars,
            "screenshots": len(img_rows),
            "screenshots_placed": len(placed),
            "images": count_images(path),
        }
        docs.append(doc)

        fm = {k: v for k, v in doc.items() if k not in ("body_chars",)}
        md = "---\n" + json.dumps(fm, indent=2, ensure_ascii=False) + "\n---\n\n"
        md += f"# {title}\n\n{body_full}\n"
        with open(os.path.join(OUT, doc_id + ".md"), "w", encoding="utf-8") as fh:
            fh.write(md)
        with open(os.path.join(OUT, doc_id + ".json"), "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=False)

        bundle.append(
            f"## [{doc_id}] {title}\n"
            f"Topic: {topic or 'unknown'} | Updated: {doc['updated']} | "
            f"Screenshots: {len(img_rows)} | {doc['source_url']}\n\n{body_full}\n"
        )

        shared = {k: doc[k] for k in (
            "doc_id", "source_url", "title", "topic", "topic_path",
            "scanner", "updated")}

        for i, ctext in enumerate(chunk_article(doc, body_full)):
            chunks.append({
                "chunk_id": f"{doc_id}#{i}",
                "text": ctext,
                "source_type": "kb",
                "image": None,
                **shared,
            })

        # One chunk per screenshot, so an image can be retrieved on its own merits
        # and returned alongside the answer. The retrieval text deliberately
        # combines the step it illustrates with the words visible in it: "which
        # button do I press" matches the caption, "what does X look like" matches
        # the OCR.
        for row in img_rows:
            name = os.path.basename(row["image"])
            ocr_text = ocr_map.get((doc_id, name), "")
            step = (row.get("anchor_text") or "").strip()
            parts = [f"Screenshot from: {title}"]
            if step:
                parts.append(f"Illustrates this step: {step}")
            if ocr_text:
                parts.append("Visible on screen:\n" + ocr_text)
            if not step and not ocr_text:
                parts.append("Photograph with no readable text.")
            chunks.append({
                "chunk_id": f"{doc_id}#img-{row['order']}",
                "text": "\n\n".join(parts),
                "source_type": "screenshot",
                "image": row["image"],
                "image_size": f"{row['width']}x{row['height']}",
                "illustrates": step or None,
                "order": row["order"],
                **shared,
            })

        # column-crop sanity: body+sidebar should account for the full extract
        loss = len(re.sub(r"\s+", "", full)) - (
            len(re.sub(r"\s+", "", body_txt)) + len(re.sub(r"\s+", "", side_txt)))
        report.append((doc_id, title, doc["body_chars"], doc["ocr_chars"],
                       doc["images"], loss, hits, refs))

    with open(os.path.join(OUT, "chunks.jsonl"), "w", encoding="utf-8") as fh:
        for c in chunks:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")

    # Whole-corpus bundle. At this corpus size the entire KB fits in one prompt,
    # which beats retrieval outright. Kept in step with the chunked output so the
    # switch to a vector store is a decision about volume, not about format.
    bundle_text = (
        "# IOS (intra-oral scanner) support knowledge base\n\n"
        "Source: ManageEngine ServiceDesk Plus, Clinical Systems > Imaging > "
        "IOS Devices. Extracted text only; screenshots are not included, so "
        "articles with a high image count are incomplete.\n\n"
        + "\n---\n\n".join(bundle)
    )
    with open(os.path.join(OUT, "ALL-IOS-KB.md"), "w", encoding="utf-8") as fh:
        fh.write(bundle_text)

    # ------------------------------------------------------------- report
    print(f"{len(docs)} docs -> {len(chunks)} chunks\n")
    print(f"{'doc':9s} {'text':>6s} {'+ocr':>6s} {'total':>6s} {'loss':>5s}  title")
    thin, lossy = [], []
    for doc_id, title, chars, ocr_c, imgs, loss, hits, refs in report:
        flag = ""
        total_c = chars + ocr_c
        if total_c < 400:
            flag, _ = " THIN", thin.append((doc_id, title, total_c, imgs))
        if loss > 40:
            flag += " LOSS"
            lossy.append((doc_id, loss))
        print(f"{doc_id:9s} {chars:6d} {ocr_c:6d} {total_c:6d} {loss:5d}  "
              f"{title[:46]}{flag}")

    have = {d["doc_id"] for d in docs}
    titles = {(d["title"] or "").lower().replace("how to:", "").strip(): d["doc_id"]
              for d in docs}
    missing = defaultdict(list)
    for d in docs:
        for r in d["references"]:
            if r.lower().strip() not in titles:
                missing[r].append(d["doc_id"])

    print(f"\n--- referenced but NOT in corpus ({len(missing)}) ---")
    for r, srcs in sorted(missing.items(), key=lambda kv: -len(kv[1])):
        print(f"  {len(srcs)}x  HOW TO: {r}   <- {', '.join(srcs)}")

    if thin:
        print(f"\n--- still thin after OCR ({len(thin)}) ---")
        for doc_id, title, chars, imgs in thin:
            print(f"  {doc_id}  {chars} chars total  {title[:50]}")

    if lossy:
        print(f"\n--- column crop may be dropping text ({len(lossy)}) ---")
        for doc_id, loss in lossy:
            print(f"  {doc_id}  {loss} chars unaccounted")

    allhits = defaultdict(int)
    for *_, hits, _ in report:
        for h in hits:
            allhits[h.split("x")[0]] += 1
    print(f"\n--- redaction hits by type ---")
    print("  none" if not allhits else "  " + ", ".join(
        f"{k}: {v} docs" for k, v in sorted(allhits.items())))
    text_total = sum(r[2] for r in report)
    ocr_total = sum(r[3] for r in report)
    total = text_total + ocr_total
    print(f"\n--- corpus size ---")
    print(f"  PDF text layer:       {text_total:7,d} chars")
    print(f"  OCR from screenshots: {ocr_total:7,d} chars")
    print(f"  total:                {total:7,d} chars  (~{total // 4:,} tokens)")
    print(f"  bundle: {len(bundle_text):,} chars -> ALL-IOS-KB.md")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()

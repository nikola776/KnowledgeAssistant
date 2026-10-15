"""
OCR review: correct Azure's reading of the screenshots in the browser and
save the result as ground truth for `ocr_bench.py compare`.

Every word Azure read starts out correct. Click a wrong word to fix or delete it,
strike lines that are not on the screen, and add words Azure missed. When a
screenshot is marked done, its corrected text is written to
04-ocr-benchmark/ground-truth/<doc_id>/<image basename>.txt, and compare scores
every engine against it.

Starting from Azure's text is far quicker than typing from scratch, at a cost: an
Azure error you do not notice counts as correct, and Azure's line order becomes
the reference. Word precision and recall ignore order, so they stay fair; CER and
WER flatter Azure slightly.

Serves on 127.0.0.1 only. Edits are kept in 04-ocr-benchmark/review.json, which
is never committed: like the screenshots, it holds unredacted names and emails.

Run:  python pipeline/ocr_review.py
"""

import argparse
import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ocr_bench import BASELINE, BENCH, IMAGES, join_lines, list_images, load_rows, write_json

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "ocr_review.html")
GROUND_TRUTH = os.path.join(BENCH, "ground-truth")
REVIEW = os.path.join(BENCH, "review.json")

BASE_RUN = "azure-read"

# Shown collapsed under the editor: another engine reading a word Azure missed
# is the quickest way to notice it. (label, run folder or None for Windows, min conf)
HINT_RUNS = [("Windows", None, 0), ("EasyOCR", "easyocr", 0.5),
             ("Tesseract x2", "tesseract-x2", 0.5), ("Unlimited-OCR", "unlimited-ocr-base", 0)]

# Every screenshot in the corpus. The page skips the ones already done, so the
# first 20-image sample (the easy ones went first) carries straight on.
ALL = [(doc_id, os.path.basename(path)) for doc_id, path in list_images()]
KNOWN = set(ALL)

lock = threading.Lock()


def load_review():
    if not os.path.exists(REVIEW):
        return {}
    with open(REVIEW, encoding="utf-8") as fh:
        return json.load(fh)


def run_rows(name):
    path = BASELINE if name is None else os.path.join(BENCH, name, "ocr.json")
    return load_rows(path) if os.path.exists(path) else None


def items():
    base = run_rows(BASE_RUN)
    if base is None:
        raise SystemExit(f"no 04-ocr-benchmark/{BASE_RUN}/ocr.json: "
                         f"run `python pipeline/ocr_bench.py run azure` first")
    hints = [(label, run_rows(name), mc) for label, name, mc in HINT_RUNS]
    review = load_review()
    out = []
    for key in ALL:
        lines, confs, boxes = {}, {}, {}
        for s in base[key]["segments"]:
            lines.setdefault(s["line"], []).append(s["text"])
            confs.setdefault(s["line"], []).append(s["conf"])
            boxes.setdefault(s["line"], []).append(s.get("box"))
        item_id = "/".join(key)
        out.append({
            "id": item_id,
            "lines": [lines[n] for n in sorted(lines)],
            "conf": [confs[n] for n in sorted(confs)],  # per word, for flagging
            "boxes": [boxes[n] for n in sorted(boxes)],  # per word outline, image pixels
            "hints": [{"name": label, "text": join_lines(rows[key].get("segments", []), mc)
                       if "segments" in rows[key] else rows[key].get("text", "")}
                      for label, rows, mc in hints if rows and key in rows],
            "review": review.get(item_id),
        })
    return out


def ground_truth_text(lines, state):
    """Azure's lines with the reviewer's corrections applied, plus missed words."""
    edits, struck = state.get("edits", {}), set(state.get("struck", []))
    out = []
    for li, words in enumerate(lines):
        if li in struck:
            continue
        kept = [edits.get(f"{li}:{wi}", w).strip() for wi, w in enumerate(words)]
        if any(kept):
            out.append(" ".join(w for w in kept if w))
    out += [line.strip() for line in state.get("missing", "").splitlines() if line.strip()]
    return "\n".join(out)


def save(item_id, state):
    doc_id, image = item_id.split("/")
    base = run_rows(BASE_RUN)[(doc_id, image)]
    lines = {}
    for s in base["segments"]:
        lines.setdefault(s["line"], []).append(s["text"])
    lines = [lines[n] for n in sorted(lines)]
    gt = os.path.join(GROUND_TRUTH, doc_id, image[:-4] + ".txt")
    with lock:
        review = load_review()
        was_done = (review.get(item_id) or {}).get("done")
        review[item_id] = state
        write_json(REVIEW, review)
        if state.get("done"):
            os.makedirs(os.path.dirname(gt), exist_ok=True)
            with open(gt, "w", encoding="utf-8") as fh:
                fh.write(ground_truth_text(lines, state) + "\n")
        elif was_done and os.path.exists(gt):
            os.remove(gt)  # reopened: it is no longer checked


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            with open(PAGE, "rb") as fh:
                return self.send(200, fh.read(), "text/html; charset=utf-8")
        if self.path == "/api/items":
            return self.send(200, json.dumps(items()).encode(), "application/json")
        if self.path.startswith("/img/"):
            key = tuple(self.path[len("/img/"):].split("/"))
            if key in KNOWN:  # only corpus screenshots, so no path tricks reach the disk
                with open(os.path.join(IMAGES, *key), "rb") as fh:
                    return self.send(200, fh.read(), "image/png")
        self.send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path != "/api/review":
            return self.send(404, b"not found", "text/plain")
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if tuple(body["id"].split("/")) not in KNOWN:
            return self.send(400, b"unknown image", "text/plain")
        save(body["id"], body["review"])
        self.send(200, b"{}", "application/json")

    def log_message(self, *args):
        pass  # one line per click is noise


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="do not open a browser")
    args = ap.parse_args()
    items()  # fail now, not on the first page load, if the Azure run is missing
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"OCR review on {url}  (Ctrl+C to stop; edits save as you go)")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

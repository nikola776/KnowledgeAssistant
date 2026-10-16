"""
Stage 2: ask a question, get the answer text and the screenshots that go with it.

BM25 over 01-normalised/chunks.jsonl. Lexical, not semantic, and stdlib only, so
it runs on a locked-down machine with nothing installed. At ~130 chunks of
support prose this is not the compromise it sounds like: the vocabulary is small
and highly specific ("calibrate", "Unite", "virtual keyboard", "scanner tips"),
which is exactly where BM25 is strong. Swap in embeddings when the corpus grows
past the point where the whole thing fits in a prompt.

Screenshots are ranked as first-class results. Each has its own chunk whose text
combines the step it illustrates with the words visible in it, so "which button
do I press" matches the caption and "what does X look like" matches the OCR.

Usage:
  python pipeline/query.py "how do I calibrate a scanner"
  python pipeline/query.py "scanner is running slowly" --n 5
  python pipeline/query.py "sign in to unite" --html result.html
"""

import argparse
import html
import json
import math
import os
import re
import webbrowser
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHUNKS = os.path.join(ROOT, "01-normalised", "chunks.jsonl")

K1, B = 1.5, 0.75

STOP = set("""a an and are as at be but by for from how i if in into is it its of on or
that the their then there these this to was what when where which who will with you your
do does did can could should would my me we our us""".split())

TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text):
    return [t for t in TOKEN.findall(text.lower()) if t not in STOP and len(t) > 1]


def load():
    if not os.path.exists(CHUNKS):
        raise SystemExit("run pipeline/normalise.py first")
    rows = []
    with open(CHUNKS, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


class Bm25:
    def __init__(self, docs):
        self.docs = docs
        self.tf = [Counter(tokenize(d["text"])) for d in docs]
        self.len = [sum(c.values()) for c in self.tf]
        self.avg = (sum(self.len) / len(self.len)) if self.len else 0.0
        df = Counter()
        for c in self.tf:
            df.update(c.keys())
        n = len(docs)
        self.idf = {
            t: math.log(1 + (n - v + 0.5) / (v + 0.5)) for t, v in df.items()
        }

    def score(self, query):
        q = tokenize(query)
        out = []
        for i, tf in enumerate(self.tf):
            s = 0.0
            dl = self.len[i] or 1
            for t in q:
                f = tf.get(t)
                if not f:
                    continue
                s += self.idf.get(t, 0.0) * (f * (K1 + 1)) / (
                    f + K1 * (1 - B + B * dl / self.avg)
                )
            if s > 0:
                out.append((s, i))
        out.sort(reverse=True)
        return out


def render_html(question, articles, shots, path):
    """A result page whose images are referenced on disk, so it works offline."""
    def img_src(rel):
        # paths in chunks.jsonl are repo-relative; the html sits at the repo root
        return html.escape(rel)

    parts = [
        "<!doctype html><meta charset='utf-8'>",
        "<title>IOS support: " + html.escape(question) + "</title>",
        """<style>
        body{font-family:'Segoe UI',system-ui,sans-serif;max-width:900px;margin:2rem auto;
             padding:0 1rem;color:#404846;line-height:1.5}
        h1{font-size:1.3rem;color:#404846}
        h2{font-size:1.05rem;margin-top:2rem;border-bottom:2px solid #66C1BF;
           padding-bottom:.3rem}
        .q{background:#66C1BF;color:#fff;padding:.7rem 1rem;border-radius:6px;
           font-weight:600}
        .meta{color:#869492;font-size:.85rem}
        pre{white-space:pre-wrap;background:#f6f8f8;padding:.8rem;border-radius:6px;
            font-family:inherit;font-size:.92rem}
        .shot{margin:1.2rem 0;border:1px solid #dfe5e4;border-radius:6px;overflow:hidden}
        .shot img{width:100%;display:block}
        .cap{padding:.6rem .8rem;background:#f6f8f8;font-size:.88rem}
        .cap b{color:#BA4E97}
        a{color:#BA4E97}
        </style>""",
        f"<h1>IOS support</h1><p class='q'>{html.escape(question)}</p>",
    ]

    parts.append("<h2>Matching articles</h2>")
    if not articles:
        parts.append("<p class='meta'>No article matched.</p>")
    for s, c in articles:
        parts.append(
            f"<h3>{html.escape(c['title'] or c['doc_id'])} "
            f"<span class='meta'>score {s:.1f}</span></h3>"
            f"<p class='meta'>{html.escape(c['doc_id'])} &middot; "
            f"<a href='{html.escape(c.get('source_url') or '')}'>open in service desk</a>"
            f"</p><pre>{html.escape(c['text'])}</pre>"
        )

    parts.append("<h2>Screenshots</h2>")
    if not shots:
        parts.append("<p class='meta'>No screenshot matched.</p>")
    for s, c in shots:
        cap = c.get("illustrates") or "(no caption)"
        parts.append(
            f"<div class='shot'><img src='{img_src(c['image'])}' alt=''>"
            f"<div class='cap'><b>{html.escape(c['title'] or '')}</b><br>"
            f"{html.escape(cap)} <span class='meta'>&middot; score {s:.1f} &middot; "
            f"{html.escape(c.get('image_size') or '')}</span></div></div>"
        )

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--n", type=int, default=3, help="articles to return")
    ap.add_argument("--shots", type=int, default=4,
                    help="maximum screenshots to return")
    ap.add_argument("--floor", type=float, default=0.35,
                    help="drop screenshots scoring below this fraction of the "
                         "best screenshot (0 disables)")
    ap.add_argument("--scanner", help="filter to a model, e.g. 'TRIOS 3'")
    ap.add_argument("--html", help="write a result page with the images inline")
    ap.add_argument("--open", action="store_true", help="open the page in a browser")
    args = ap.parse_args()

    rows = load()
    if args.scanner:
        rows = [r for r in rows if args.scanner in (r.get("scanner") or [])]
        if not rows:
            raise SystemExit(f"nothing tagged {args.scanner!r}")

    bm = Bm25(rows)
    ranked = bm.score(args.question)
    if not ranked:
        raise SystemExit("no match. try different words.")

    arts = [(s, rows[i]) for s, i in ranked if rows[i]["source_type"] == "kb"]
    shots = [(s, rows[i]) for s, i in ranked if rows[i]["source_type"] == "screenshot"]

    # Keep one chunk per article so three results mean three articles.
    seen, arts_top = set(), []
    for s, c in arts:
        if c["doc_id"] in seen:
            continue
        seen.add(c["doc_id"])
        arts_top.append((s, c))
        if len(arts_top) >= args.n:
            break

    # A screenshot from a top-ranked article is more likely to be the one the
    # user needs, so give those a modest lift rather than ranking images alone.
    top_docs = {c["doc_id"] for _, c in arts_top}
    shots = sorted(
        ((s * (1.6 if c["doc_id"] in top_docs else 1.0), c) for s, c in shots),
        key=lambda t: -t[0],
    )

    # Relevance floor, relative to the best screenshot for this question.
    #
    # --shots on its own is a fixed count, so a question whose answer has one
    # illustration still returned three, padding the result with screenshots from
    # unrelated articles. Showing a practice a picture of the account-verification
    # screen when they asked about the keyboard is worse than showing nothing.
    #
    # The floor is relative because BM25 scores are not comparable between
    # queries: a rare-term question scores far higher than a common-term one, so
    # any absolute cutoff would either admit noise or suppress good matches
    # depending on the wording.
    dropped = 0
    if shots and args.floor > 0:
        cutoff = shots[0][0] * args.floor
        kept = [(s, c) for s, c in shots if s >= cutoff]
        dropped = len(shots) - len(kept)
        shots = kept
    shots = shots[:args.shots]

    print(f"\nQ: {args.question}\n")
    print("--- articles ---")
    for s, c in arts_top:
        print(f"\n[{s:5.1f}] {c['doc_id']}  {c['title']}")
        print(f"        {c.get('source_url')}")
        body = re.sub(r"\n{2,}", "\n", c["text"]).strip()
        for line in body.splitlines()[1:9]:
            print(f"        {line[:96]}")

    note = f"  ({dropped} below the {args.floor:.0%} floor)" if dropped else ""
    print(f"\n--- screenshots to show ---{note}")
    if not shots:
        print("  (none matched)")
    for s, c in shots:
        print(f"\n[{s:5.1f}] {c['image']}  ({c.get('image_size')})")
        print(f"        from {c['doc_id']}: {c.get('illustrates') or '(no caption)'}")

    if args.html:
        out = args.html if os.path.isabs(args.html) else os.path.join(ROOT, args.html)
        render_html(args.question, arts_top, shots, out)
        print(f"\nwrote {out}")
        if args.open:
            webbrowser.open("file:///" + out.replace("\\", "/"))


if __name__ == "__main__":
    main()

"""
Automatic ("silver") ground truth: Azure's reading of each screenshot, cleaned up
by agreement with the other engines and a vocabulary. A backup for, and check
on, the hand-checked ground truth that ocr_review.py produces.

For every word Azure read:
  - kept if another engine read it too, it is a known word, or Azure was sure;
  - otherwise replaced by the closest word another engine read on the same
    screenshot, else the closest word in the KB articles;
  - otherwise dropped if Azure was unsure, kept if not.
Lines of stray characters no other engine saw are dropped (window controls like
"- 0 X"), and words Azure missed are added on a final line when three other
engines read them, or two did and the KB articles use the word.

Two rules were tried and removed after checking against the first 16 hand
labels: correcting to the nearest dictionary word ("acoma" -> "aroma") and
swapping a known word for a near one two engines read ("Wind" -> "Win"). Both
guessed wrong more often than right; dropping or keeping the word is safer.

"Known word": British and American English dictionaries plus the prose of the
KB articles. Not the articles' OCR blocks, which are Windows output and would
tilt the vote. That makes UI names like "TRIOS" and "Unite" known.

The result leans toward Azure and toward whatever the engines agree on: a
mistake every engine makes is invisible to it. So it is scored against the
hand-checked labels, and autotruth.md says how often its changes were right.

Writes 04-ocr-benchmark/ground-truth-auto/ (same format as ground-truth/) and
04-ocr-benchmark/autotruth.md. Both hold screenshot text, names and emails
included, so neither is committed.

Run:  python pipeline/ocr_autotruth.py
      python pipeline/ocr_bench.py compare --gt 04-ocr-benchmark/ground-truth-auto \\
          --report 04-ocr-benchmark/report-auto.md
"""

import glob
import os
import re
import shutil
from collections import Counter, defaultdict
from datetime import date

from ocr_bench import (BASELINE, BENCH, ROOT, edit_distance, join_lines, list_images,
                       load_ground_truth, load_rows, words)

BASE_RUN = "azure-read"
# One vote per engine. tesseract-x2 stands in for all Tesseract variants, so
# Tesseract does not get three votes. Confidence filters as in report.md.
VOTERS = [("Windows", None, 0), ("EasyOCR", "easyocr", 0.5),
          ("Tesseract", "tesseract-x2", 0.5), ("Unlimited-OCR", "unlimited-ocr-base", 0)]
SURE = 0.9        # Azure confidence at which an unsupported word is kept as read
UNSURE = 0.8      # same flag line as the review page
DROP_BELOW = 0.5  # unsupported, unknown, unfixable and below this: dropped
DICTS = ["/usr/share/dict/british-english", "/usr/share/dict/american-english"]
OUT = os.path.join(BENCH, "ground-truth-auto")
REPORT = os.path.join(BENCH, "autotruth.md")
WORD = re.compile(r"[A-Za-z0-9]+")


def load_vocab():
    """(dictionary words, KB article words), lowercased."""
    dictionary = set()
    for path in DICTS:
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="ignore") as fh:
                dictionary.update(w.strip().lower() for w in fh if w.strip().isalpha())
    domain = set()
    for md in glob.glob(os.path.join(ROOT, "01-normalised", "sol-*.md")):
        with open(md, encoding="utf-8") as fh:
            text = re.sub(r"\A---\n.*?\n---\n", "", fh.read(), flags=re.S)
        prose = [l for l in text.splitlines() if not l.startswith((">", "!["))]
        domain.update(words("\n".join(prose)))
    return dictionary, domain


def limit(core):
    """Edits allowed when correcting a word of this length."""
    return 1 if len(core) <= 4 else 2 if len(core) <= 8 else 3


def closest(core, pool, max_d):
    """(best word, distance) within max_d, or (None, None) if none or a tie."""
    best, best_d, tie = None, max_d + 1, False
    for w in pool:
        if w == core or abs(len(w) - len(core)) > max_d:
            continue
        d = edit_distance(core, w)
        if d < best_d:
            best, best_d, tie = w, d, False
        elif d == best_d:
            tie = True
    return (None, None) if best is None or tie else (best, best_d)


def match_case(orig, new):
    if len(orig) > 1 and orig.isupper():
        return new.upper()
    if orig[:1].isupper():
        return new[:1].upper() + new[1:]
    return new


def replace_core(text, core, new):
    """Swap the alphanumeric run spelling `core` in text, keeping punctuation around it."""
    for m in WORD.finditer(text):
        if m.group().lower() == core:
            return text[:m.start()] + match_case(m.group(), new) + text[m.end():]
    return text


class Fixer:
    def __init__(self):
        self.dictionary, self.domain = load_vocab()
        self.known = self.dictionary | self.domain

    def is_known(self, core):
        # Every single letter is in the dictionary, so a lone letter proves nothing.
        return core.isdigit() or (len(core) >= 2 and core in self.known)

    def word(self, text, conf, votes):
        """(new text or None, change or None, mark or None) for one Azure word.

        The mark is what ocr_review.py shows a reviewer: "fix" with a suggestion,
        "drop", or "check" for a word kept as read that nothing else supports.
        """
        cores = words(text)
        if not cores:  # pure symbols: "■", ">", "|"
            if conf >= SURE:
                return text, None, None
            return None, ("dropped", text, "symbol"), {"kind": "drop", "why": "a stray symbol"}
        if all(votes[c] or self.is_known(c) for c in cores):
            return text, None, None
        unsupported = (f"no other engine read it and it isn't a known word "
                       f"(Azure {conf:.0%} sure)")
        if conf >= SURE:
            return text, None, {"kind": "check", "why": unsupported}
        if len(cores) == 1 and len(cores[0]) >= 3:
            core = cores[0]
            alt, _ = closest(core, list(votes), limit(core))
            if alt:
                return self._swap(text, core, alt, f"read by {votes[alt]} other engine(s)")
            alt, _ = closest(core, self.domain, limit(core))
            if alt:
                return self._swap(text, core, alt, "used in the KB articles")
        if conf < DROP_BELOW:
            return (None, ("dropped", text, f"unknown, unsupported, Azure {conf:.0%} sure"),
                    {"kind": "drop", "why": unsupported})
        return text, None, {"kind": "check", "why": unsupported}

    def _swap(self, text, core, alt, why):
        new = replace_core(text, core, self.forms.get(alt, alt))
        return new, ("fixed", text, new, why), {"kind": "fix", "to": new, "why": why}

    def screenshot(self, base_row, voter_texts):
        """(silver text, changes, votes, marks, added) for one screenshot.

        marks are keyed "line:word" in the same numbering ocr_review.py uses:
        Azure's lines in order, words in order within each line.
        """
        votes, order, self.forms = Counter(), [], {}
        for text in voter_texts:
            cores = words(text)
            votes.update(set(cores))
            order += cores
            for m in WORD.finditer(text):
                self.forms.setdefault(m.group().lower(), m.group())
        lines = defaultdict(list)
        for s in base_row["segments"]:
            lines[s["line"]].append((s["text"], s["conf"]))

        out, log, marks = [], [], {}
        for li, n in enumerate(sorted(lines)):
            kept, kept_at = [], []
            for wi, (text, conf) in enumerate(lines[n]):
                new, change, mark = self.word(text, conf, votes)
                if change:
                    log.append(change)
                if mark:
                    marks[f"{li}:{wi}"] = mark
                if new:
                    kept.append(new)
                    kept_at.append(wi)
            cores = words(" ".join(kept))
            # Nothing but stray characters that no other engine saw: "X", ">", "0 :"
            if kept and all(len(c) < 2 for c in cores) and not any(votes[c] for c in cores):
                log.append(("dropped", " ".join(kept), "stray characters"))
                for wi in kept_at:
                    marks[f"{li}:{wi}"] = {"kind": "drop", "why": "stray characters no "
                                           "other engine read (window buttons, icons)"}
                continue
            if kept:
                out.append(" ".join(kept))

        have = set(words("\n".join(out)))
        added = []
        for c in dict.fromkeys(order):  # first-seen order, Windows first
            # Two votes alone let in "genera" (a cut-off "General" that two engines
            # read, and a real word): need a third engine or the KB's own vocabulary.
            if len(c) < 3 or c in have or c not in self.known:
                continue
            if votes[c] >= 3 or (votes[c] >= 2 and c in self.domain):
                added.append(self.forms.get(c, c))
        if added:
            out.append(" ".join(added))
            log.append(("added", " ".join(added), "other engines agree"))
        return "\n".join(out), log, votes, marks, added


def load_voters():
    """[(label, rows, min conf)] for the engines that have runs."""
    voters = []
    for label, run, mc in VOTERS:
        path = BASELINE if run is None else os.path.join(BENCH, run, "ocr.json")
        if os.path.exists(path):
            voters.append((label, load_rows(path), mc))
        else:
            print(f"note: no {run} run, voting without {label}")
    return voters


def voter_texts(voters, key):
    texts = []
    for _, rows, mc in voters:
        r = rows.get(key, {})
        texts.append(join_lines(r["segments"], mc) if "segments" in r else r.get("text") or "")
    return texts


def word_match(texts, gold):
    """Order-free word precision and recall of texts against gold, as compare scores it."""
    hit = n_hyp = n_ref = 0
    for k, ref in gold.items():
        rw, hw = words(ref), words(texts.get(k, ""))
        hit += sum((Counter(rw) & Counter(hw)).values())
        n_hyp += len(hw)
        n_ref += len(rw)
    return hit / max(1, n_hyp), hit / max(1, n_ref)


def pct(a, b):
    return f"{a / b:.0%}" if b else "-"


def main():
    base = load_rows(os.path.join(BENCH, BASE_RUN, "ocr.json"))
    voters = load_voters()
    fixer = Fixer()
    images = [(d, os.path.basename(p)) for d, p in list_images()]

    silver, logs, agree, missed = {}, {}, Counter(), {v[0]: Counter() for v in voters}
    reads = {v[0]: 0 for v in voters}
    only_azure = Counter()
    for key in images:
        texts = voter_texts(voters, key)
        silver[key], logs[key], votes, _, _ = fixer.screenshot(base[key], texts)
        final = words(silver[key])
        for c in final:
            agree[votes[c]] += 1
            if votes[c] == 0:
                only_azure[c] += 1
        for (label, _, _), text in zip(voters, texts):
            seen = set(words(text))
            reads[label] += sum(c in seen for c in final)
            missed[label].update(c for c in final if c not in seen and len(c) >= 3)

    shutil.rmtree(OUT, ignore_errors=True)
    for (doc_id, image), text in silver.items():
        os.makedirs(os.path.join(OUT, doc_id), exist_ok=True)
        with open(os.path.join(OUT, doc_id, image[:-4] + ".txt"), "w", encoding="utf-8") as fh:
            fh.write(text + ("\n" if text else ""))

    # --- report --------------------------------------------------------------
    counts = Counter(c[0] for log in logs.values() for c in log)
    total = sum(agree.values())
    md = [f"# Automatic ground truth ({date.today().isoformat()})", "",
          "Generated by `python pipeline/ocr_autotruth.py`. Holds screenshot text, names and "
          "emails included: local only, never committed. Files: `ground-truth-auto/`.", "",
          f"{len(images)} screenshots. Starting from Azure's {sum(len(r['segments']) for r in base.values())} "
          f"words: {counts['fixed']} corrected, {counts['dropped']} dropped (words, symbols or "
          f"stray lines), and {counts['added']} screenshots got words that other engines agreed "
          "Azure missed.", ""]

    gold = load_ground_truth(os.path.join(BENCH, "ground-truth"))
    gold = {k: v for k, v in gold.items() if k in silver}
    if gold:
        az = {k: join_lines(r["segments"]) for k, r in base.items()}
        p0, r0 = word_match(az, gold)
        p1, r1 = word_match(silver, gold)
        good, disagree = Counter(), []
        for k in gold:
            ref = set(words(gold[k]))
            for c in logs[k]:
                kind, before = c[0], c[1]
                after = c[2] if kind == "fixed" else ""
                if kind == "fixed":
                    ok = set(words(after)) <= ref and not set(words(before)) <= ref
                elif kind == "dropped":
                    ok = not (set(words(before)) & ref)
                else:
                    ok = bool(set(words(before)) & ref)
                good[(kind, ok)] += 1
                if not ok:
                    disagree.append((k, c))
        md += ["## Checked against the hand-checked labels", "",
               f"On the {len(gold)} screenshots labelled in the review page so far. Those labels "
               "also start from Azure, so treat this as a floor on what the cleanup fixes, not proof "
               "that what it kept is right.", "",
               "| | Word precision | Word recall |", "|---|---|---|",
               f"| Azure as read | {p0:.1%} | {r0:.1%} |",
               f"| After cleanup (silver) | {p1:.1%} | {r1:.1%} |", "",
               "| Change | Agrees with the labels | Disagrees |", "|---|---|---|"]
        for kind in ("fixed", "dropped", "added"):
            md.append(f"| {kind} | {good[(kind, True)]} | {good[(kind, False)]} |")
        md += ["", "Where they disagree, either side can be wrong: the labels keep any Azure "
               "word the reviewer left alone. Each one, to judge by eye:", ""]
        for (doc_id, image), c in disagree:
            what = {"fixed": f"cleanup `{c[1]}` → `{c[2]}`, label keeps `{c[1]}`",
                    "dropped": f"cleanup drops `{c[1]}`, label keeps it",
                    "added": f"cleanup adds `{c[1]}`, label lacks it"}[c[0]]
            md.append(f"- {doc_id}/{image}: {what}")
        md.append("")

    md += ["## How the engines agree", "",
           f"For each of the {total} words in the silver text, how many of the other "
           f"{len(voters)} engines also read it on that screenshot:", "",
           "| Other engines that read it | Words | Share |", "|---|---|---|"]
    for n in range(len(voters), -1, -1):
        name = {len(voters): "all (always extracted)", 0: "none (only Azure)"}.get(n, str(n))
        md.append(f"| {name} | {agree[n]} | {pct(agree[n], total)} |")
    known_only = sum(n for c, n in only_azure.items() if fixer.is_known(c))
    md += ["", f"Of the {sum(only_azure.values())} words only Azure read, {pct(known_only, sum(only_azure.values()))} "
           "are dictionary or KB words, so mostly real text the others could not read (the blurry "
           "screens), not Azure inventing it. Most common: "
           + ", ".join(f"`{c}`" for c, _ in only_azure.most_common(15)) + ".", "",
           "| Engine | Silver words it also read | Words it misses most |", "|---|---|---|"]
    for label in reads:
        md.append(f"| {label} | {pct(reads[label], total)} | "
                  + ", ".join(f"`{c}` ({n})" for c, n in missed[label].most_common(8)) + " |")

    md += ["", "## Changes per screenshot", ""]
    for (doc_id, image), log in logs.items():
        if not log:
            continue
        parts = []
        for c in log:
            if c[0] == "fixed":
                parts.append(f"fixed `{c[1]}` → `{c[2]}` ({c[3]})")
            elif c[0] == "dropped":
                parts.append(f"dropped `{c[1]}` ({c[2]})")
            else:
                parts.append(f"added `{c[1]}`")
        md.append(f"- **{doc_id}/{image}**: " + "; ".join(parts))
    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")

    print("\n".join(md[:md.index("## Changes per screenshot")]))
    print(f"wrote {os.path.relpath(OUT, ROOT)}/ and {os.path.relpath(REPORT, ROOT)}")


if __name__ == "__main__":
    main()

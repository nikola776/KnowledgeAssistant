# IOS support agent

Answer intra-oral scanner (IOS) support questions from the corpus in this folder, and show
the relevant screenshots.

The corpus is 3Shape TRIOS and Unite material extracted from ManageEngine ServiceDesk Plus
(Clinical Systems → Imaging → IOS Devices). It is not a general knowledge base: 25 articles,
about 10,000 tokens of text and 67 screenshots.

## How to answer a question

**Retrieve first. Never answer from memory about TRIOS or Unite.** Model behaviour differs
between TRIOS 3, 4 and 5, and between Unite versions, and guessing produces plausible wrong
instructions for a clinician mid-appointment.

```bash
python pipeline/query.py "<the user's question>" --n 3 --shots 3
```

That returns matching articles and a ranked list of screenshots that passed the relevance
floor. Then:

1. Read the full article from `01-normalised/sol-<id>.md` if the query excerpt is truncated.
2. Answer from that text, in the article's own order of steps.
3. **Show the screenshots.** Use the Read tool on each returned image path to display it
   inline. The captions tell you which step each one illustrates, so place them with the
   step they belong to rather than dumping them at the end.
4. Cite the `source_url` so the user can open the original article.

If the corpus does not cover the question, say so and point at the service desk. Do not
extrapolate from a neighbouring article.

## What is in each folder

| Path | Contents |
|---|---|
| `00-intake/` | The original print-to-PDF articles. Source of truth, do not edit. |
| `01-normalised/sol-*.md` | One article each: metadata frontmatter, prose, screenshots inline |
| `01-normalised/ALL-IOS-KB.md` | Whole corpus in one file. Read this to answer broad questions |
| `01-normalised/chunks.jsonl` | 127 retrieval units (60 article, 67 screenshot) |
| `01-ocr/images/sol-*/` | The screenshots as PNGs |
| `01-ocr/anchors.json` | Which step each screenshot illustrates |
| `pipeline/` | Extraction and retrieval scripts. See README.md |
| `04-ocr-benchmark/` | Tesseract and EasyOCR runs scored against the Windows OCR. Not used for answering |

For a question that spans several articles ("what should I check before escalating to
3Shape?"), reading `ALL-IOS-KB.md` whole is cheaper and better than three queries. The
corpus is small enough that this is often the right move.

## Things to be careful about

**The screenshots are not redacted.** OCR text in the markdown has had emails and names
removed, but the PNGs themselves still show them, notably in the 3Shape account and
verification screens (sol-396, sol-392) and the Unite header (sol-406, sol-412). Displaying
an image discloses whatever is in it. Do not paste screenshots into anything external.

**Four articles are nearly empty**: sol-385, sol-400, sol-410, sol-432. Their content sits
elsewhere (Wisdom, or a linked article that was never saved). Say the article is a stub
rather than presenting its two lines as the full answer.

**Three referenced articles are missing from the corpus**: high performance mode on TRIOS
laptops, WIFI & WIFI 2, and BIOS/driver updates. sol-400 points at all three, so any
performance question is only partly answerable here.

**OCR reading order follows on-screen layout**, not logical order, so screenshot text can
read oddly. Trust the prose over the OCR when they disagree.

## Do not

- Do not edit anything in `00-intake/`.
- Do not regenerate `01-normalised/` or `01-ocr/` unless new PDFs were added. Rerunning the
  pipeline is `normalise.py` → `extract_images.py` → `ocr.ps1` → `anchor_images.py` →
  `normalise.py` again (twice, because anchoring depends on the first pass).
- Do not give clinical advice. This covers device and software support only.

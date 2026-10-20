# IOS Support RAG

A local, file-based retrieval system over the intra-oral scanner (IOS) support corpus:
ServiceDesk Plus Solutions articles, their attachments, vendor documentation, and resolved
IOS tickets.

Deliberately **outside** the Obsidian vault. The vault schema forbids patient or customer
data, and the ticket corpus will contain practice names and patient-adjacent detail.

## Setting up on a new machine

Git holds only the code and docs. The corpus, screenshots and OCR text are internal support
material, and the screenshots are unredacted, so `.gitignore` keeps all of it out. A fresh
clone can't answer anything until the corpus is added.

### 1. Add the corpus from `IOS-RAG.zip`

`IOS-RAG.zip` is a snapshot of the whole project folder from 2026-09-21. Copy only its data
folders into the clone. Its `README.md`, `CLAUDE.md` and `pipeline/` are older than the
repo's, so leave those out.

```bash
unzip -q IOS-RAG.zip -d /tmp/ios-rag
chmod -R u+w /tmp/ios-rag   # the archive stores its folders read-only
for p in 00-intake 01-normalised 01-ocr 02-index 03-chatbot-upload result.html; do
  cp -r "/tmp/ios-rag/IOS-RAG/$p" .
done
git status --short          # prints nothing: all of it is ignored
python3 pipeline/query.py "how do I calibrate a scanner" --n 2 --shots 3   # smoke test
rm -rf /tmp/ios-rag
```

Never commit these folders.

The zip predates the OCR benchmark. It doesn't contain the per-engine run folders
(`04-ocr-benchmark/<run>/`) or the hand-labelled transcriptions
(`04-ocr-benchmark/ground-truth/<doc_id>/`). Without them, `ocr_bench.py compare` and the
scores in [OCR benchmark](#ocr-benchmark-2026-10-16) can't be reproduced. Answering
questions doesn't need them.

### 2. Create `.env` (Azure OCR only)

Only `pipeline/ocr_bench.py run azure` reads it. Keep comments on their own line: the
loader doesn't strip text after a value.

```
AZURE_DI_ENDPOINT=https://di-mydentist-ai-uks.cognitiveservices.azure.com/
AZURE_DI_TENANT=29d4f8a4-6bc4-4032-b2c1-5ed82e97e377
# Only for a resource that allows API keys. mydentist's doesn't.
# AZURE_DI_KEY=
```

### 3. Set up the Azure CLI (Azure OCR only)

mydentist's resource disables API keys, so the azure engine signs in with a Microsoft Entra
token from the Azure CLI.

```bash
pipx install azure-cli        # Linux. Windows: winget install --exact --id Microsoft.AzureCLI
az login --tenant mydentist.co.uk
```

The account needs the **Cognitive Services User** role on `di-mydentist-ai-uks`. Owner or
Contributor alone gets 401/403.

### What each task needs

| Task | Needs |
|---|---|
| Answering questions (`query.py`) | Python 3, standard library only, any OS. The corpus from step 1 |
| OCR benchmark: `tesseract`, `easyocr` | `tesseract` on PATH, or `pip install easyocr` (PyTorch). `--scale` needs Pillow |
| OCR benchmark: `azure` | Steps 2 and 3 |
| OCR benchmark: `unlimited-ocr` | NVIDIA GPU, Python 3.12, `pipeline/requirements-unlimited-ocr.txt` |
| Rebuilding the corpus from new PDFs | Windows: Xpdf's `pdftotext` from Git for Windows (path set in `PDFTOTEXT` in `normalise.py` and `anchor_images.py`), and the Windows OCR engine for `ocr.ps1` |

The rebuild doesn't run on Linux as-is. Its `pdftotext` is Poppler's, which has no
`-marginl`/`-marginr`, and `ocr.ps1` uses `Windows.Media.Ocr`. It's only needed when new
articles are added.

## Source

ManageEngine ServiceDesk Plus (Enterprise Service Management edition), not SharePoint.

- Portal: `https://myservicedesk.mydentist.co.uk/ESM.do?type=portal`
- Path: IT HelpDesk → Solutions → Clinical Systems → Imaging → IOS Devices

Neither browser surface available to Claude can reach it: the in-app browser blocks the
domain by policy, and Claude in Chrome is not connected. So intake is manual for now. If a
technician API key is ever granted, `GET /api/v3/solutions` replaces stages 0 and 1 entirely
and makes incremental re-sync trivial.

## Pipeline stages

```
00-intake/      you drop files here, as-saved, no renaming needed
01-normalised/  one .md per document + one .json sidecar of metadata
02-index/       FAISS or Chroma store (exclude this folder from OneDrive sync)
pipeline/       the scripts
```

## What to save where

| Folder | Put in it | Preferred format |
|---|---|---|
| `00-intake/kb-articles/` | Each Solutions article under IOS Devices | Print-to-PDF, or Ctrl+S "Webpage, complete" |
| `00-intake/attachments/` | Files attached to those articles | Whatever they are (PDF, DOCX, PNG) |
| `00-intake/tickets/` | Resolved IOS request/incident export | XLSX or CSV, one row per ticket |
| `00-intake/vendor-docs/` | Manufacturer manuals and release notes | PDF |

Two things worth doing while saving, because they cannot be recovered later:

1. **Keep the solution ID or URL.** Print-to-PDF usually puts the URL in the page footer,
   which is enough. Without it, retrieved answers cannot cite back to the service desk.
2. **Save the topic path** if the article sits somewhere other than IOS Devices.

Start with 3 or 4 representative articles plus one attachment rather than the whole set. The
normaliser gets written against real files, and the formats you actually produce determine
how it parses them.

## Ticket export columns that matter

Request ID, Subject, Description, Resolution, Category/Subcategory/Item, Created and
Resolved time, Asset or CI, Site or practice, Status, Technician. Resolution plus Description
is the retrieval payload; the rest becomes filter metadata.

## Chunk metadata schema

Every chunk in the vector store carries:

| Field | Purpose |
|---|---|
| `doc_id` | Stable ID: solution ID, ticket ID, or file hash for vendor docs |
| `source_type` | `kb` \| `ticket` \| `attachment` \| `vendor` |
| `source_url` | Deep link back into ServiceDesk Plus, where known |
| `title` | Article subject or ticket subject |
| `topic_path` | e.g. `Clinical Systems/Imaging/IOS Devices` |
| `scanner` | Normalised make and model (TRIOS 4, Primescan, iTero Element, Medit i700 …) |
| `heading` | The heading the chunk sits under, for citation precision |
| `updated` | Last-modified or resolved date, for recency ranking and staleness checks |
| `redacted` | Whether the redaction pass altered this chunk |

`scanner` is the highest-value field. Most IOS support questions are model-specific, and
filtering on it before semantic search prevents Primescan answers surfacing for TRIOS faults.

## Redaction

Runs between stages 0 and 1, before anything is embedded. Targets practice names and
codes, patient identifiers, technician and colleague names, email addresses, phone numbers,
and NHS numbers. Ticket Description fields are the main risk: users paste anything into them.

Redaction is a filter, not a guarantee. The corpus stays treated as confidential regardless.

## Known constraint: ThreatLocker

The IDH laptop blocks execution of installed Python packages from user-writable folders, so
`pip install faiss-cpu && python build_index.py` will fail here. Options, cheapest first:

1. Run the build on a VM or dev server where execution is permitted.
2. Request a ThreatLocker exception for a specific interpreter path via IT.
3. Use an approved managed Python install rather than a user-space venv.

Stages 0 and 1 are file manipulation only and are less likely to be blocked. Stage 2 is the
one that needs a resolved execution path.

## Findings from the first intake (25 articles, 2026-09-07)

Extraction works with no third-party packages: Xpdf `pdftotext` (Program Files, so
ThreatLocker-safe) plus stdlib Python. `pipeline/normalise.py` runs the whole of stage 1.

The pages are A4 portrait two-column print-to-PDF. Body sits left of x=385pt, metadata
sidebar right of it, and `-layout` interleaves them, so each page is extracted twice with
`-marginr`/`-marginl` crops. Verified lossless across all 25 files.

Filenames carry the solution ID (`#403, ...`), which gives stable `doc_id` values and
reconstructable `source_url` deep links. Every article's topic is `Trios`, so the corpus is
single-vendor 3Shape today.

**Three things that change the plan:**

1. **The corpus is 22,059 characters, roughly 5,500 tokens.** That is small enough to pass
   whole in a single prompt. A vector store over 30 chunks retrieves worse than no retrieval
   at all, because the embedding step can only lose information the model would otherwise
   have seen. `01-normalised/ALL-IOS-KB.md` is the whole KB as one file, and it is the
   correct answer until the corpus grows by roughly 20x.

2. **171 images against 22k characters of text.** The corpus is mostly screenshots. Eight
   articles are effectively empty without OCR: sol-385, 393, 394, 400, 402, 406, 410, 432.
   `HOW TO: IOS Toolkit` extracts 70 characters. OCR is a bigger quality win than any
   retrieval work.

3. **Redaction found nothing to redact.** No emails, phone numbers, practice names or
   patient identifiers in any article body. Colleague names appear only in the sidebar, which
   the parser excludes, so nothing written to `01-normalised/` carries PII. That holds for
   the KB only. It will not hold once tickets arrive.

### Corpus gaps

Referenced by article but not saved, all from sol-400 and all laptop-side rather than
scanner-side, so probably filed under a different topic:

- `HOW TO: Enabling high performance mode on Trios laptops`
- `HOW TO: WIFI & WIFI 2`
- `HOW TO: BIOS/Driver updates for laptop`

Two articles defer to Wisdom rather than containing the content: sol-410 (IOS Toolkit) and
sol-407 (tips maintenance guide).

## OCR results (2026-09-07)

Done, on-device, with nothing installed. Two stages:

- `pipeline/extract_images.py` pulls the screenshots straight out of the PDFs as PNGs. No
  rasteriser is needed or available: the screenshots are image XObjects, all `/FlateDecode`
  at 8 bits per component, so they come out at native resolution rather than re-rendered.
  Where the PDF applies a PNG predictor the inflated bytes are already PNG scanlines and
  pass into IDAT unchanged.
- `pipeline/ocr.ps1` runs the Windows built-in engine (`Windows.Media.Ocr`, en-GB) over
  them. System WinRT assemblies only, so ThreatLocker is a non-issue and nothing leaves
  the machine.

**456 image objects, 67 of them actual content.** The rest is portal furniture, mostly
16x16 icons, filtered out at 30,000px area. Of the 67, 55 yielded text; the remaining 12
are product photographs (calibration tips, scanner parts) with nothing to read.

**Corpus went from 22,059 to 43,179 characters, roughly 10,800 tokens.** OCR contributed
21,120 characters, very nearly doubling it. Biggest single gain was sol-386 (NVIDIA control
panel) at +6,918 characters, which went from a stub to the most detailed article in the set.

Quality is good enough for retrieval. Reading order on dense UI panels follows on-screen
layout rather than logical order, and there is light character noise. OCR text is kept in a
separate `## Screenshot text (OCR)` section per article so a reader can weigh it accordingly.

**Only 10 of 25 articles contained screenshots at all.** The earlier assumption that thin
articles were thin because content sat in images was wrong for most of them: sol-400, 410,
432, 397, 404, 407, 415, 431, 399 and 390 have no large images, they are simply short. Four
remain genuinely thin after OCR and are worth a human look: sol-385, sol-400, sol-410,
sol-432.

### OCR changed the PII picture

Redaction found nothing in the PDF text layer but **does** fire on OCR text: real email
addresses in the 3Shape signup and account-verification screenshots, and a colleague name
in the Unite header (`<name> (Elevated Account)`).

Residual risk worth naming: that name was caught only because the person also authors KB
articles, so their name was in the redaction list. A name appearing in a screenshot and
nowhere else would pass through. Anyone reviewing this corpus before it leaves the tenant
should read the `## Screenshot text (OCR)` sections specifically.

## Surfacing screenshots with the answer (2026-09-07)

Knowing a screenshot exists is not enough to show the right one. `pipeline/anchor_images.py`
works out which step each screenshot illustrates, and `pipeline/query.py` retrieves images
as first-class results.

**Anchoring.** An image XObject is drawn into the unit square, so its page position comes
from the CTM at the `Do` operator. Reading the nearest `cm` is not sufficient: browser print
drivers wrap the page in an outer scale (these files draw in CSS pixels at about 0.24) and
nest content in Form XObjects, so the script tracks the full q/Q matrix stack and recurses
into forms. Then, because Xpdf's `-marginb` ignores text in the bottom margin, extracting
with `-marginb <image top>` returns exactly the text above the image and its last line is
the caption. That avoids parsing text coordinates entirely.

All 67 images anchored, 64 of them to a specific step. Spot-checked on sol-403: the
calibration-tip photo lands on "you will need a 3D Calibration object", the button
screenshot on "Press the button 3D Calibrate".

**Retrieval.** `01-normalised/chunks.jsonl` now holds 127 chunks: 60 article chunks and 67
screenshot chunks. A screenshot's retrieval text combines the step it illustrates with the
words visible in it, so "which button do I press" matches the caption and "what does X look
like" matches the OCR. Screenshots from a top-ranked article get a 1.6x lift.

Scoring is BM25, stdlib, no embeddings. At this corpus size that is not a compromise: the
vocabulary is small and highly specific, which is where BM25 is strongest. Tested queries
land correctly, e.g. "the virtual keyboard keeps popping up" returns sol-406 and the exact
screenshot captioned "Ensure Mute on-screen keyboard is selected".

```bash
python pipeline/query.py "how do I calibrate a scanner" --n 2 --shots 3
python pipeline/query.py "sign in to unite" --html result.html --open
```

`--html` writes a result page with the images inline, brand-coloured, that works offline
because it references the PNGs on disk.

**Relevance floor.** `--shots` alone is a fixed count, so a question with one illustration
still returned three, padding the result with screenshots from unrelated articles. `--floor`
(default 0.35) drops any screenshot scoring below that fraction of the best screenshot for
the question. It is relative rather than absolute because BM25 scores are not comparable
between queries: a rare-term question scores far higher than a common-term one, so a fixed
cutoff would either admit noise or suppress good matches depending on wording.

Effect: the keyboard question now returns 1 screenshot instead of 4, dropping the
account-verification image it used to pad with; the calibration question still returns all
4 of its genuinely relevant ones. `--floor 0` disables it.

### Bug found and fixed: the column split was truncating body text

The original body crop at x=385, the visually obvious gutter, was silently cutting words off
the end of every wide body line. sol-403 read "your scanned image will" then jumped to the
next line, losing "display correctly".

The cause is that Xpdf discards a text run unless it falls *entirely* inside the kept
region. Body prose runs out to about 440pt and the sidebar's runs start at about 455pt, so
the two bounds have to be asymmetric: body cropped at 460, sidebar at 450. The loss check
had read zero throughout, because the missing words were not lost, they were being
misfiled into the sidebar extract.

Recovered 459 characters across the corpus. `BODY_X` in `normalise.py` and `BODY_MARGIN_R`
in `anchor_images.py` must stay in step, since anchor text is matched to body lines by exact
string comparison.

### Still worth doing

**Ask the article authors for the source material.** The screenshots came from somewhere. If
the underlying vendor guides exist, they beat OCR of a screenshot of a guide, and they would
also fix the four still-thin articles.

## OCR benchmark (2026-10-16)

`pipeline/ocr_bench.py` runs five OCR models over the 67 screenshots and scores them against
a hand-labelled transcription of every one (built with `pipeline/ocr_review.py`):

| # | Model | Words read correctly | Words found | Overall |
|---|---|---|---|---|
| 1 | **Azure Document Intelligence** | 96% | **98%** | **97%** |
| 2 | Windows OCR (current) | 96% | 69% | 80% |
| 3 | Unlimited-OCR | 90% | 67% | 77% |
| 4 | EasyOCR | 82% | 68% | 74% |
| 5 | Tesseract | 86% | 59% | 70% |

- **Words read correctly:** of the words a model read, how many were right (precision).
- **Words found:** of the words on screen, how many it picked up (recall).
- **Overall:** both combined into one score (F1), which sets the order.

Azure finds nearly every word; the Windows OCR this corpus was built with misses almost a
third, nearly all of it on blurry or small screenshots where it returned nothing. Azure's
figures lean slightly optimistic, since the labels started from its text, but not by enough
to change the order. Details, running it and the data handling are in
`04-ocr-benchmark/README.md`.

Retrieval is unaffected so far: `normalise.py` still reads the Windows output in
`01-ocr/ocr.json`.

### Flagging a bad reading (proposed)

Azure gives every word a confidence from 0 to 1. Per screenshot, or per page of a scanned
document:

- Fewer than 10 words, or none: accept.
- Mean word confidence below **0.70**: flag it for a person to check. Until then, leave its
  OCR text out of the index, but keep the article and the image. A whole file is flagged
  only when OCR is its only text.
- Otherwise accept, with all its words.
- With a flag, report the median text height in pixels. Under 9 px: "text too small,
  capture the screenshot at full size".

On the 67 screenshots this flags 4, all barely legible to a person. 0.80 would also
flag 6 that Azure read mostly correctly. Not yet checked against the hand labels. The
evidence and limits are in `04-ocr-benchmark/README.md`.

## Open questions

- Is a technician API key obtainable? It removes almost all of the manual effort.
- Which embedding model? Local (bge-small, all-MiniLM) keeps everything on-device; Azure
  OpenAI gives better retrieval quality but sends chunks off-device, which changes the
  redaction requirement from prudent to mandatory.
- How many Solutions articles are actually under IOS Devices? Sizes the intake job.
- What is the retrieval surface: a CLI, a small local web UI, or an MCP server that this
  Claude session can query directly?

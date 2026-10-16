# OCR benchmark

Compares OCR engines on the 67 screenshots in `01-ocr/images/` against the Windows OCR
output that the corpus was built with (`01-ocr/ocr.json`). The question it answers: would a
different engine put better screenshot text into retrieval?

Code: `pipeline/ocr_bench.py`. Nothing in `01-ocr/` or `01-normalised/` is touched.

## Running it

```bash
python pipeline/ocr_bench.py run tesseract                  # ~7s
python pipeline/ocr_bench.py run tesseract --psm 11         # sparse-text mode
python pipeline/ocr_bench.py run tesseract --scale 2        # upscale first (Pillow)
python pipeline/ocr_bench.py run easyocr                    # ~3.5 min on CPU (PyTorch)
python pipeline/ocr_bench.py run unlimited-ocr              # GPU only, see below
python pipeline/ocr_bench.py run azure                      # ~30 s, uploads to Azure, see below
python pipeline/ocr_bench.py compare                        # writes report.md
python pipeline/ocr_review.py                               # correct Azure's text -> ground truth
```

Each run writes `<run>/ocr.json` (same shape as `01-ocr/ocr.json`, plus per-segment
confidence and timing) and `<run>/run.json` (engine version, settings, date). `compare`
picks up every run folder, so a new engine only needs a function in `ocr_bench.py` that
returns segments.

Every engine except `azure` runs on-device. None installs on the ThreatLocker-managed Windows
machine, so this runs elsewhere; it is a benchmark, not a pipeline stage. If running
Tesseract by hand in parallel, set `OMP_THREAD_LIMIT=1` or it stalls for minutes per image.
Results are saved after every image (`ocr.partial.json`), so a crashed run keeps its work.

## Azure Document Intelligence

Uses `di-mydentist-ai-uks` (UK South) in mydentist's own tenant, subscription
`myd-aiagents-dev_test`, so the screenshots stay inside the client's Azure. Settings live in
`.env` at the project root, which is never committed:

```
AZURE_DI_ENDPOINT=https://di-mydentist-ai-uks.cognitiveservices.azure.com/
AZURE_DI_TENANT=29d4f8a4-6bc4-4032-b2c1-5ed82e97e377
```

mydentist's policy disables API keys, so the script gets a Microsoft Entra token from the
Azure CLI. Before running: `az login --tenant mydentist.co.uk`, and the account needs the
**Cognitive Services User** role on the resource (Owner or Contributor alone gets 401/403).
`--di-model prebuilt-layout` runs the layout model instead of plain OCR.

## Unlimited-OCR (needs a GPU server)

`baidu/Unlimited-OCR` is a 3B vision-language model derived from DeepSeek-OCR, MIT licence,
6.7 GB of weights. Its code calls `.cuda()` throughout, so it needs an NVIDIA GPU.

Its code was reviewed at commit `8249d20b2822` (2026-10-14): it makes no network calls
(`requests` is imported but unused) and flash-attn is optional. Differences from the model
card snippet:

- `eval_mode=True, save_results=False`. The card's `save_results=True` path runs `eval()` on
  the generated text to draw boxes, which would execute whatever the model wrote. Eval mode
  returns the text instead.
- The model revision is pinned, since `trust_remote_code` runs the repo's Python.
- Its markdown, grounding markers and HTML tables are reduced to plain lines
  (`plain_text()`), so it compares fairly with engines that return plain text. The raw
  output is kept in each row's `raw` field.
- It reports no confidence, so it gets no `conf>=` row in the report.

On a GPU server, with `server` standing for its SSH host:

```bash
# from the laptop: code and screenshots only
ssh server mkdir -p ios-ocr-bench/pipeline ios-ocr-bench/01-ocr
rsync -a pipeline/ocr_bench.py pipeline/requirements-unlimited-ocr.txt server:ios-ocr-bench/pipeline/
rsync -a 01-ocr/images server:ios-ocr-bench/01-ocr/

# on the server
cd ios-ocr-bench
python3.12 -m venv .venv && .venv/bin/pip install -r pipeline/requirements-unlimited-ocr.txt
.venv/bin/python pipeline/ocr_bench.py run unlimited-ocr --mode base \
    --revision 8249d20b2822a7d8a4c22c8d8c9069b5ac36bda3 --gpu-memory 4GiB

# back on the laptop
rsync -a server:ios-ocr-bench/04-ocr-benchmark/unlimited-ocr 04-ocr-benchmark/
python pipeline/ocr_bench.py compare
ssh server rm -rf ios-ocr-bench/01-ocr        # the screenshots are unredacted
```

Only use a company-controlled server: the screenshots show real email addresses and names.

On an 8 GB card (RTX 2080) two things are needed. `--gpu-memory 4GiB` keeps part of the
weights in system RAM, since the bf16 weights alone fill the card. `--mode base` reads each
image once at 1024 px: the default gundam mode tiles into 2 to 32 crops chosen by aspect
ratio, so an 828x711 screenshot becomes 30 tiles and runs out of memory. Expect 20 s to
3.5 min per image there; a 12 GB+ Ampere or newer card should be far faster.

## What the numbers mean

| Column | Meaning |
|---|---|
| word overlap with windows | Share of distinct words both engines found. **Agreement, not accuracy.** |
| conf>=0.5 rows | Same run with low-confidence segments dropped. Windows OCR reports no confidence. |
| CER, WER | Character and word error rate against ground truth. Order-sensitive, so dense panels read in a different order score badly even when every word is right. |
| word precision / recall | Order-free word match against ground truth. Closest to what BM25 retrieval sees. |
| noise chars on blank images | Text an engine invents on images whose ground truth is empty (product photos). |

The ground-truth columns appear once `ground-truth/` has transcriptions. Until then the
report cannot say which engine is right. See `ground-truth/README.md`.

## Findings so far (2026-10-15, no ground truth)

See `report.md` for the current table.

- **Azure Document Intelligence (`prebuilt-read`) is the strongest so far.** 40% more text
  than Windows, readable text on 61 of 67 images against Windows' 47, the highest agreement
  with Windows (82%), and 30 s for the whole set. Its confidence is high throughout, so the
  `conf>=0.5` filter barely changes it.
- On clean screens all engines agree; on the sol-403 calibration screen Windows and
  Tesseract were word-for-word identical.
- On dense, low-resolution UI (sol-386 NVIDIA panel) every engine makes errors, just
  different ones.
- Where Windows returns nothing (blurry portal and scanner-list screens in sol-391, 395,
  403, 441), Tesseract and EasyOCR return gibberish; EasyOCR marks it with low confidence,
  so `conf>=0.5` removes nearly all of it. Azure reads those screens properly ("Installed
  Apps & Subscriptions", "Scanner Management", "TRIOS Patient Monitoring") with a few
  character errors ("Patiems", "Scan semings").
- **Windows' 12 empty images were mostly not product photos.** Azure found real text on 10
  of them: small, low-resolution dialogs (Unite sign-in, Communicate case screens) and the
  label on a scanner. Only sol-403 obj0017 and obj0053 are truly blank. Use those two as
  the blank images in `ground-truth/`.
- Unlimited-OCR (`base` mode) reads small screens cleanly and in logical order, but took
  67 min for the set on the RTX 2080 (median 33 s per image). Two dense NVIDIA panels
  (sol-386 obj0019, obj0021) looped until the 4096-token cap, at 10 min each.

## Accuracy (2026-10-16, all 67 screenshots hand-labelled)

Against the hand labels from `ocr_review.py`: all 67 screenshots, two of them blank, 249
corrections to Azure's reading. Word precision is how many words an engine read are right;
recall is how many words on screen it found. Full table in `report.md`.

| Engine | Word precision | Word recall | Time for 67 |
|---|---|---|---|
| **Azure prebuilt-read** | 96% | **98%** | 30 s |
| Windows (current corpus) | 96% | 69% | – |
| Tesseract x2 | 85% | 69% | 12 s |
| EasyOCR | 82% | 68% | 3.7 min |
| Unlimited-OCR base | 90% | 67% | 67 min (RTX 2080) |
| Tesseract default | 86% | 59% | 7 s |

- **Azure finds nearly every word; Windows misses almost a third**, at the same precision.
  The gap is the blurry and small screenshots, where Windows returned nothing.
- Azure's numbers lean optimistic: the labels start from its text, so a confident Azure
  mistake the reviewer did not notice counts as correct. The flags made that unlikely but not
  impossible. Its lead is far too large for this to change the ranking.
- Unlimited-OCR reads clean screens well but, on this GPU, is no better than Windows overall
  and 130x slower than Azure. Tesseract and EasyOCR trail on precision.
- CER and WER penalise reading order, which differs by engine on multi-column screens;
  precision and recall ignore order and are the fair comparison.
- The automatic ground truth was validated by the hand labels: it lifts Azure's raw text
  from 95.5% to 98.0% precision, and 86% of its corrections and 90% of its deletions agree
  with the reviewer.
- A few icon glyphs (`X`) may remain in the labels despite the convention below. Under 0.2%
  of words, too few to move any score.

## Automatic ground truth (backup)

`python pipeline/ocr_autotruth.py` builds a machine-made ("silver") ground truth for all 67
screenshots in `ground-truth-auto/`, without anyone reading them. It starts from Azure and,
word by word, keeps what another engine also read or a dictionary or the KB articles know,
corrects unsupported low-confidence words to the closest word another engine read or the
KB uses ("Patiems" → "Patients"), drops stray characters and garbage, and adds words three
engines agree Azure missed. `autotruth.md` explains every change and how the engines agree;
it holds screenshot text, so it stays local. Score against it with
`python pipeline/ocr_bench.py compare --gt 04-ocr-benchmark/ground-truth-auto --report
04-ocr-benchmark/report-auto.md`.

It favours Azure, and an error every engine makes is invisible to it, so the hand labels
remain the reference. Checked against the first 16 of them, every change it made that the
labels disagree with was either a leftover Azure misreading in the label (15 of them on the
tiny sol-441 sign-in dialog, confirmed against the image) or a convention question: logo
text read as `3Shape` and window controls (`- 0 X`) dropped as not being text. Two
guessing rules (nearest dictionary word, swapping a known word) were removed for being
wrong more often than right.

Against it, over all 67 screenshots: Windows finds 69% of the words (96% of what it reads is
right), Tesseract x2 69%, EasyOCR 68%, Unlimited-OCR 67%, Azure 99% (inflated, as it is the
starting point). See `report-auto.md`.

## Data handling

The `ocr.json` files contain the same unredacted text as `01-ocr/ocr.json`: email
addresses and a colleague's name from the 3Shape account and Unite screenshots. Treat run
folders and ground truth like the screenshots themselves and keep them off anything
external. `report.md` holds aggregate numbers only.

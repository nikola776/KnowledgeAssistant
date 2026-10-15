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
python pipeline/ocr_bench.py compare                        # writes report.md
```

Each run writes `<run>/ocr.json` (same shape as `01-ocr/ocr.json`, plus per-segment
confidence and timing) and `<run>/run.json` (engine version, settings, date). `compare`
picks up every run folder, so a new engine only needs a function in `ocr_bench.py` that
returns segments.

Engines run on-device. Neither installs on the ThreatLocker-managed Windows machine, so this
runs elsewhere; it is a benchmark, not a pipeline stage. If running Tesseract by hand in
parallel, set `OMP_THREAD_LIMIT=1` or it stalls for minutes per image.

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
.venv/bin/python pipeline/ocr_bench.py run unlimited-ocr \
    --revision 8249d20b2822a7d8a4c22c8d8c9069b5ac36bda3          # add --mode base for the other config

# back on the laptop
rsync -a server:ios-ocr-bench/04-ocr-benchmark/unlimited-ocr 04-ocr-benchmark/
python pipeline/ocr_bench.py compare
ssh server rm -rf ios-ocr-bench/01-ocr        # the screenshots are unredacted
```

Only use a company-controlled server: the screenshots show real email addresses and names.

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

## Findings so far (2026-10-14, no ground truth)

See `report.md` for the current table.

- On clean screens all engines agree; on the sol-403 calibration screen Windows and
  Tesseract were word-for-word identical.
- On dense, low-resolution UI (sol-386 NVIDIA panel) every engine makes errors, just
  different ones.
- Where Windows returns nothing (blurry portal and scanner-list screens in sol-391, 395,
  403, 441), Tesseract and EasyOCR return gibberish. EasyOCR marks it with low confidence,
  so the `conf>=0.5` filter removes nearly all of it.
- None of this measures accuracy yet.

## Data handling

The `ocr.json` files contain the same unredacted text as `01-ocr/ocr.json`: email
addresses and a colleague's name from the 3Shape account and Unite screenshots. Treat run
folders and ground truth like the screenshots themselves and keep them off anything
external. `report.md` holds aggregate numbers only.

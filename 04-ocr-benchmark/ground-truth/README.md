# Ground truth

Hand-checked transcriptions that `pipeline/ocr_bench.py compare` scores every engine
against. Without them the benchmark measures agreement with Windows OCR, not accuracy.

## Format

One file per screenshot: `<doc_id>/<image name without .png>.txt`, for example
`sol-386/obj0018_1243x807.txt` for `01-ocr/images/sol-386/obj0018_1243x807.png`.

- Type what is on screen, one on-screen line per line, in logical reading order.
- Include every readable label, button and menu item, not just the relevant ones.
- Leave text you cannot read out rather than guessing.
- **An empty file means the image has no readable text** (a product photo). These score how
  much noise an engine invents, so include two or three.
- Type from the image. Starting from one engine's output, including `01-ocr/text/`, biases
  the score toward that engine.

## Which images

15 to 20 is enough to separate the engines. Cover the kinds of screen in the corpus rather
than one article:

- dense settings panels: sol-386 (NVIDIA), sol-402
- Unite: sol-406, sol-412, sol-441
- 3Shape portal and account screens: sol-384, sol-395, sol-396
- calibration and scanner management: sol-403
- the blurry screens where engines disagree most: sol-391, sol-395, sol-403
- two or three product photos, as empty files

The account and Unite screenshots show real email addresses and names. Transcribe them as
shown, and keep this folder off anything external.

# Ground truth

Hand-checked transcriptions that `pipeline/ocr_bench.py compare` scores every engine
against. Without them the benchmark measures agreement with Windows OCR, not accuracy.

## The quick way: correct Azure's reading

```bash
python pipeline/ocr_review.py        # opens http://127.0.0.1:8765/
```

The page shows each screenshot next to what Azure read. Every word counts as correct
unless you change it:

- **Amber words** are ones Azure was less than 80% sure of, and by default only their lines
  are shown; the rest collapse and count as correct. Tab jumps between amber words, Enter
  opens one, Enter again saves it and moves on. At 80% the flags caught 12 of 13 Azure
  errors found so far; confident mistakes ("stanes" for "status", 94%) need "Show all".
- **Click a wrong word** and type the right one. Enter saves, Esc cancels, an empty box
  deletes the word.
- **✕** strikes a whole line that isn't on the screen.
- **Missing words** takes text Azure didn't read at all. "What other engines read" at the
  bottom helps you spot it.
- **Done — next** writes this folder's file for that screenshot. Reopen removes it again.

Edits save as you go into `../review.json`, so you can stop and come back. The page holds
all 67 screenshots; Prev, Next and Done skip finished ones, and the dropdown groups them
into To do and Done so a finished one can be reopened. Two images have no text at all
(sol-403 obj0017 and obj0053): mark them done empty.

**The trade-off:** starting from Azure is far quicker than typing, but an Azure mistake you
miss counts as correct, and Azure's line order becomes the reference. Word precision and
recall ignore order and stay fair. CER and WER flatter Azure slightly, so read Azure's row
with that in mind.

## File format

To transcribe by hand instead, or to add screenshots outside the sample:
`<doc_id>/<image name without .png>.txt`, for example `sol-386/obj0018_1243x807.txt` for
`01-ocr/images/sol-386/obj0018_1243x807.png`.

- One on-screen line per line, in logical reading order.
- Every readable label, button and menu item, not just the relevant ones.
- Leave out text you cannot read rather than guessing.
- **An empty file means the image has no readable text.** These score how much noise an
  engine invents.

The account and Unite screenshots show real email addresses and names. Transcribe them as
shown, and keep this folder and `review.json` off anything external. Neither is committed.

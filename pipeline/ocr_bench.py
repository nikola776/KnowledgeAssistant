"""
OCR benchmark: run alternative engines over the extracted screenshots and score
them against the Windows OCR baseline (01-ocr/ocr.json) and, once it exists, a
hand-checked ground truth.

Each run writes 04-ocr-benchmark/<run>/ocr.json in the same shape as
01-ocr/ocr.json, so any run can stand in for the Windows output in normalise.py.
Rows carry two extra fields: `segments`, the engine's own units (Tesseract words,
EasyOCR phrases, Unlimited-OCR lines) with a 0-1 confidence where the engine
reports one and a line number, and `seconds`. Windows.Media.Ocr reports no
confidence, so the baseline has no segments.

Every engine but one runs on-device. None of them can be installed on the
ThreatLocker-managed Windows machine, so this is a benchmark, not a pipeline
stage: run it wherever tesseract (binary on PATH), easyocr (pip, PyTorch) or
Unlimited-OCR (CUDA GPU, pipeline/requirements-unlimited-ocr.txt) is.

The exception is `azure`, which uploads every screenshot to Azure AI Document
Intelligence. The screenshots are unredacted, so point it only at a resource in
mydentist's own tenant. Settings come from the environment or a .env file in
the project root (never committed): AZURE_DI_ENDPOINT, plus AZURE_DI_KEY if
the resource allows keys. mydentist's does not, so by default it signs in with
a Microsoft Entra token from the Azure CLI (`az login --tenant mydentist.co.uk`;
the account needs the Cognitive Services User role on the resource).

Ground truth: 04-ocr-benchmark/ground-truth/<doc_id>/<image basename>.txt, one
on-screen line per line, in logical reading order. An empty file means the image
has no readable text, and scores how much noise an engine invents.

Run:
  python pipeline/ocr_bench.py run tesseract
  python pipeline/ocr_bench.py run tesseract --psm 11 --scale 2
  python pipeline/ocr_bench.py run easyocr
  python pipeline/ocr_bench.py run unlimited-ocr --mode base --gpu-memory 4GiB
  python pipeline/ocr_bench.py run azure --di-model prebuilt-read
  python pipeline/ocr_bench.py compare
"""

import argparse
import html
import io
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGES = os.path.join(ROOT, "01-ocr", "images")
BASELINE = os.path.join(ROOT, "01-ocr", "ocr.json")
BENCH = os.path.join(ROOT, "04-ocr-benchmark")

# Same floor as normalise.py: below this an image's text is a stray word off a
# product photo, and normalise.py drops it.
OCR_MIN_CHARS = 25

# Same tokeniser as query.py. Retrieval is BM25, a bag of words, so word-level
# scores say more about search impact than character error rate does.
TOKEN = re.compile(r"[a-z0-9]+")


def words(text):
    return TOKEN.findall(text.lower())


def list_images():
    """[(doc_id, path)] in the order ocr.ps1 processes them."""
    out = []
    for doc_id in sorted(os.listdir(IMAGES)):
        d = os.path.join(IMAGES, doc_id)
        for name in sorted(os.listdir(d)):
            if name.endswith(".png"):
                out.append((doc_id, os.path.join(d, name)))
    return out


def join_lines(segments, min_conf=0.0):
    """Rebuild text from segments, one line per line number, dropping low-confidence ones."""
    lines = {}
    for s in segments:
        # conf is None for engines that report none; nothing to filter on.
        if (s["conf"] is None or s["conf"] >= min_conf) and s["text"].strip():
            lines.setdefault(s["line"], []).append(s["text"].strip())
    return "\n".join(" ".join(lines[n]) for n in sorted(lines))


def make_row(doc_id, path, segments, seconds):
    """A row shaped like 01-ocr/ocr.json, plus segments and timing."""
    text = join_lines(segments)
    return {
        "doc_id": doc_id,
        "image": os.path.basename(path),
        "lines": len(text.splitlines()),
        "words": len(text.split()),
        "chars": len(text),
        "text": text,
        "seconds": round(seconds, 2),
        "segments": [{"text": s["text"],
                      "conf": None if s["conf"] is None else round(s["conf"], 3),
                      "line": s["line"],
                      # Word outline where the engine gives one; ocr_review.py draws it.
                      **({"box": [round(v, 1) for v in s["box"]]} if s.get("box") else {})}
                     for s in segments],
    }


# --- engines ---------------------------------------------------------------
# Each returns (run, workers): run(path) -> [{"text", "conf", "line"}] in
# reading order, and how many images to process at once.

def tesseract_engine(psm, scale):
    # Tesseract parallelises internally with OpenMP. One process per core on top
    # of that oversubscribes the CPU badly: a 700px screenshot took over seven
    # minutes instead of one second. One thread per process, many processes.
    env = {**os.environ, "OMP_THREAD_LIMIT": "1"}

    def run(path):
        cmd = ["tesseract", path, "-", "--psm", str(psm), "-l", "eng", "tsv"]
        data = None
        if scale != 1:
            cmd[1], data = "stdin", upscale(path, scale)
        res = subprocess.run(cmd, input=data, capture_output=True, env=env)
        if res.returncode:
            raise RuntimeError(res.stderr.decode(errors="replace").strip())
        return parse_tsv(res.stdout.decode("utf-8"))

    return run, os.cpu_count() or 1


def upscale(path, scale):
    """PNG bytes of the image resized by `scale`. UI text is often too small for Tesseract."""
    from PIL import Image  # only needed for --scale

    with Image.open(path) as im:
        im = im.convert("RGB").resize(
            (round(im.width * scale), round(im.height * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()


def parse_tsv(tsv):
    """Tesseract TSV -> word segments. Words sharing block, paragraph and line are one line."""
    segs, line_ids = [], {}
    for row in tsv.splitlines()[1:]:
        f = row.split("\t")
        # level 5 is a word; columns are level page block par line word l t w h conf text
        if len(f) < 12 or f[0] != "5" or not f[11].strip():
            continue
        n = line_ids.setdefault((f[2], f[3], f[4]), len(line_ids))
        segs.append({"text": f[11], "conf": max(0.0, float(f[10])) / 100, "line": n})
    return segs


def easyocr_engine(gpu):
    import easyocr  # PyTorch; only needed for this engine

    reader = easyocr.Reader(["en"], gpu=gpu, verbose=False)

    def run(path):
        dets = reader.readtext(path)  # [(4-point box, text, conf)]
        return group_lines([(box, text, float(conf)) for box, text, conf in dets])

    # One reader in one process: PyTorch already uses every core.
    return run, 1


def group_lines(dets):
    """EasyOCR boxes -> segments with line numbers.

    EasyOCR returns loose boxes with no line structure. A box joins the current
    line when its vertical centre is within half a box height of the line's
    first box; otherwise it starts a new line. Within a line, left to right.
    """
    items = []
    for box, text, conf in dets:
        xs, ys = [float(p[0]) for p in box], [float(p[1]) for p in box]
        items.append(((min(ys) + max(ys)) / 2, max(ys) - min(ys), min(xs), text, conf))
    items.sort()
    lines = []
    for cy, h, x, text, conf in items:
        if lines and abs(cy - lines[-1][0]) <= lines[-1][1] / 2:
            lines[-1][2].append((x, text, conf))
        else:
            lines.append((cy, h, [(x, text, conf)]))
    segs = []
    for n, (_, _, row) in enumerate(lines):
        for _, text, conf in sorted(row, key=lambda r: r[0]):
            segs.append({"text": text, "conf": conf, "line": n})
    return segs


def unlimited_ocr_engine(mode, model_name, revision, gpu_memory=None, max_tokens=4096):
    """baidu/Unlimited-OCR, a 3B vision-language model derived from DeepSeek-OCR.

    Its infer() calls .cuda() directly throughout, so this needs an NVIDIA GPU.
    It reports no confidence: segments are its output lines with conf None.
    Returns (run, workers, version) since the version depends on the loaded model.
    """
    import tempfile

    import torch
    import transformers
    from transformers import AutoModel, AutoTokenizer

    if not torch.cuda.is_available():
        raise SystemExit("unlimited-ocr needs a CUDA GPU: its infer() calls .cuda() directly")

    # trust_remote_code runs Python from the model repo. Pin --revision to the
    # commit whose code was reviewed, so a later push to the repo cannot change it.
    kw = dict(trust_remote_code=True, revision=revision)
    tokenizer = AutoTokenizer.from_pretrained(model_name, **kw)
    if gpu_memory:
        # The bf16 weights alone fill an 8 GB card, leaving nothing for the vision
        # encoder's activations. Cap the GPU share and let accelerate keep the
        # remaining decoder layers in system RAM, streaming them in per forward
        # pass. Slower, but the weights stay exactly as published.
        model = AutoModel.from_pretrained(model_name, use_safetensors=True,
                                          dtype=torch.bfloat16, device_map="auto",
                                          max_memory={0: gpu_memory, "cpu": "64GiB"}, **kw)
        model = model.eval()
    else:
        model = AutoModel.from_pretrained(model_name, use_safetensors=True,
                                          dtype=torch.bfloat16, **kw)
        model = model.eval().cuda()

    # generation_config.json sets max_new_tokens=32768 for long multi-page
    # documents, and it overrides infer()'s max_length. A screenshot needs a few
    # hundred tokens; when the model falls into a loop instead, the cap is what
    # stops one image running for hours (seen on sol-386 at 35+ minutes).
    model.generation_config.max_new_tokens = max_tokens

    # The model card's two single-image configurations. gundam tiles the image
    # into 640px crops plus a 1024px overview, which suits small UI text.
    sizes = {"gundam": dict(base_size=1024, image_size=640, crop_mode=True),
             "base": dict(base_size=1024, image_size=1024, crop_mode=False)}[mode]
    scratch = tempfile.mkdtemp(prefix="unlimited_ocr_")  # infer() creates it even when not saving

    def run(path):
        # eval_mode returns the text. save_results stays off: that path calls
        # eval() on the generated text to draw boxes, executing whatever the model wrote.
        raw = model.infer(tokenizer, prompt="<image>document parsing.", image_file=path,
                          output_path=scratch, max_length=max_tokens, no_repeat_ngram_size=35,
                          ngram_window=128, eval_mode=True, save_results=False, **sizes)
        lines = plain_text(raw or "").splitlines()
        segs = [{"text": line, "conf": None, "line": n} for n, line in enumerate(lines)]
        return segs, {"raw": raw}

    commit = getattr(model.config, "_commit_hash", None) or revision or "main"
    version = (f"{model_name}@{commit[:12]} (transformers {transformers.__version__}, "
               f"{torch.cuda.get_device_name()})")
    return run, 1, version


# Unlimited-OCR answers in markdown, with <|det|>type[bbox]<|/det|> grounding
# markers and HTML for tables. The other engines return plain text, so reduce
# it to the words a reader would see before comparing. A marker sometimes comes
# out unclosed and full of junk ("<|det|>header { } { }"), so one ends at its
# closing tag or the end of the line, whichever is first.
GROUNDING = re.compile(r"<\|(ref|det)\|>.*?(?:<\|/\1\|>|$)", re.M)


def plain_text(md):
    t = GROUNDING.sub("\n", md)
    t = re.sub(r"<br\s*/?>|</(?:tr|p|div|li|h\d)>", "\n", t, flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    out = []
    for line in t.splitlines():
        line = re.sub(r"!\[[^\]]*\]\([^)]*\)|\[Non-Text\]", "", line)  # image/icon placeholders
        if re.fullmatch(r"[\s|:\-]*", line):  # blank, or a markdown table rule
            continue
        line = re.sub(r"^\s*(#+|[-*+])\s+", "", line)  # headings, bullets
        line = " ".join(line.replace("**", "").replace("|", " ").split())
        if line:
            out.append(line)
    return "\n".join(out)


DI_API_VERSION = "2024-11-30"
DI_SCOPE = "https://cognitiveservices.azure.com"


def load_dotenv():
    """KEY=VALUE lines from <root>/.env into os.environ, without overriding what is set."""
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def azure_engine(model):
    """Azure AI Document Intelligence over its REST API, stdlib only.

    prebuilt-read is plain OCR; prebuilt-layout adds reading order and tables.
    Word confidences become segments, grouped into Azure's own lines.
    """
    load_dotenv()
    endpoint = os.environ.get("AZURE_DI_ENDPOINT", "").rstrip("/")
    if not endpoint:
        raise SystemExit("set AZURE_DI_ENDPOINT (environment or .env)")
    if os.environ.get("AZURE_DI_KEY"):
        auth = {"Ocp-Apim-Subscription-Key": os.environ["AZURE_DI_KEY"]}
    else:
        # mydentist's resource has key auth disabled by policy. A token lasts about
        # an hour, far longer than 67 images take.
        cmd = ["az", "account", "get-access-token", "--resource", DI_SCOPE,
               "--query", "accessToken", "-o", "tsv"]
        if os.environ.get("AZURE_DI_TENANT"):
            cmd += ["--tenant", os.environ["AZURE_DI_TENANT"]]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode:
            raise SystemExit(f"no AZURE_DI_KEY and az could not get a token:\n{res.stderr}")
        auth = {"Authorization": f"Bearer {res.stdout.strip()}"}
    url = (f"{endpoint}/documentintelligence/documentModels/{model}:analyze"
           f"?api-version={DI_API_VERSION}")

    def call(req):
        # S0 allows 15 requests a second and F0 far fewer; on 429 wait as told.
        for _ in range(10):
            try:
                return urllib.request.urlopen(req, timeout=60)
            except urllib.error.HTTPError as e:
                if e.code != 429:
                    raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
                time.sleep(float(e.headers.get("Retry-After") or 2))
        raise RuntimeError("still rate limited after 10 retries")

    def run(path):
        with open(path, "rb") as fh:
            body = fh.read()
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={**auth, "Content-Type": "image/png"})
        with call(req) as res:
            op = res.headers["Operation-Location"]
        while True:
            time.sleep(1)
            with call(urllib.request.Request(op, headers=auth)) as res:
                result = json.load(res)
            if result["status"] == "succeeded":
                return parse_analyze(result["analyzeResult"])
            if result["status"] == "failed":
                raise RuntimeError(json.dumps(result.get("error"))[:300])

    return run, 4, f"Azure Document Intelligence {model} (api {DI_API_VERSION})"


def parse_analyze(result):
    """analyzeResult -> word segments, one line number per Azure line."""
    segs, n = [], 0
    for page in result.get("pages", []):
        words_ = page.get("words", [])
        for line in page.get("lines", []):
            spans = [(s["offset"], s["offset"] + s["length"]) for s in line["spans"]]
            for w in words_:
                if any(a <= w["span"]["offset"] < b for a, b in spans):
                    segs.append({"text": w["content"], "conf": w.get("confidence"), "line": n,
                                 "box": w.get("polygon")})  # x1 y1 .. x4 y4, image pixels
            n += 1
    return segs


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def cmd_run(args):
    if args.engine == "tesseract":
        run, workers = tesseract_engine(args.psm, args.scale)
        v = subprocess.run(["tesseract", "--version"], capture_output=True, text=True)
        version = (v.stdout or v.stderr).splitlines()[0]
        settings = {"psm": args.psm, "scale": args.scale}
        default = ("tesseract" + (f"-psm{args.psm}" if args.psm != 3 else "")
                   + (f"-x{args.scale:g}" if args.scale != 1 else ""))
    elif args.engine == "easyocr":
        run, workers = easyocr_engine(args.gpu)
        import easyocr
        version = f"easyocr {easyocr.__version__}"
        settings = {"gpu": args.gpu}
        default = "easyocr"
    elif args.engine == "azure":
        run, workers, version = azure_engine(args.di_model)
        settings = {"model": args.di_model, "api_version": DI_API_VERSION}
        default = "azure-" + args.di_model.replace("prebuilt-", "")
    else:
        run, workers, version = unlimited_ocr_engine(args.mode, args.model, args.revision,
                                                     args.gpu_memory, args.max_tokens)
        settings = {"mode": args.mode, "model": args.model, "revision": args.revision,
                    "gpu_memory": args.gpu_memory, "max_tokens": args.max_tokens}
        default = "unlimited-ocr" + ("-base" if args.mode == "base" else "")
    name = args.name or default
    images = list_images()[:args.limit]
    print(f"{version}: {len(images)} images -> 04-ocr-benchmark/{name}\n")

    def one(item):
        doc_id, path = item
        t = time.time()
        try:
            # An engine may return (segments, extra fields for the row), e.g. raw output.
            result = run(path)
            segs, extra = result if isinstance(result, tuple) else (result, {})
            row = make_row(doc_id, path, segs, time.time() - t)
            row.update(extra)
            return row
        except Exception as e:  # keep going, as ocr.ps1 does; the row records why
            row = make_row(doc_id, path, [], time.time() - t)
            row["error"] = str(e)
            return row

    out = os.path.join(BENCH, name)
    os.makedirs(out, exist_ok=True)
    # Saved after every image, so a crash hours into a slow engine keeps what it
    # finished. compare only reads ocr.json, so it never scores a partial run.
    partial = os.path.join(out, "ocr.partial.json")

    t0, rows = time.time(), []
    with ThreadPoolExecutor(workers) as ex:
        for i, row in enumerate(ex.map(one, images), 1):
            rows.append(row)
            write_json(partial, rows)
            status = f"FAILED: {row['error']}" if "error" in row else \
                f"lines={row['lines']:<4} words={row['words']:<4} {row['seconds']}s"
            print(f"[{i:3}/{len(images)}] {row['doc_id']:<9} {row['image']:<28} {status}",
                  flush=True)
    seconds = round(time.time() - t0, 1)

    write_json(os.path.join(out, "ocr.json"), rows)
    os.remove(partial)
    meta = {"name": name, "engine": version, "settings": settings,
            "date": date.today().isoformat(), "images": len(rows),
            "failed": sum("error" in r for r in rows), "seconds": seconds}
    with open(os.path.join(out, "run.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=1)
    print(f"\n{len(rows)} images in {seconds}s, {meta['failed']} failed. "
          f"wrote 04-ocr-benchmark/{name}/")


# --- scoring ---------------------------------------------------------------

def load_rows(path):
    with open(path, encoding="utf-8-sig") as fh:  # ocr.ps1 writes a BOM
        return {(r["doc_id"], r["image"]): r for r in json.load(fh)}


def load_runs():
    """name -> (rows, meta), Windows baseline first."""
    runs = {"windows": (load_rows(BASELINE), {"engine": "Windows.Media.Ocr (01-ocr/ocr.json)"})}
    for name in sorted(os.listdir(BENCH)):
        path = os.path.join(BENCH, name, "ocr.json")
        if not os.path.exists(path):
            continue
        meta_path = os.path.join(BENCH, name, "run.json")
        meta = {}
        if os.path.exists(meta_path):
            with open(meta_path, encoding="utf-8") as fh:
                meta = json.load(fh)
        runs[name] = (load_rows(path), meta)
    return runs


def load_ground_truth(gt_dir):
    """(doc_id, image) -> reference text, from <gt_dir>/<doc_id>/<image basename>.txt."""
    gt = {}
    if not os.path.isdir(gt_dir):
        return gt
    for doc_id in sorted(os.listdir(gt_dir)):
        d = os.path.join(gt_dir, doc_id)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if name.endswith(".txt"):
                with open(os.path.join(d, name), encoding="utf-8-sig") as fh:
                    gt[(doc_id, name[:-4] + ".png")] = fh.read()
    return gt


def text_of(row, min_conf):
    if min_conf and "segments" in row:
        return join_lines(row["segments"], min_conf)
    return row.get("text") or ""


def edit_distance(a, b):
    """Levenshtein distance between two sequences: strings for CER, word lists for WER."""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def ratio(a, b):
    return a / b if b else None


def score(rows, min_conf, baseline, gt):
    texts = {k: text_of(r, min_conf) for k, r in rows.items()}
    s = {"chars": sum(len(t) for t in texts.values()),
         "with_text": sum(len(t.strip()) >= OCR_MIN_CHARS for t in texts.values())}

    # Agreement, not accuracy: share of distinct words also in the Windows text,
    # over the images where Windows found real text.
    overlaps = []
    for k, ref in baseline.items():
        if len(ref.strip()) < OCR_MIN_CHARS:
            continue
        a, b = set(words(texts.get(k, ""))), set(words(ref))
        overlaps.append(ratio(len(a & b), len(a | b)) or 0.0)
    s["overlap"] = ratio(sum(overlaps), len(overlaps))

    if gt:
        ce = cn = we = wn = hit = n_hyp = n_ref = noise = 0
        for k, ref in gt.items():
            hyp = texts.get(k, "")
            if not ref.strip():
                noise += len(hyp.strip())
                continue
            # Line breaks are layout, not content: compare whitespace-collapsed text.
            r, h = " ".join(ref.split()), " ".join(hyp.split())
            ce += edit_distance(h, r)
            cn += len(r)
            rw, hw = words(ref), words(hyp)
            we += edit_distance(hw, rw)
            wn += len(rw)
            # Order-free word match, which is what BM25 sees. Reading order
            # differences on dense panels inflate CER and WER but not these.
            hit += sum((Counter(rw) & Counter(hw)).values())
            n_hyp += len(hw)
            n_ref += len(rw)
        s.update(cer=ratio(ce, cn), wer=ratio(we, wn), precision=ratio(hit, n_hyp),
                 recall=ratio(hit, n_ref), noise=noise)
    return s


def fmt(v, pct=False):
    if v is None:
        return "-"
    if pct:
        return f"{v * 100:.0f}%"
    return f"{v:,}" if isinstance(v, int) else f"{v:g}"


def cmd_compare(args):
    runs = load_runs()
    gt = load_ground_truth(args.gt)
    images = {(d, os.path.basename(p)) for d, p in list_images()}
    unknown = sorted(k for k in gt if k not in images)
    for k in unknown:
        print(f"warning: ground truth for {k[0]}/{k[1]} matches no screenshot")
    gt = {k: v for k, v in gt.items() if k in images}
    baseline = {k: r.get("text") or "" for k, r in runs["windows"][0].items()}

    variants = []
    for name, (rows, meta) in runs.items():
        variants.append((name, rows, meta, 0.0))
        has_conf = any(s["conf"] is not None
                       for r in rows.values() for s in r.get("segments", []))
        if args.min_conf and has_conf:
            variants.append((f"{name} conf>={args.min_conf:g}", rows, meta, args.min_conf))

    head = ["run", "chars", f"images >={OCR_MIN_CHARS} chars", "word overlap with windows",
            "seconds"]
    if gt:
        head += ["CER", "WER", "word precision", "word recall", "noise chars on blank images"]
    table = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for name, rows, meta, mc in variants:
        s = score(rows, mc, baseline, gt)
        cells = [name, fmt(s["chars"]), fmt(s["with_text"]), fmt(s["overlap"], True),
                 fmt(meta.get("seconds"))]
        if gt:
            cells += [fmt(s["cer"], True), fmt(s["wer"], True), fmt(s["precision"], True),
                      fmt(s["recall"], True), fmt(s["noise"])]
        table.append("| " + " | ".join(cells) + " |")

    blank = sum(not v.strip() for v in gt.values())
    if gt:
        gt_note = (f"Scored against {len(gt)} hand-checked transcriptions "
                   f"({blank} of them blank images).")
    else:
        gt_note = ("No ground truth yet, so nothing here measures accuracy: word overlap "
                   "says how much an engine agrees with Windows, not which is right. "
                   "See ground-truth/README.md.")
    engines = "\n".join(f"- `{name}`: {meta.get('engine', '?')}"
                        + (f", {meta['settings']}" if meta.get("settings") else "")
                        + (f", {meta['date']}" if meta.get("date") else "")
                        for name, (_, meta) in runs.items())
    report = "\n".join([
        f"# OCR benchmark report ({date.today().isoformat()})",
        "",
        "Generated by `python pipeline/ocr_bench.py compare`. Do not edit by hand.",
        "",
        gt_note,
        "",
        *table,
        "",
        "Runs:",
        "",
        engines,
        "",
    ])
    print(report)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(report)
        print(f"wrote {os.path.relpath(args.report, ROOT)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="OCR every screenshot with one engine")
    r.add_argument("engine", choices=["tesseract", "easyocr", "unlimited-ocr", "azure"])
    r.add_argument("--psm", type=int, default=3,
                   help="tesseract page segmentation mode (11 = sparse text)")
    r.add_argument("--scale", type=float, default=1,
                   help="tesseract: upscale images by this factor first (needs Pillow)")
    r.add_argument("--gpu", action="store_true", help="easyocr: use CUDA")
    r.add_argument("--mode", choices=["gundam", "base"], default="gundam",
                   help="unlimited-ocr: gundam tiles into 640px crops, base reads at 1024px")
    r.add_argument("--model", default="baidu/Unlimited-OCR",
                   help="unlimited-ocr: Hugging Face id or a local copy of the repo")
    r.add_argument("--revision",
                   help="unlimited-ocr: model repo commit to load (pin the reviewed one)")
    r.add_argument("--max-tokens", type=int, default=4096,
                   help="unlimited-ocr: cap on generated tokens per image (model default 32768)")
    r.add_argument("--di-model", default="prebuilt-read",
                   help="azure: prebuilt-read (OCR) or prebuilt-layout (adds reading order, tables)")
    r.add_argument("--gpu-memory",
                   help="unlimited-ocr: cap the GPU share of the weights, e.g. 4GiB, and keep "
                        "the rest in system RAM (needs accelerate); for cards under ~12 GB")
    r.add_argument("--name", help="output folder under 04-ocr-benchmark "
                                  "(default: engine plus non-default options)")
    r.add_argument("--limit", type=int,
                   help="only the first N images, for a smoke test; use a separate --name "
                        "so compare does not score a partial run")

    c = sub.add_parser("compare", help="score every run against Windows OCR and the ground truth")
    c.add_argument("--min-conf", type=float, default=0.5,
                   help="also score runs with segments below this confidence dropped "
                        "(0 disables)")
    c.add_argument("--gt", default=os.path.join(BENCH, "ground-truth"),
                   help="ground-truth folder")
    c.add_argument("--report", default=os.path.join(BENCH, "report.md"),
                   help="where to write the markdown report ('' to skip)")

    args = ap.parse_args()
    {"run": cmd_run, "compare": cmd_compare}[args.cmd](args)


if __name__ == "__main__":
    main()

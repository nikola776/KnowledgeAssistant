"""
Stage 1b: pull the embedded screenshots out of the Solutions PDFs as PNGs.

No rasteriser is available on this machine and none is needed: the screenshots are
stored as image XObjects inside the PDF, so they are extracted at native resolution
rather than re-rendered. Zero third-party dependencies.

Every image in these files is /FlateDecode at 8 bits per component. Where the PDF
applies a PNG predictor (/Predictor >= 10) the inflated bytes are already PNG-filtered
scanlines and pass straight into IDAT; otherwise a zero filter byte is prepended per row.

Image XObjects are never held inside object streams, so a raw object scan finds all of
them without needing to parse the xref table.

Run:  python pipeline/extract_images.py
"""

import binascii
import json
import os
import re
import struct
import zlib
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INTAKE = os.path.join(ROOT, "00-intake")
OUT = os.path.join(ROOT, "01-ocr")
IMGDIR = os.path.join(OUT, "images")

# Portal chrome (icons, avatars, spacers) sits far below this. Content screenshots
# in this corpus are 800x400 and up.
MIN_AREA = 30_000

OBJ = re.compile(rb"(\d+)\s+0\s+obj\b(.*?)\bendobj", re.S)


def dict_of(head):
    """Shallow scan of the stream dictionary for the keys we need."""
    def num(key):
        m = re.search(rb"/" + key + rb"\s+(\d+)", head)
        return int(m.group(1)) if m else None

    d = {
        "width": num(b"Width"),
        "height": num(b"Height"),
        "bpc": num(b"BitsPerComponent"),
        "length": num(b"Length"),
    }
    m = re.search(rb"/Predictor\s+(\d+)", head)
    d["predictor"] = int(m.group(1)) if m else 1
    m = re.search(rb"/Colors\s+(\d+)", head)
    d["colors"] = int(m.group(1)) if m else None
    m = re.search(rb"/ColorSpace\s*/(\w+)", head)
    d["cs"] = m.group(1).decode() if m else None
    m = re.search(rb"/Filter\s*(\[[^\]]*\]|/\w+)", head)
    d["filter"] = m.group(1).decode() if m else None
    d["smask_ref"] = [int(x) for x in re.findall(rb"/SMask\s+(\d+)\s+0\s+R", head)]
    d["is_mask"] = b"/ImageMask" in head
    return d


def png_chunk(tag, payload):
    out = struct.pack(">I", len(payload)) + tag + payload
    return out + struct.pack(">I", binascii.crc32(tag + payload) & 0xFFFFFFFF)


def write_png(path, width, height, colortype, scanlines):
    """scanlines: bytes already in PNG format (filter byte per row)."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, colortype, 0, 0, 0)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(png_chunk(b"IHDR", ihdr))
        fh.write(png_chunk(b"IDAT", zlib.compress(scanlines, 6)))
        fh.write(png_chunk(b"IEND", b""))


COLORTYPE = {1: 0, 3: 2, 4: None}  # gray, RGB, CMYK unsupported by PNG


def cmyk_to_rgb(row):
    out = bytearray()
    for i in range(0, len(row), 4):
        c, m, y, k = row[i:i + 4]
        out += bytes((
            max(0, 255 - min(255, c + k)),
            max(0, 255 - min(255, m + k)),
            max(0, 255 - min(255, y + k)),
        ))
    return bytes(out)


def build_scanlines(raw, w, h, d):
    """Return (png_bytes, colortype) or (None, reason)."""
    predictored = d["predictor"] and d["predictor"] >= 10

    if predictored:
        if len(raw) % h:
            return None, f"predictored data {len(raw)} not divisible by h={h}"

        row_len = len(raw) // h
        comps = (row_len - 1) / w
    else:
        row_len = len(raw) / h if h else 0
        comps = row_len / w if w else 0

    if comps != int(comps) or int(comps) not in (1, 3, 4):
        return None, f"unexpected components/pixel={comps:.3f} (w={w} h={h} bytes={len(raw)})"
    comps = int(comps)

    if comps == 4:
        # convert CMYK to RGB, dropping any predictor filter byte per row
        stride = int(row_len) - (1 if predictored else 0)
        out = bytearray()
        for r in range(h):
            start = r * int(row_len) + (1 if predictored else 0)
            out += b"\x00" + cmyk_to_rgb(raw[start:start + stride])
        return bytes(out), 2

    if predictored:
        return raw, COLORTYPE[comps]

    stride = int(row_len)
    out = bytearray()
    for r in range(h):
        out += b"\x00" + raw[r * stride:(r + 1) * stride]
    return bytes(out), COLORTYPE[comps]


def stream_bytes(data, body_start, body, d):
    m = re.search(rb"stream\r?\n", body)
    if not m:
        return None
    start = m.end()
    if d["length"]:
        return body[start:start + d["length"]]
    end = body.find(b"endstream", start)
    return body[start:end] if end > 0 else None


def main():
    os.makedirs(IMGDIR, exist_ok=True)
    manifest = []
    seen_global = defaultdict(list)

    pdfs = sorted(
        os.path.join(dp, f)
        for dp, _, fs in os.walk(INTAKE)
        for f in fs if f.lower().endswith(".pdf")
    )

    for path in pdfs:
        fname = os.path.basename(path)
        m = re.match(r"#(\d+)", fname)
        doc_id = f"sol-{m.group(1)}" if m else os.path.splitext(fname)[0]
        data = open(path, "rb").read()

        objs, smasks = {}, set()
        for om in OBJ.finditer(data):
            objnum = int(om.group(1))
            body = om.group(2)
            head = body.split(b"stream", 1)[0]
            if b"/Image" not in head:
                continue
            d = dict_of(head)
            smasks.update(d["smask_ref"])
            objs[objnum] = (body, d)

        d_out = os.path.join(IMGDIR, doc_id)
        os.makedirs(d_out, exist_ok=True)

        kept, skipped, failed, dupes = 0, 0, [], 0
        seen_local = set()

        for objnum in sorted(objs):
            body, d = objs[objnum]
            w, h = d["width"], d["height"]
            if not w or not h:
                skipped += 1
                continue
            if w * h < MIN_AREA:
                skipped += 1
                continue
            if objnum in smasks or d["is_mask"]:
                skipped += 1
                continue
            if d["filter"] != "/FlateDecode":
                failed.append((objnum, f"filter {d['filter']}"))
                continue

            raw_stream = stream_bytes(data, 0, body, d)
            if not raw_stream:
                failed.append((objnum, "no stream"))
                continue
            try:
                raw = zlib.decompress(raw_stream)
            except zlib.error:
                try:
                    raw = zlib.decompressobj().decompress(raw_stream)
                except zlib.error as e:
                    failed.append((objnum, f"inflate: {e}"))
                    continue

            digest = binascii.crc32(raw)
            if digest in seen_local:
                dupes += 1
                continue
            seen_local.add(digest)

            png, ct = build_scanlines(raw, w, h, d)
            if png is None:
                failed.append((objnum, ct))
                continue

            name = f"obj{objnum:04d}_{w}x{h}.png"
            out_path = os.path.join(d_out, name)
            write_png(out_path, w, h, ct, png)
            kept += 1
            manifest.append({
                "doc_id": doc_id,
                "image": os.path.relpath(out_path, ROOT).replace("\\", "/"),
                "obj": objnum,
                "width": w,
                "height": h,
                "colortype": ct,
                "crc": digest,
            })
            seen_global[digest].append(doc_id)

        note = f"  kept={kept:3d} skipped={skipped:3d} dupes={dupes:3d}"
        if failed:
            note += f" FAILED={len(failed)} {failed[:2]}"
        print(f"{doc_id:9s}{note}")

    # Screenshots repeated across articles (portal furniture that survived the size
    # filter, or genuinely shared steps) are worth knowing about before OCR.
    shared = {k: v for k, v in seen_global.items() if len(set(v)) > 2}
    with open(os.path.join(OUT, "images.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    print(f"\n{len(manifest)} images extracted to {IMGDIR}")
    if shared:
        print(f"{len(shared)} images appear in >2 articles (likely page furniture):")
        for k, v in list(shared.items())[:6]:
            print(f"  crc {k} in {len(set(v))} docs")


if __name__ == "__main__":
    main()

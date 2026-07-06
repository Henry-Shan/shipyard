#!/usr/bin/env python3
"""
notion_ingest.py — turn a Notion page (fetched via the Notion MCP) into a compact,
token-light doc-map for the manager review board, and download its images locally so
the daemon can serve them without any Notion access.

Why a script: the MCP `notion-fetch` result is large (mostly signed S3 image URLs).
The orchestrator fetches the page ONCE, saves the returned enhanced-markdown to a
file, and this script does ALL the heavy lifting deterministically:
  - parse the page into ordered blocks (## Test, ### Feature, findings, text, images)
  - pull out each finding (🔴 bug / 🟠 feature + its X.Y.Z id + text)
  - download every image into <out>/img/ (signed URLs expire in ~1h)
  - write <out>/map.json (compact; local image paths, no signed URLs)
  - print a compact candidate list to stdout for triage (no giant blob in context)

Usage:
  notion_ingest.py --raw raw.md --url <page-url> --title "<title>" --out <dir>
  notion_ingest.py --raw raw.md --url <page-url> --no-images   # skip downloads
"""
import argparse, json, os, re, sys, hashlib, urllib.request

# a finding line: optional severity emoji, a kind word, an X.Y[.Z[.W]] id, then text
SEV = {"🔴": "bug", "🟠": "feature", "🟡": "minor", "🟢": "done", "⚪": "note", "✅": "done"}
FIND = re.compile(r'^\s*([🔴🟠🟡🟢⚪✅])?\s*(bug|feature|issue|test|problem|note)\b[^\dA-Za-z]*'
                  r'(\d+(?:\.\d+){1,3})\s*[-:–—]?\s*(.*)$', re.I)
IMG = re.compile(r'!\[[^\]]*\]\((https?://[^)]+)\)')
# a screenshot-evidence line: "image 4.1.1 — <what it shows>". The doc export
# often carries these as plain text (no embed), so we attach them to finding
# 4.1.1 as evidence — rendered together with that finding on the board.
IMGLINE = re.compile(r'^image\s+(\d+(?:\.\d+){1,3})\s*[-–—:]?\s*(.*)$', re.I)


def download(url, outdir):
    os.makedirs(outdir, exist_ok=True)
    name = hashlib.sha1(url.split("?")[0].encode()).hexdigest()[:16] + ".png"
    dest = os.path.join(outdir, name)
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        return name
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=45) as r, open(dest, "wb") as f:
            f.write(r.read())
        return name if os.path.getsize(dest) > 0 else None
    except Exception as e:
        sys.stderr.write(f"  image download failed: {e}\n")
        return None


CAPLINE = re.compile(r'^\\?\^\s*(.*)$')   # a "\^ what the screenshot shows" caption line


def parse(raw, url, title, outdir, do_images=True):
    blocks, findings = [], []
    test = feature = ""
    by_id = {}          # finding id -> finding block, so evidence attaches to its finding
    last_find = None    # most recent finding, for un-labelled image embeds
    img_id = None       # finding id the current run of images belongs to (from an "image N.N.N" label)
    pending = []        # evidence dicts awaiting the caption line that follows them
    # True right after a finding line, until a blank line or another block type
    # ends it. A plain text line while this is true is the SAME sentence
    # wrapped onto the next physical line (common in QA docs), not new
    # standalone context — merge it into the finding instead of orphaning it.
    just_after_find = False
    for line in raw.splitlines():
        s = line.strip()
        if not s or s == "<empty-block/>":
            just_after_find = False
            continue
        # A "\^ ..." line captions the image(s) that just preceded it.
        cm = CAPLINE.match(s)
        if cm and pending:
            cap = cm.group(1).strip()
            for ev in pending:
                ev["caption"] = cap
            pending = []
            just_after_find = False
            continue
        m = IMG.search(s)
        if m:
            # A real screenshot embed: download it (signed URLs expire in ~1h)
            # and attach to its finding so it renders — and is fed to agents —
            # together with that finding.
            local = download(m.group(1), os.path.join(outdir, "img")) if do_images else None
            target = by_id.get(img_id) or last_find
            ev = {"caption": "", "src": ("img/" + local) if local else None,
                  "ref": img_id or (target or {}).get("id")}
            if target is not None:
                target.setdefault("evidence", []).append(ev)
            else:                                   # image before any finding — keep standalone
                ev = {"t": "image", "src": ev["src"], "caption": ""}
                blocks.append(ev)
            pending.append(ev)
            just_after_find = False
            continue
        if s.startswith("## "):
            test = s[3:].strip(); feature = ""; last_find = None; img_id = None; pending = []
            blocks.append({"t": "test", "text": test}); just_after_find = False; continue
        if s.startswith("### "):
            feature = s[4:].strip(); last_find = None; img_id = None; pending = []
            blocks.append({"t": "feature", "text": feature}); just_after_find = False; continue
        if s.startswith("#"):
            last_find = None; img_id = None
            blocks.append({"t": "feature", "text": s.lstrip("# ").strip()}); just_after_find = False; continue
        fm = FIND.match(s)
        if fm:
            emoji, kind, fid, txt = fm.groups()
            sev = SEV.get(emoji or "", (kind or "").lower())
            f = {"t": "finding", "id": fid, "kind": (kind or "").lower(),
                 "sev": sev, "text": txt.strip(), "test": test, "feature": feature}
            blocks.append(f); by_id[fid] = f; last_find = f; img_id = None; pending = []
            findings.append({"id": fid, "kind": f["kind"], "sev": sev,
                             "text": txt.strip()[:200], "test": test, "feature": feature})
            just_after_find = True
            continue
        # "image N.N.N [— text]": a label saying the next embeds belong to N.N.N.
        im = IMGLINE.match(s)
        if im:
            fid, cap = im.group(1), im.group(2).strip()
            img_id = fid
            pending = []
            if cap:   # older text-only export carried the description inline
                target = by_id.get(fid) or last_find
                ev = {"caption": cap, "src": None, "ref": fid}
                (target.setdefault("evidence", []).append(ev) if target
                 else blocks.append({"t": "image", "src": None, "caption": s}))
            just_after_find = False
            continue
        if just_after_find and last_find is not None:
            last_find["text"] = (last_find["text"] + " " + s).strip()
            if findings and findings[-1]["id"] == last_find["id"]:
                findings[-1]["text"] = last_find["text"][:200]
            continue
        blocks.append({"t": "text", "text": s})
    return {"source": "notion", "url": url, "title": title, "blocks": blocks, "findings": findings}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True, help="file with the notion-fetch enhanced-markdown")
    ap.add_argument("--url", required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("--out", default=None, help="output dir (default: .notion/<pageid>)")
    ap.add_argument("--no-images", action="store_true")
    a = ap.parse_args()
    pageid = re.sub(r'[^0-9a-f]', '', (a.url.split("/")[-1] or "page").split("?")[0].lower())[-32:] or "page"
    here = os.path.dirname(os.path.abspath(__file__))
    outdir = a.out or os.path.join(here, ".notion", pageid)
    os.makedirs(outdir, exist_ok=True)
    raw = open(a.raw, errors="replace").read()
    m = parse(raw, a.url, a.title, outdir, do_images=not a.no_images)
    json.dump(m, open(os.path.join(outdir, "map.json"), "w"), indent=1)
    # token-light triage view on stdout
    print(f"NOTION_MAP {os.path.join(outdir, 'map.json')}")
    n_img = (sum(1 for b in m['blocks'] if b['t'] == 'image' and b.get('src'))
             + sum(1 for b in m['blocks'] for ev in b.get('evidence', []) if ev.get('src')))
    n_evi = sum(len(b.get('evidence', [])) for b in m['blocks'])
    print(f"  title: {a.title or m['title']}  ·  {len(m['findings'])} findings  ·  "
          f"{n_img} images  ·  {n_evi} evidence notes")
    for f in m["findings"]:
        print(f"  [{f['sev']:7}] {f['id']:8} {f['text'][:90]}")


if __name__ == "__main__":
    main()

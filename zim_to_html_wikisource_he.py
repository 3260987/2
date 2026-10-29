#!/usr/bin/env python3
"""
Kiwix ZIM (Hebrew Wikisource / ויקיטקסט, no images)  ->  plain offline HTML files (NO JavaScript needed).

This is the same converter used for Hebrew Wikipedia, adapted for Wikisource.
The logic is fully generic (it works off whatever Hebrew article titles are
in the ZIM), so the only real changes versus the Wikipedia version are the
default --title / --base-name values below. You can still override them
from the command line if you want.

Output layout
  * one folder per first letter (22 Hebrew letters; "אחר" is created only if there are articles that start
    with a digit / English / symbol), and inside it all the HTML files of that letter
  * every HTML file is ~part-mb MB; it is named after the first and the last article it contains
  * every file starts with a sticky top bar: jump to the start of the title list, or to 25% / 50% / 75% / 100%
    of the titles (the titles list, not the articles themselves), then the alphabetical list of titles, then the articles
  * links between articles of the same file work; a link to an article in another file is shown as plain text
  * every folder has 000_אינדקס.html: every article name (and redirect name) of that letter + the number of the file
  * everything is packed into ONE zip; only if it would be bigger than ~1.99GB it is split into several zips

Usage example:
    python3 zim_to_html_wikisource_he.py \
        --zim wikisource_he_all_nopic_2026-XX.zim \
        --title "ויקיטקסט העברית" \
        --base-name Wikisource-he \
        --zip
"""
import argparse
import collections
import html as htmllib
import itertools
import multiprocessing as mp
import os
import posixpath
import re
import sqlite3
import sys
import time
import zipfile
from urllib.parse import unquote

import lxml.html
from lxml import etree

# ----------------------------------------------------------------------------------------------
# cleaning rules
# ----------------------------------------------------------------------------------------------
DROP_TAGS = {
    "script", "style", "link", "meta", "img", "picture", "source", "video", "audio", "track", "svg",
    "canvas", "noscript", "iframe", "object", "embed", "template", "form", "button", "input", "select",
    "textarea", "header", "nav", "footer", "aside", "h1", "head", "title", "base", "map", "area",
}
UNWRAP_TAGS = {"span", "section", "article", "main", "bdi", "font", "figure", "center", "wbr", "bdo"}
DROP_CLASSES_BASE = {
    "navbox", "navbox-inner", "navbox-subgroup", "vertical-navbox", "mw-editsection", "mw-editsection-like",
    "noprint", "catlinks", "printfooter", "sistersitebox", "mw-empty-elt", "ambox", "metadata",
    "mw-authority-control", "authority-control", "navigation-not-searchable", "toc", "mw-toc", "vector-toc",
    "portalbox", "sidebar", "navbar", "mw-jump-link", "mw-hidden-catlinks", "hlist-navbox", "mw-indicators",
    "mw-cite-backlink", "mw-redirectedfrom", "shortdescription", "nomobile", "mw-collapsible-toggle",
    # Wikisource-specific chrome worth stripping too (proofreading/quality UI, headers that don't apply offline)
    "ws-noexport", "prp-pagequality-0", "prp-pagequality-1", "prp-pagequality-2",
    "prp-pagequality-3", "prp-pagequality-4", "pagequality",
}
DROP_CLASSES_REFS = {"reference", "reflist", "references", "mw-references-wrap", "refbegin", "reference-text"}
DISPLAY_NONE = re.compile(r"display\s*:\s*none", re.I)
SCHEME = re.compile(r"^(?:https?|ftp|mailto|tel|javascript|data|geo|irc|sms|magnet|tg|news|ssh):", re.I)
CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
WS = re.compile(r"[ \t\r\n]{2,}")
EMPTY = re.compile(r"<(p|div|li|ul|ol|dl|dd|dt|small|b|i|sup|sub)>\s*</\1>")
BAD_FN = re.compile(r'[<>:"/\\|?*\x00-\x1f\u200e\u200f\u202a-\u202e]')
LXML_PARSER = lxml.html.HTMLParser(encoding="utf-8", remove_comments=True, remove_pis=True)

# globals filled before the worker pool is forked
PID = {}
ARC = None
ZIM_PATH = None
DROP_CLASSES = DROP_CLASSES_BASE


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def resolve(href, base_dir):
    """href of a link inside an article -> article number (or None)."""
    if not href or href[0] == "#" or href.startswith("//") or SCHEME.match(href):
        return None
    href = href.split("#", 1)[0].split("?", 1)[0]
    if not href:
        return None
    raw = unquote(href)
    r = _lookup(posixpath.normpath(posixpath.join(base_dir, raw)))
    if r is None and base_dir:          # article path contains "/" and the link is relative to the root
        r = _lookup(posixpath.normpath(raw))
    return r


def _lookup(p):
    while p.startswith("../"):
        p = p[3:]
    r = PID.get(p)
    if r is None:
        r = PID.get(p.replace(" ", "_"))
    return r


def clean_article(data, path):
    doc = lxml.html.document_fromstring(data, parser=LXML_PARSER)
    body = doc.find("body")
    if body is None:
        body = doc
    base_dir = posixpath.dirname(path)

    drop = []
    for el in body.iter():
        t = el.tag
        if not isinstance(t, str):
            drop.append(el)
            continue
        if t in DROP_TAGS:
            if el is not body:
                drop.append(el)
            continue
        a = el.attrib
        if a:
            c = a.get("class")
            if c and not DROP_CLASSES.isdisjoint(c.split()):
                drop.append(el)
                continue
            st = a.get("style")
            if st and DISPLAY_NONE.search(st):
                drop.append(el)
                continue
            if a.get("hidden") is not None:
                drop.append(el)
                continue
    for el in drop:
        if el.getparent() is not None:
            el.drop_tree()

    unwrap, anchors = [], []
    for el in body.iter():
        t = el.tag
        if not isinstance(t, str):
            continue
        if t == "a":
            anchors.append(el)
            continue
        if t in UNWRAP_TAGS:
            unwrap.append(el)
        a = el.attrib
        if a:
            keep = {}
            if t in ("td", "th"):
                for k in ("colspan", "rowspan"):
                    v = a.get(k)
                    if v and v.isdigit():
                        keep[k] = v
            a.clear()
            for k, v in keep.items():
                a[k] = v
    for el in anchors:
        tid = resolve(el.get("href"), base_dir)
        el.attrib.clear()
        if tid is None:
            unwrap.append(el)
        else:
            el.set("data-i", str(tid))
    for el in unwrap:
        if el.getparent() is not None:
            el.drop_tag()

    s = etree.tostring(body, encoding="unicode", method="html")
    if s.startswith("<body>"):
        s = s[6:]
        if s.endswith("</body>"):
            s = s[:-7]
    s = CTRL.sub("", s)
    s = WS.sub(" ", s)
    for _ in range(3):
        s2 = EMPTY.sub("", s)
        if s2 == s:
            break
        s = s2
    s = s.strip()
    return s or "<p>(ערך ריק)</p>"


def worker_init():
    global ARC
    from libzim.reader import Archive
    ARC = Archive(ZIM_PATH)


def work(chunk):
    out = []
    for aid, path in chunk:
        try:
            item = ARC.get_entry_by_path(path).get_item()
            out.append((aid, clean_article(bytes(item.content), path)))
        except Exception as ex:  # keep going, never lose an id
            out.append((aid, "<p>שגיאה בעיבוד הערך: %s</p>" % str(ex).replace("<", "&lt;")))
    return out


# ----------------------------------------------------------------------------------------------
# pass 1: titles + redirects
# ----------------------------------------------------------------------------------------------
def pass1(zim, limit):
    from libzim.reader import Archive
    arc = Archive(zim)
    total = arc.entry_count
    log("ZIM entries:", total, "| new namespace scheme:", arc.has_new_namespace_scheme)
    # libzim >=4.0.0 renamed the public accessor to get_entry_by_id() and removed
    # the old _get_entry_by_id(); older releases only have the underscored one.
    get_entry_by_id = getattr(arc, "get_entry_by_id", None) or arc._get_entry_by_id
    arts, reds = {}, {}
    errors = 0
    first_error = None
    for i in range(total):
        try:
            e = get_entry_by_id(i)
            if e.is_redirect:
                reds[e.path] = (e.title or e.path, e.get_item().path)
            else:
                it = e.get_item()
                if it.mimetype.startswith("text/html"):
                    arts[e.path] = e.title or e.path.replace("_", " ")
        except Exception as ex:
            errors += 1
            if first_error is None:
                first_error = "%s: %s" % (type(ex).__name__, ex)
            continue
        if i and i % 200000 == 0:
            log("  scanned", i, "of", total, "| articles", len(arts), "redirects", len(reds))
    if errors:
        log("  WARNING: %d/%d entries raised an exception while scanning (first: %s)" % (errors, total, first_error))
    if limit:
        arts = dict(itertools.islice(arts.items(), limit))
    log("articles:", len(arts), "| redirects:", len(reds))
    return arts, reds


# ----------------------------------------------------------------------------------------------
# folders (one per first letter) and sorting
# ----------------------------------------------------------------------------------------------
LINK_RE = re.compile(r'<a data-i="(\d+)">(.*?)</a>', re.S)
FINAL_LETTERS = str.maketrans("ךםןףץ", "כמנפצ")
NIKUD_RE = re.compile(r"[\u0591-\u05C7]")
HEB_LETTERS = "אבגדהוזחטיכלמנסעפצקרשת"          # 22 letters (final forms are merged into the regular ones)
FOLDER_NAMES = list(HEB_LETTERS) + ["אחר"]
FOLDER_ASCII = ["alef", "bet", "gimel", "dalet", "he", "vav", "zayin", "het", "tet", "yod", "kaf", "lamed", "mem",
                "nun", "samekh", "ayin", "pe", "tsadi", "qof", "resh", "shin", "tav", "other"]
INDEX_NAME = "000_אינדקס"


def norm_key(t):
    """sort key: no nikud, final letters merged, lower case"""
    return NIKUD_RE.sub("", t).translate(FINAL_LETTERS).lower()


def folder_of(t):
    """0..21 = Hebrew first letter, 22 = everything else (digits, English, symbols)"""
    for ch in norm_key(t):
        if ch.isalnum():
            return HEB_LETTERS.index(ch) if ch in HEB_LETTERS else 22
    return 22


STATIC_CSS = (
    ":root{color-scheme:light dark}"
    "body{font-family:system-ui,Arial,sans-serif;line-height:1.7;margin:0 8px 40px;overflow-wrap:anywhere}"
    "#bar{position:sticky;top:0;z-index:5;background:#202124;padding:6px 2px;white-space:nowrap;overflow-x:auto}"
    "#bar a{color:#8ab4f8;text-decoration:none;display:inline-block;padding:5px 13px;margin-left:5px;border:1px solid #5f6368;border-radius:6px}"
    "h1{font-size:1.45em;border-bottom:1px solid #999;margin:.8em 0 .4em}"
    "h2{font-size:1.25em;border-bottom:1px solid #999;margin-top:1.3em}h3{font-size:1.1em}"
    "a{color:#0645ad}table{border-collapse:collapse;display:block;overflow-x:auto}td,th{border:1px solid #999;padding:3px 6px}"
    ".bar a{display:inline-block;padding:4px 9px;margin:2px;border:1px solid #999;border-radius:5px;text-decoration:none}"
    ".t div{padding:3px 0}"
    ".art{content-visibility:auto;contain-intrinsic-size:auto 1500px;border-bottom:2px solid #888;padding-bottom:6px}"
    ".pm{color:#fff;background:#1a73e8;border-radius:4px;padding:0 6px;font-size:.8em;margin-left:6px}"
    "@media (prefers-color-scheme:dark){body{background:#121212;color:#e4e4e4}a{color:#8ab4f8}}"
)

BAR_HTML = ('<div id="bar"><a href="#top">&#8593;</a><a href="#toc">התחלה</a><a href="#p25">25%</a>'
            '<a href="#p50">50%</a><a href="#p75">75%</a><a href="#p100">100%</a></div>')


def fn_piece(t, maxlen=35):
    t = BAD_FN.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip(" .")
    if len(t) > maxlen:
        t = t[:maxlen].rstrip(" .")
    return t or "_"


def static_head(title):
    return ('<!doctype html>\n<html lang="he" dir="rtl"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>%s</title><style>%s</style></head><body>\n%s' % (htmllib.escape(title), STATIC_CSS, BAR_HTML))


def gkey(k, plen):
    k = re.sub(r"^[\W_]+", "", k)
    return k[:plen] or "?"


def letter_groups(keys):
    """keys: group keys in sorted order -> list of (key, first_position)."""
    out, last = [], None
    for pos, k in enumerate(keys):
        c = k.upper()
        if c != last:
            out.append((c, pos))
            last = c
    return out


def emit_list(out, entries, plen):
    """entries: list of (sortkey, html). Writes the group bar and the list of titles, with the 25/50/75/100% markers."""
    esc = htmllib.escape
    n = len(entries)
    groups = letter_groups([gkey(e[0], plen) for e in entries])
    out.write('<div class="bar">%s</div>' % "".join(
        '<a href="#g%d">%s</a>' % (gi, esc(c)) for gi, (c, _) in enumerate(groups)))
    pp = {}
    for pct in (25, 50, 75, 100):
        pos = min(n - 1, max(0, -(-n * pct // 100) - 1))
        pp.setdefault(pos, []).append(pct)
    out.write('<div class="t" id="toc">')
    gi = 0
    for pos, e in enumerate(entries):
        if gi < len(groups) and groups[gi][1] == pos:
            out.write('<h2 id="g%d">%s</h2>' % (gi, esc(groups[gi][0])))
            gi += 1
        marks = "".join('<span id="p%d"></span><b class="pm">%d%%</b>' % (p, p) for p in pp.get(pos, []))
        out.write("<div>%s%s</div>" % (marks, e[1]))
    out.write("</div>")


def build_static(args, db, titles, red_lines, N):
    import bisect
    from urllib.parse import quote
    esc = htmllib.escape
    part_limit = int(args.part_mb * 1024 * 1024)
    hard_limit = int(args.hard_mb * 1024 * 1024)
    fol = [folder_of(t) for t in titles]          # ids are sorted by (folder, key): every folder is one id range

    # ---- boundaries of the files: never cross a folder, ~part_mb each
    parts, cur = [], None
    for aid, ln in db.execute("SELECT id, length(CAST(h AS BLOB)) FROM a ORDER BY id"):
        cost = ln + len(titles[aid].encode("utf-8")) * 2 + 320
        if cur is None or cur["f"] != fol[aid] or (cur["size"] + cost > part_limit and cur["hi"] > cur["lo"]):
            cur = {"f": fol[aid], "lo": aid, "hi": aid, "size": 20000}
            parts.append(cur)
        cur["hi"] = aid + 1
        cur["size"] += cost
    per_folder = {}
    for p in parts:
        per_folder.setdefault(p["f"], []).append(p)
    width = max(3, max(len(str(len(v))) for v in per_folder.values()))
    for f, plist in per_folder.items():
        for n, p in enumerate(plist, 1):
            p["n"] = n
            p["dir"] = "%02d_%s" % (f + 1, FOLDER_NAMES[f])
            p["name"] = "%0*d_%s - %s.html" % (width, n, fn_piece(titles[p["lo"]]), fn_piece(titles[p["hi"] - 1]))
    los = [p["lo"] for p in parts]
    log("%d articles -> %d folder(s), %d file(s)" % (N, len(per_folder), len(parts)))

    def pidx(aid):
        return bisect.bisect_right(los, aid) - 1

    items = []      # (path, arcname)

    # ---- the index of every folder: name -> file number
    by_f = {}
    for j in range(N):
        p = parts[pidx(j)]
        by_f.setdefault(p["f"], []).append((norm_key(titles[j]), "%s <b>[%0*d]</b>" % (esc(titles[j]), width, p["n"])))
    for lname, name, tid in red_lines:
        f2 = folder_of(name)
        tp = parts[pidx(tid)]
        ref = ("%0*d" % (width, tp["n"])) if tp["f"] == f2 else ("%s/%0*d" % (FOLDER_NAMES[tp["f"]], width, tp["n"]))
        by_f.setdefault(f2, []).append((norm_key(name), "%s &#8592; %s <b>[%s]</b>" % (esc(name), esc(titles[tid]), ref)))
    del red_lines
    chunk_limit = max(part_limit - 150000, part_limit // 2)
    for f in sorted(by_f):
        entries = by_f[f]
        entries.sort(key=lambda e: e[0])
        chunks, cur_c, size = [], [], 0
        for e in entries:
            cur_c.append(e)
            size += len(e[1].encode("utf-8")) + 20
            if size >= chunk_limit:
                chunks.append(cur_c)
                cur_c, size = [], 0
        if cur_c:
            chunks.append(cur_c)
        inames = [INDEX_NAME + ".html"] if len(chunks) == 1 else ["%s %d.html" % (INDEX_NAME, i + 1) for i in range(len(chunks))]
        table = "".join('<div>%0*d &#8594; %s</div>' % (width, p["n"], esc(p["name"][:-5])) for p in per_folder.get(f, []))
        d = "%02d_%s" % (f + 1, FOLDER_NAMES[f])
        os.makedirs(os.path.join(args.outdir, d), exist_ok=True)
        for ci, chunk in enumerate(chunks):
            path = os.path.join(args.outdir, d, inames[ci])
            with open(path, "w", encoding="utf-8") as out:
                out.write(static_head("%s - אינדקס %s" % (args.title, FOLDER_NAMES[f])))
                out.write('<h1 id="top">%s - אינדקס אות %s%s</h1>' % (esc(args.title), esc(FOLDER_NAMES[f]),
                          (" (חלק %d מתוך %d)" % (ci + 1, len(chunks))) if len(chunks) > 1 else ""))
                out.write('<p>חפש כאן שם ערך (חיפוש בדף של האפליקציה). ליד כל שם מופיע <b>מספר הקובץ</b> '
                          '(ואם הוא בתיקיית אות אחרת - האות והמספר). שם עם חץ (&#8592;) הוא שם חלופי שמפנה לערך.</p>')
                out.write('<details><summary>רשימת הקבצים בתיקייה</summary><div class="t">%s</div></details>' % table)
                emit_list(out, chunk, 2 if f < 22 else 1)
                out.write("</body></html>\n")
            items.append((path, d + "/" + inames[ci]))
            log("wrote %s/%s  %.1f MB" % (d, inames[ci], os.path.getsize(path) / 1e6))
    del by_f

    # ---- the content files
    for i, p in enumerate(parts):
        f, lo, hi = p["f"], p["lo"], p["hi"]
        plist = per_folder[f]
        path = os.path.join(args.outdir, p["dir"], p["name"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        prev_p = parts[i - 1] if i > 0 and parts[i - 1]["f"] == f else None
        next_p = parts[i + 1] if i + 1 < len(parts) and parts[i + 1]["f"] == f else None

        def sub_link(m, lo=lo, hi=hi):
            t = int(m.group(1))
            return '<a href="#a%d">%s</a>' % (t, m.group(2)) if lo <= t < hi else m.group(2)

        entries = [(norm_key(titles[j]), '<a href="#a%d">%s</a>' % (j, esc(titles[j]))) for j in range(lo, hi)]
        with open(path, "w", encoding="utf-8") as out:
            out.write(static_head("%s - %s %d: %s - %s" % (args.title, FOLDER_NAMES[f], p["n"], titles[lo], titles[hi - 1])))
            out.write('<h1 id="top">%s - אות %s - קובץ %d מתוך %d</h1>' % (esc(args.title), esc(FOLDER_NAMES[f]), p["n"], len(plist)))
            out.write('<p>ערכים: <b>%s</b> עד <b>%s</b> (%d ערכים)<br>%s%s'
                      'אינדקס כללי של האות: <b>%s</b> בתיקייה זו.<br>'
                      'קישור לערך שנמצא בקובץ אחר אינו פעיל - חפש את שמו באינדקס.</p>' % (
                          esc(titles[lo]), esc(titles[hi - 1]), hi - lo,
                          ("קובץ קודם: %s<br>" % esc(prev_p["name"][:-5])) if prev_p else "",
                          ("קובץ הבא: %s<br>" % esc(next_p["name"][:-5])) if next_p else "",
                          INDEX_NAME))
            emit_list(out, entries, 2 if f < 22 else 1)
            out.write("<hr>\n")
            for aid, h in db.execute("SELECT id, h FROM a WHERE id>=? AND id<? ORDER BY id", (lo, hi)):
                out.write('<section class="art" id="a%d"><h1>%s</h1>%s<p><a href="#toc">&#8593; לרשימת הכותרות</a></p></section>\n'
                          % (aid, esc(titles[aid]), LINK_RE.sub(sub_link, h)))
            out.write("</body></html>\n")
        size = os.path.getsize(path)
        items.append((path, p["dir"] + "/" + p["name"]))
        log("wrote %s/%s  %.1f MB  (articles %d-%d)%s" % (p["dir"], p["name"], size / 1e6, lo, hi - 1,
            "  WARNING: bigger than %.1f MB" % args.hard_mb if size > hard_limit else ""))
    return items


def zip_outputs(args, items, t0):
    """items: (path, arcname). One ZIP with everything; a new ZIP is started only near the size limit."""
    if args.zip:
        limit = args.zip_limit_mb * 1024 * 1024
        z, zc, last_c, zi = None, 0, 0, 0
        zips, dirs = [], set()
        for path, arc in items:
            if z is None or zc + last_c * 1.2 > limit:
                if z is not None:
                    z.close()
                zi += 1
                zpath = os.path.join(args.outdir, "%s-%02d.zip" % (args.base_name, zi))
                z = zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True)
                zips.append(zpath)
                zc = 0
            z.write(path, arcname=arc)
            last_c = z.infolist()[-1].compress_size
            zc += last_c
            os.remove(path)
            dirs.add(os.path.dirname(path))
        if z is not None:
            z.close()
        for d in dirs:
            try:
                os.rmdir(d)
            except OSError:
                pass
        if len(zips) == 1:                      # a single ZIP gets the plain name
            final = os.path.join(args.outdir, "%s.zip" % args.base_name)
            os.replace(zips[0], final)
            zips = [final]
        for zpath in zips:
            zmb = os.path.getsize(zpath) / 1048576
            log("zipped %s  %.0f MB" % (os.path.basename(zpath), zmb))
            if zmb > 1990:
                log("ERROR: ZIP is bigger than 1990 MB (GitHub Release limit is 2 GiB). Run again with a smaller --zip-limit-mb.")
                sys.exit(2)
    log("done in %.1f min" % ((time.time() - t0) / 60))


# ----------------------------------------------------------------------------------------------
def main():
    global PID, ZIM_PATH, DROP_CLASSES
    ap = argparse.ArgumentParser()
    ap.add_argument("--zim", required=True)
    ap.add_argument("--workdir", default="work")
    ap.add_argument("--outdir", default="output")
    ap.add_argument("--part-mb", type=int, default=10, help="target size of one HTML file (MB); a file may go a bit over")
    ap.add_argument("--hard-mb", type=float, default=None, help="warn if an HTML file is bigger than this (MB); default part-mb + 10%%")
    ap.add_argument("--zip-limit-mb", type=int, default=1900, help="start a new ZIP when the current one reaches about this size (MB)")
    # defaults changed for Wikisource (ויקיטקסט) instead of Wikipedia
    ap.add_argument("--title", default="ויקיטקסט העברית", help="name shown inside the files")
    ap.add_argument("--keep-references", action="store_true")
    ap.add_argument("--no-redirects", action="store_true", help="do not add redirect names to the indexes")
    ap.add_argument("--limit", type=int, default=0, help="only first N articles (testing)")
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--zip", action="store_true", help="pack everything into one ZIP (split only above the limit) and delete the .html")
    ap.add_argument("--base-name", default="Wikisource-he")
    args = ap.parse_args()
    if args.hard_mb is None:
        args.hard_mb = args.part_mb * 1.1

    os.makedirs(args.workdir, exist_ok=True)
    os.makedirs(args.outdir, exist_ok=True)
    ZIM_PATH = args.zim
    workers = args.workers or max(1, os.cpu_count() or 2)
    DROP_CLASSES = DROP_CLASSES_BASE | (set() if args.keep_references else DROP_CLASSES_REFS)

    # ---- pass 1
    t0 = time.time()
    arts, reds = pass1(args.zim, args.limit)
    if not arts:
        log("No HTML articles found in the ZIM file.")
        sys.exit(1)
    tmap = {p: (re.sub(r"[\t\r\n]+", " ", t).strip() or p) for p, t in arts.items()}
    order = sorted(arts, key=lambda p: (folder_of(tmap[p]), norm_key(tmap[p]), tmap[p], p))
    titles = [tmap[p] for p in order]
    N = len(order)
    PID = {p: i for i, p in enumerate(order)}
    red_lines, seen_red = [], set()
    title_set = set(t.lower() for t in titles)
    for rp, (rt, tp) in reds.items():
        if tp in PID and rp not in PID:
            PID[rp] = PID[tp]
            if not args.no_redirects:
                name = re.sub(r"[\t\r\n]+", " ", rt).strip()
                key = (name.lower(), PID[tp])
                if name and name.lower() not in title_set and key not in seen_red:
                    seen_red.add(key)
                    red_lines.append((name.lower(), name, PID[tp]))
    red_lines.sort()
    del arts, reds, tmap, title_set, seen_red
    log("sorted %d articles, %d redirect names (%.0fs)" % (N, len(red_lines), time.time() - t0))

    # ---- clean every article in parallel -> sqlite
    ctx = mp.get_context("fork")
    dbpath = os.path.join(args.workdir, "cleaned.db")
    if os.path.exists(dbpath):
        os.remove(dbpath)
    pool = ctx.Pool(workers, initializer=worker_init)
    db = sqlite3.connect(dbpath)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.execute("CREATE TABLE a (id INTEGER PRIMARY KEY, h TEXT)")
    CH = 32
    chunks = ([(i, order[i]) for i in range(s, min(N, s + CH))] for s in range(0, N, CH))
    done, t1, total_chars = 0, time.time(), 0
    for res in pool.imap_unordered(work, chunks):
        db.executemany("INSERT INTO a VALUES (?,?)", res)
        done += len(res)
        total_chars += sum(len(h) for _, h in res)
        if (done // CH) % 400 == 0:
            db.commit()
            rate = done / max(1, time.time() - t1)
            log("cleaned %d/%d articles (%.0f/s, eta %.0f min) | %.0f MB of text" %
                (done, N, rate, (N - done) / max(rate, 1) / 60, total_chars / 1e6))
    db.commit()
    log("cleaning done: %d articles, %.0f MB of cleaned HTML (%.0f min)" % (done, total_chars / 1e6, (time.time() - t1) / 60))
    del order
    pool.close()
    pool.join()

    items = build_static(args, db, titles, red_lines, N)
    db.close()
    os.remove(dbpath)
    zip_outputs(args, items, t0)


if __name__ == "__main__":
    main()

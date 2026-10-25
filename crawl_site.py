#!/usr/bin/env python3
"""
Crawl a website with SingleFile and pack ALL pages into ONE offline HTML file.
The output file contains a small viewer (back / home / page list with search)
and every page inside it, so links between pages work offline.

Link discovery is done on the HTML saved by Chromium (after JavaScript ran),
so no separate HTTP requests are needed (works better behind Cloudflare etc).
"""
import argparse
import html as htmllib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse, urldefrag, unquote

ap = argparse.ArgumentParser()
ap.add_argument("--url", required=True)
ap.add_argument("--chrome", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--max-pages", type=int, default=50)
ap.add_argument("--include", default="", help="regex; only links matching it are followed (start page always saved)")
ap.add_argument("--block-images", default="false")
ap.add_argument("--workers", type=int, default=3)
args = ap.parse_args()

BLOCK_IMAGES = args.block_images.strip().lower() == "true"
INCLUDE = re.compile(args.include) if args.include.strip() else None

SKIP_EXT = re.compile(
    r"\.(jpe?g|png|gif|svg|webp|ico|bmp|pdf|zip|7z|rar|gz|tar|mp3|m4a|ogg|wav|mp4|webm|mkv|avi|"
    r"css|js|json|xml|txt|rss|woff2?|ttf|apk|exe|dmg)$",
    re.I,
)
LINK_RE = re.compile(r"<a\b[^>]*?\shref\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", re.I | re.S)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def key(u):
    """Canonical key. MUST match the key() function in the JS viewer below."""
    p = urlparse(u)
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = unquote(p.path).rstrip("/") or "/"
    return host + path + ("?" + p.query if p.query else "")


START = urldefrag(args.url)[0]
START_HOST = key(START).split("/", 1)[0]


def wanted(link):
    p = urlparse(link)
    if p.scheme not in ("http", "https"):
        return False
    if key(link).split("/", 1)[0] != START_HOST:
        return False
    if SKIP_EXT.search(p.path):
        return False
    if INCLUDE and not INCLUDE.search(link):
        return False
    return True


def extract_links(page_html, base_url):
    out = []
    for m in LINK_RE.finditer(page_html):
        href = htmllib.unescape((m.group(1) or m.group(2) or "").strip())
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:", "data:")):
            continue
        full = urldefrag(urljoin(base_url, href))[0]
        if wanted(full):
            out.append(full)
    return out


def save_page(url):
    cmd_base = [
        "single-file", url, "{out}",
        f"--browser-executable-path={args.chrome}",
        '--browser-args=["--no-sandbox"]',
        "--block-videos", "--block-audios",
        "--remove-unused-styles", "--remove-alternative-fonts", "--compress-CSS",
    ]
    if BLOCK_IMAGES:
        cmd_base.append("--block-images")
    for attempt in (1, 2):
        # Fresh EMPTY directory + a file name that does not exist yet.
        # (single-file renames the output to "name (1).html" if the file already exists,
        # which is what happened when the temp file was pre-created.)
        tmpdir = tempfile.mkdtemp(prefix="sf_")
        tmp = os.path.join(tmpdir, "page.html")
        cmd = [c.replace("{out}", tmp) for c in cmd_base]
        try:
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
                err = ("rc=%s | %s | %s" % (r.returncode, r.stderr or "", r.stdout or ""))[-600:]
            except subprocess.TimeoutExpired:
                err = "timeout"
            # take whatever HTML file single-file produced in that directory
            for name in sorted(os.listdir(tmpdir)):
                path = os.path.join(tmpdir, name)
                if os.path.isfile(path) and os.path.getsize(path) > 0:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    if "<html" in content.lower():
                        return url, content
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        print(f"  attempt {attempt} failed for {url}: {err}", flush=True)
    return url, None


# ------------------------------------------------------------------ crawl
pages = {}          # key -> {"u": url, "t": title, "h": html}
seen = {key(START)}
queue = [START]
failed = []

while queue and len(pages) < args.max_pages:
    room = args.max_pages - len(pages)
    batch, queue = queue[:room], queue[room:]
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(save_page, batch))
    for url, content in results:
        if content is None:
            failed.append(url)
            print(f"FAILED  {url}", flush=True)
            continue
        m = TITLE_RE.search(content)
        title = htmllib.unescape(m.group(1)).strip() if m else url
        pages[key(url)] = {"u": url, "t": title or url, "h": content}
        print(f"OK [{len(pages)}/{args.max_pages}] {url}", flush=True)
        for link in extract_links(content, url):
            k = key(link)
            if k not in seen:
                seen.add(k)
                queue.append(link)

if not pages:
    print("No pages were saved.")
    sys.exit(1)

print(f"Saved {len(pages)} pages, failed {len(failed)}, not saved (limit/filter) {len(queue)}")

# ------------------------------------------------------------------ viewer
VIEWER = r"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Offline Website</title>
<style>
html,body{margin:0;height:100%;font-family:sans-serif;background:#fff}
#bar{display:flex;gap:6px;padding:6px;background:#222;color:#fff;align-items:center;height:34px}
#bar button{padding:4px 12px;font-size:18px;border:0;border-radius:4px;background:#444;color:#fff}
#title{flex:1;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;font-size:13px}
#f{width:100%;height:calc(100% - 46px);border:0;display:block}
#list{position:fixed;top:46px;left:0;right:0;bottom:0;background:#fff;overflow:auto;z-index:5}
#list[hidden]{display:none}
#q{width:calc(100% - 16px);margin:8px;padding:8px;font-size:16px;box-sizing:border-box}
.it{padding:10px 12px;border-bottom:1px solid #ddd;font-size:15px}
.it small{display:block;color:#777;font-size:11px;direction:ltr;overflow:hidden;text-overflow:ellipsis}
</style></head><body>
<div id="bar"><button id="back">&larr;</button><button id="home">&#8962;</button><button id="menu">&#9776;</button><span id="title" dir="auto"></span></div>
<div id="list" hidden><input id="q" placeholder="Search pages..."><div id="items"></div></div>
<iframe id="f"></iframe>
<script>
const PAGES = __DATA__;
const START = __START__;
const f = document.getElementById('f'), t = document.getElementById('title');
const list = document.getElementById('list'), items = document.getElementById('items'), q = document.getElementById('q');
let stack = [], cur = null, pendingHash = null;

function key(u){
  try{
    const x = new URL(u);
    let h = x.hostname.toLowerCase().replace(/^www\./,'');
    let p = x.pathname; try{ p = decodeURIComponent(p); }catch(e){}
    p = p.replace(/\/+$/,'') || '/';
    return h + p + x.search;
  }catch(e){ return null; }
}
function go(k, push, hash){
  if(!(k in PAGES)) return false;
  if(push && cur) stack.push(cur);
  cur = k; pendingHash = hash || null;
  t.textContent = PAGES[k].t;
  list.hidden = true;
  f.srcdoc = PAGES[k].h;
  return true;
}
f.addEventListener('load', () => {
  let d; try{ d = f.contentDocument; }catch(e){ return; }
  if(!d) return;
  if(pendingHash){
    const el = d.getElementById(pendingHash) || d.getElementsByName(pendingHash)[0];
    if(el) el.scrollIntoView(); pendingHash = null;
  }
  d.addEventListener('click', e => {
    const a = e.target.closest && e.target.closest('a[href]');
    if(!a) return;
    const href = a.getAttribute('href') || '';
    if(href.startsWith('#')){
      e.preventDefault();
      const id = decodeURIComponent(href.slice(1));
      const el = d.getElementById(id) || d.getElementsByName(id)[0];
      if(el) el.scrollIntoView(); else d.defaultView.scrollTo(0,0);
      return;
    }
    let full; try{ full = new URL(href, PAGES[cur].u); }catch(_){ return; }
    if(full.protocol !== 'http:' && full.protocol !== 'https:') return;
    e.preventDefault();
    const k = key(full.href);
    if(k && PAGES[k]) go(k, true, full.hash ? full.hash.slice(1) : null);
    else alert('Page not saved offline:\n' + full.href);
  }, true);
});
document.getElementById('back').onclick = () => { if(stack.length) go(stack.pop(), false); };
document.getElementById('home').onclick = () => { stack = []; go(START, false); };
document.getElementById('menu').onclick = () => { list.hidden = !list.hidden; if(!list.hidden){ q.focus(); render(); } };
function render(){
  const s = q.value.trim().toLowerCase();
  items.innerHTML = '';
  Object.keys(PAGES).forEach(k => {
    const p = PAGES[k];
    if(s && !(p.t.toLowerCase().includes(s) || p.u.toLowerCase().includes(s))) return;
    const div = document.createElement('div'); div.className = 'it';
    div.textContent = p.t;
    const sm = document.createElement('small'); sm.textContent = p.u; div.appendChild(sm);
    div.onclick = () => { stack = []; go(k, false); };
    items.appendChild(div);
  });
}
q.addEventListener('input', render);
go(START, false);
</script></body></html>"""

start_key = key(START) if key(START) in pages else next(iter(pages))
data = json.dumps(pages, ensure_ascii=False, separators=(",", ":"))
data = data.replace("</", "<\\/").replace("<!--", "<\\!--")
out_html = VIEWER.replace("__DATA__", data).replace("__START__", json.dumps(start_key))

os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
with open(args.out, "w", encoding="utf-8") as fh:
    fh.write(out_html)

size_mb = os.path.getsize(args.out) / 1048576
print(f"Wrote {args.out}: {len(pages)} pages, {size_mb:.1f} MB")
if size_mb > 80:
    print("WARNING: file is large; some Android HTML viewers may fail to open it. "
          "Try block_images=true or a smaller max_pages / include filter.")

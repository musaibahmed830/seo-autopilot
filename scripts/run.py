"""SEO autopilot: runs daily in GitHub Actions.

For every site in config.json it will:
 1. audit the live site (title, meta, links, sitemap, speed)
 2. generate ONE draft post with Gemini (free tier) and open a draft Pull Request
    in the site's repo (you review + merge = publish)
 3. count merged / open auto-posts
 4. save everything to docs/data/history.json (the dashboard reads this file)
"""
import base64
import datetime
import json
import os
import pathlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
HIST = ROOT / "docs" / "data" / "history.json"
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GH_TOKEN = os.environ.get("TARGET_REPO_TOKEN", "")
PSI_KEY = os.environ.get("PSI_API_KEY", "")
MODELS = [m for m in (os.environ.get("GEMINI_MODEL"), "gemini-3.5-flash", "gemini-3-flash-preview", "gemini-2.5-flash") if m]
TODAY = datetime.date.today().isoformat()


# ---------- helpers ----------
def http(url, data=None, headers=None, method=None, timeout=60):
    h = {"User-Agent": "Mozilla/5.0 (seo-autopilot)"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, e.read(), time.time() - t
    except Exception as e:
        return 0, str(e).encode(), time.time() - t


def norm_repo(r):
    r = r.strip()
    for p in ("https://github.com/", "http://github.com/"):
        r = r.replace(p, "")
    if r.endswith(".git"):
        r = r[:-4]
    return r.strip("/")


def gh(path, method="GET", payload=None):
    h = {
        "Authorization": "Bearer " + GH_TOKEN,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        h["Content-Type"] = "application/json"
    code, body, _ = http("https://api.github.com" + path, data, h, method)
    try:
        return code, json.loads(body)
    except Exception:
        return code, None


# ---------- 1. site audit ----------
class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title, self._in_title = "", False
        self.desc, self.canon = None, None
        self.h1 = self.imgs = self.noalt = 0
        self.links = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta" and (a.get("name") or "").lower() == "description":
            self.desc = a.get("content") or ""
        elif tag == "link" and a.get("rel") == "canonical":
            self.canon = a.get("href")
        elif tag == "h1":
            self.h1 += 1
        elif tag == "img":
            self.imgs += 1
            if not (a.get("alt") or "").strip():
                self.noalt += 1
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"].strip())

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, d):
        if self._in_title:
            self.title += d


def audit(site):
    issues, m = [], {}
    code, body, secs = http(site)
    if code != 200:
        return {"issues": [f"Homepage returned status {code}"], "metrics": {}}
    m["load_sec"] = round(secs, 2)
    if secs > 3:
        issues.append(f"Homepage is slow to respond ({secs:.1f}s)")
    p = Page()
    p.feed(body.decode("utf-8", "ignore"))
    t = p.title.strip()
    if not t:
        issues.append("Homepage has no <title>")
    elif not 30 <= len(t) <= 65:
        issues.append(f"Homepage title length is {len(t)} (aim for 30-65)")
    if not p.desc:
        issues.append("Homepage has no meta description")
    elif not 70 <= len(p.desc) <= 160:
        issues.append(f"Meta description length is {len(p.desc)} (aim for 70-160)")
    if p.h1 != 1:
        issues.append(f"Homepage has {p.h1} H1 tags (should be exactly 1)")
    if p.noalt:
        issues.append(f"{p.noalt} of {p.imgs} homepage images have no alt text")
    if not p.canon:
        issues.append("No canonical tag on homepage")
    dead = sum(1 for h in p.links if h in ("#", ""))
    if dead:
        issues.append(f"{dead} links on homepage point to '#' (empty links)")

    host = urllib.parse.urlparse(site).netloc
    seen, sample = set(), []
    for h in p.links:
        u = urllib.parse.urljoin(site + "/", h).split("#")[0]
        if urllib.parse.urlparse(u).netloc == host and u not in seen and u != site + "/":
            seen.add(u)
            sample.append(u)
    broken = []
    for u in sample[:25]:
        c = http(u, timeout=30)[0]
        if c == 0 or c >= 400:
            broken.append(u)
    m["links_checked"] = len(sample[:25])
    m["links_broken"] = len(broken)
    if broken:
        issues.append(f"{len(broken)} broken internal links, e.g. {broken[0]}")

    if http(site + "/robots.txt")[0] != 200:
        issues.append("robots.txt not found")
    code, sm, _ = http(site + "/sitemap.xml")
    if code != 200:
        issues.append("sitemap.xml not found")
    else:
        try:
            root = ET.fromstring(sm)
            locs = [e for e in root.iter() if e.tag.endswith("}loc") or e.tag == "loc"]
            mods = [e.text[:10] for e in root.iter() if (e.tag.endswith("}lastmod") or e.tag == "lastmod") and e.text]
            m["sitemap_urls"] = len(locs)
            if mods:
                newest = max(mods)
                m["sitemap_newest"] = newest
                age = (datetime.date.today() - datetime.date.fromisoformat(newest)).days
                if age > 14:
                    issues.append(f"Newest sitemap entry is {age} days old (site looks inactive)")
        except Exception:
            issues.append("sitemap.xml could not be parsed")
    return {"issues": issues, "metrics": m}


def psi(url):
    qs = urllib.parse.urlencode(
        {"url": url, "strategy": "mobile", "category": ["performance", "seo"]}, doseq=True)
    if PSI_KEY:
        qs += "&key=" + PSI_KEY
    code, body, _ = http("https://www.googleapis.com/pagespeedonline/v5/runPagespeed?" + qs, timeout=120)
    if code != 200:
        return {}
    j = json.loads(body)["lighthouseResult"]
    return {
        "perf": round(j["categories"]["performance"]["score"] * 100),
        "seo": round(j["categories"]["seo"]["score"] * 100),
        "lcp": j["audits"]["largest-contentful-paint"]["displayValue"],
    }


# ---------- 2. post generation ----------
def headlines(queries):
    out, seen = [], set()
    for q in queries:
        url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q + " when:7d")
               + "&hl=en-PK&gl=PK&ceid=PK:en")
        code, body, _ = http(url)
        if code != 200:
            continue
        try:
            items = list(ET.fromstring(body).iter("item"))[:8]
        except Exception:
            continue
        for it in items:
            t = (it.findtext("title") or "").strip()
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
    return out[:30]


def gemini(prompt):
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.8},
    }).encode()
    hdr = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY}
    last = "no response"
    for model in MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(3):
            code, resp, _ = http(url, body, hdr, timeout=120)
            if code == 200:
                try:
                    return json.loads(json.loads(resp)["candidates"][0]["content"]["parts"][0]["text"])
                except Exception:
                    last = f"{model}: reply was not valid JSON"
                    break
            try:
                msg = json.loads(resp)["error"]["message"][:140]
            except Exception:
                msg = resp[:140].decode("utf-8", "ignore")
            last = f"{model} HTTP {code}: {msg}"
            if code == 429:
                time.sleep(30 * (attempt + 1))  # free tier limit: wait and retry
                continue
            break  # 400/403/404: try the next model
    raise RuntimeError("Gemini failed - " + last)


def build_prompt(niche, heads, slugs):
    nl = "\n"
    return f"""You are an editor for a fashion blog focused on: {niche}.
Today's date: {TODAY}.

Recent headlines (last 7 days):
{nl.join('- ' + h for h in heads)}

Slugs already published (do NOT repeat these topics):
{nl.join('- ' + s for s in slugs[-80:])}

Pick ONE topic people are searching for right now and write an original, helpful
article of 900-1200 words in Markdown.
Rules:
- Use only facts supported by the headlines above or very general, well-known knowledge.
  Do NOT invent quotes, prices, statistics, dates or brand claims. If unsure, stay general.
- Short intro, H2/H3 headings, practical styling tips, and a short FAQ (3 questions).
- No H1 in the body (title is separate). No filler. Natural, human tone.
Return JSON with keys: title (max 60 chars), slug (kebab-case), description (max 155 chars),
category (one of: Trending, Style Tips, Accessories, Wardrobe Essentials, Sustainable),
body (Markdown string)."""


def render(post):
    return "\n".join([
        "---",
        f"title: {json.dumps(post['title'])}",
        f"description: {json.dumps(post['description'])}",
        f"category: {json.dumps(post['category'])}",
        f"date: {json.dumps(TODAY)}",
        "---",
        "",
        post["body"].strip(),
        "",
    ])


def make_post(s, repo):
    branch = s.get("branch", "main")
    cdir = s.get("content_dir", "content/posts").strip("/")
    ext = s.get("ext", "mdx")
    code, prs = gh(f"/repos/{repo}/pulls?state=open&per_page=100")
    if code != 200:
        raise RuntimeError(f"cannot read pull requests of {repo} (HTTP {code}); check the token's access")
    open_auto = [x for x in prs if x["head"]["ref"].startswith("auto/post-")]
    if len(open_auto) >= int(s.get("max_open_prs", 3)):
        return None, f"Skipped new post: {len(open_auto)} drafts are waiting for your review"
    code, files = gh(f"/repos/{repo}/contents/{cdir}?ref={branch}")
    slugs = [f["name"].rsplit(".", 1)[0] for f in files] if code == 200 and isinstance(files, list) else []
    heads = headlines(s.get("queries") or [s.get("niche", "fashion")])
    if not heads:
        raise RuntimeError("no trending headlines fetched")
    post = gemini(build_prompt(s.get("niche", "fashion"), heads, slugs))
    if len(post.get("body", "").split()) < 600:
        raise RuntimeError("generated post too short, not saved")
    slug = re.sub(r"[^a-z0-9-]+", "-", post["slug"].lower()).strip("-")[:70]
    if slug in slugs:
        raise RuntimeError("slug already exists, not saved")
    path = f"{cdir}/{slug}.{ext}"
    code, ref = gh(f"/repos/{repo}/git/ref/heads/{branch}")
    if code != 200:
        raise RuntimeError(f"cannot read branch {branch} (HTTP {code})")
    bname = f"auto/post-{TODAY}-{slug[:30]}"
    code, _ = gh(f"/repos/{repo}/git/refs", "POST", {"ref": "refs/heads/" + bname, "sha": ref["object"]["sha"]})
    if code not in (200, 201):
        raise RuntimeError(f"cannot create branch (HTTP {code}); token needs Contents: write")
    code, _ = gh(f"/repos/{repo}/contents/{path}", "PUT", {
        "message": f"Add draft post: {post['title']}",
        "content": base64.b64encode(render(post).encode()).decode(),
        "branch": bname,
    })
    if code not in (200, 201):
        raise RuntimeError(f"cannot write post file (HTTP {code})")
    pr = {"title": f"Draft post: {post['title']}", "head": bname, "base": branch,
          "body": "Auto-generated draft. Read it, fix any wrong facts, then merge to publish.", "draft": True}
    code, res = gh(f"/repos/{repo}/pulls", "POST", pr)
    if code == 422:  # drafts not allowed on some private repos
        pr.pop("draft")
        code, res = gh(f"/repos/{repo}/pulls", "POST", pr)
    if code not in (200, 201):
        raise RuntimeError(f"cannot open pull request (HTTP {code})")
    return res["html_url"], "Draft post created"


# ---------- 3. progress counts ----------
def pr_stats(repo):
    code, prs = gh(f"/repos/{repo}/pulls?state=all&per_page=100")
    if code != 200 or not isinstance(prs, list):
        return {}
    auto = [x for x in prs if x["head"]["ref"].startswith("auto/post-")]
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    merged = [x for x in auto if x.get("merged_at")]
    return {
        "drafts_open": sum(1 for x in auto if x["state"] == "open"),
        "merged_total": len(merged),
        "merged_30d": sum(1 for x in merged if x["merged_at"] >= cutoff),
    }


# ---------- main ----------
def main():
    hist = {"sites": {}}
    if HIST.exists():
        try:
            hist = json.loads(HIST.read_text(encoding="utf-8"))
        except Exception:
            pass
    for s in CONFIG["sites"]:
        name, repo, site = s["name"], norm_repo(s["repo"]), s["site"].rstrip("/")
        rec, notes = {"date": TODAY}, []
        try:
            a = audit(site)
            rec.update(a["metrics"])
            rec["issues"] = a["issues"]
        except Exception as e:
            rec["issues"] = []
            notes.append(f"Audit failed: {type(e).__name__}")
        try:
            rec.update(psi(site))
        except Exception as e:
            notes.append(f"PageSpeed failed: {type(e).__name__}")
        if GH_TOKEN and GEMINI_KEY:
            try:
                url, msg = make_post(s, repo)
                notes.append(msg)
                if url:
                    rec["pr_url"] = url
            except Exception as e:
                notes.append(f"Post step failed: {e}")
            try:
                rec.update(pr_stats(repo))
            except Exception as e:
                notes.append(f"PR stats failed: {type(e).__name__}")
        else:
            notes.append("Add GEMINI_API_KEY and TARGET_REPO_TOKEN secrets to enable auto posts")
        rec["notes"] = notes
        site_hist = hist["sites"].setdefault(name, {"days": []})
        site_hist.update({"repo": repo, "site": site})
        site_hist["days"] = [d for d in site_hist["days"] if d["date"] != TODAY] + [rec]
        site_hist["days"] = site_hist["days"][-120:]
        print(name, json.dumps(rec)[:500])
    hist["updated"] = datetime.datetime.utcnow().isoformat() + "Z"
    HIST.parent.mkdir(parents=True, exist_ok=True)
    HIST.write_text(json.dumps(hist, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()

"""SEO autopilot v2 - runs every hour in GitHub Actions.

Every run:   checks whether it is time to write a post (best-hour slots) and, if so,
             finds global trends, picks a topic, researches keywords, writes it with
             Gemini (free tier) and opens a Pull Request in the site repo.
First run of the day (or a manual run): also audits the site, runs PageSpeed, pulls
             Search Console (rankings, countries, queries) and GA4 (best hours), and
             builds the "what worked / what flopped" feedback used for the next posts.
Everything is saved in docs/data/*.json for the dashboard.
"""
import base64
import datetime
import json
import os
import pathlib
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
DATA = ROOT / "docs" / "data"
NOW = datetime.datetime.utcnow()
TODAY = NOW.date().isoformat()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GH_TOKEN = os.environ.get("TARGET_REPO_TOKEN", "")
PSI_KEY = os.environ.get("PSI_API_KEY", "")
GOOGLE_SA = os.environ.get("GOOGLE_SA_JSON", "")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
FORCE = bool(os.environ.get("FORCE_FULL"))  # manual run: full report + post now

SC_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
GA_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"

DEFAULT_REGIONS = [
    {"code": "PK", "hl": "en-PK", "lang": "en"}, {"code": "US", "hl": "en-US", "lang": "en"},
    {"code": "GB", "hl": "en-GB", "lang": "en"}, {"code": "AU", "hl": "en-AU", "lang": "en"},
    {"code": "CA", "hl": "en-CA", "lang": "en"}, {"code": "AE", "hl": "en", "lang": "en"},
    {"code": "NO", "hl": "no", "lang": "no"}, {"code": "TR", "hl": "tr", "lang": "tr"},
    {"code": "FI", "hl": "fi", "lang": "fi"},
]
A3 = {"pak": "PK", "usa": "US", "gbr": "GB", "aus": "AU", "nor": "NO", "tur": "TR", "fin": "FI",
      "are": "AE", "can": "CA", "ind": "IN", "deu": "DE", "fra": "FR", "sau": "SA", "bgd": "BD",
      "idn": "ID", "mys": "MY", "sgp": "SG", "zaf": "ZA", "nga": "NG", "egy": "EG", "phl": "PH",
      "nzl": "NZ", "irl": "IE", "swe": "SE", "dnk": "DK", "nld": "NL", "ita": "IT", "esp": "ES",
      "bra": "BR", "mex": "MX", "jpn": "JP", "kor": "KR", "qat": "QA", "kwt": "KW", "omn": "OM",
      "bhr": "BH", "lka": "LK", "npl": "NP", "afg": "AF", "rus": "RU", "pol": "PL"}

SAFE = ("Skip tragedies, deaths, disasters, wars, crime, partisan politics, medical or legal advice "
        "and anything about private individuals. Never state unverified claims as facts.")


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


def jload(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def jsave(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")


def norm_repo(r):
    r = r.strip()
    for p in ("https://github.com/", "http://github.com/"):
        r = r.replace(p, "")
    if r.endswith(".git"):
        r = r[:-4]
    return r.strip("/")


def gh(path, method="GET", payload=None):
    h = {"Authorization": "Bearer " + GH_TOKEN, "Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        h["Content-Type"] = "application/json"
    code, body, _ = http("https://api.github.com" + path, data, h, method)
    try:
        return code, json.loads(body)
    except Exception:
        return code, None


# ---------- site audit ----------
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
            locs = [e for e in root.iter() if e.tag.endswith("loc")]
            mods = [e.text[:10] for e in root.iter() if e.tag.endswith("lastmod") and e.text]
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
    qs = urllib.parse.urlencode({"url": url, "strategy": "mobile", "category": ["performance", "seo"]}, doseq=True)
    if PSI_KEY:
        qs += "&key=" + PSI_KEY
    code, body, _ = http("https://www.googleapis.com/pagespeedonline/v5/runPagespeed?" + qs, timeout=120)
    if code != 200:
        return {}
    j = json.loads(body)["lighthouseResult"]
    return {"perf": round(j["categories"]["performance"]["score"] * 100),
            "seo": round(j["categories"]["seo"]["score"] * 100),
            "lcp": j["audits"]["largest-contentful-paint"]["displayValue"]}


# ---------- Google Search Console + GA4 ----------
_sess = {}


def gsess(scope):
    if scope not in _sess:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_info(json.loads(GOOGLE_SA), scopes=[scope])
        _sess[scope] = AuthorizedSession(creds)
    return _sess[scope]


def gsc_rows(prop, dims, start, end, limit):
    url = ("https://www.googleapis.com/webmasters/v3/sites/" + urllib.parse.quote(prop, safe="")
           + "/searchAnalytics/query")
    r = gsess(SC_SCOPE).post(url, json={"startDate": start, "endDate": end, "dimensions": dims,
                                        "rowLimit": limit}, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Search Console HTTP {r.status_code}: check gsc_property in config.json and that "
                           "the service account email is added as a user in Search Console")
    return r.json().get("rows", [])


def gsc_pull(prop):
    end = datetime.date.today() - datetime.timedelta(days=3)  # GSC data lags ~2-3 days
    start = end - datetime.timedelta(days=27)
    st, en = start.isoformat(), end.isoformat()
    pages = [{"url": r["keys"][0], "clicks": int(r["clicks"]), "impr": int(r["impressions"]),
              "pos": round(r["position"], 1)} for r in gsc_rows(prop, ["page"], st, en, 1000)]
    ctry = [{"country": A3.get(r["keys"][0], r["keys"][0].upper()), "clicks": int(r["clicks"]),
             "impr": int(r["impressions"])} for r in gsc_rows(prop, ["country"], st, en, 15)]
    qs = [{"q": r["keys"][0], "clicks": int(r["clicks"]), "impr": int(r["impressions"]),
           "pos": round(r["position"], 1)} for r in gsc_rows(prop, ["query"], st, en, 25)]
    ser = [{"d": r["keys"][0][5:], "c": int(r["clicks"]), "i": int(r["impressions"])}
           for r in sorted(gsc_rows(prop, ["date"], st, en, 40), key=lambda r: r["keys"][0])]
    return {"ok": True, "range": [st, en], "clicks": sum(p["clicks"] for p in pages),
            "impr": sum(p["impr"] for p in pages), "series": ser, "countries": ctry, "queries": qs,
            "top_pages": sorted(pages, key=lambda p: (-p["clicks"], -p["impr"]))[:12]}, pages


def ga4_hours(pid, offset):
    """Page views by hour of day, converted to UTC (GA4 reports in the property's time zone)."""
    r = gsess(GA_SCOPE).post(f"https://analyticsdata.googleapis.com/v1beta/properties/{pid}:runReport", json={
        "dateRanges": [{"startDate": "28daysAgo", "endDate": "yesterday"}],
        "dimensions": [{"name": "hour"}], "metrics": [{"name": "screenPageViews"}]}, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"GA4 HTTP {r.status_code}: check ga4_property_id and Viewer access")
    hrs = [0] * 24
    for row in r.json().get("rows", []):
        hrs[(int(row["dimensionValues"][0]["value"]) - offset) % 24] += int(row["metricValues"][0]["value"])
    return hrs


def make_slots(hours, n, default_peaks):
    """Post 2 hours before each peak-traffic hour (time to review, crawl and share)."""
    if hours and sum(hours) >= 50:
        peaks = sorted(range(24), key=lambda h: -hours[h])[:n]
    else:
        peaks = list(default_peaks)[:n]
    while len(peaks) < n:
        peaks.append((peaks[-1] + 3) % 24)
    return sorted((h - 2) % 24 for h in peaks), peaks


def rank_label(impr, pos, age):
    if not impr:
        return "Too new" if age < 7 else "Not ranking / not indexed"
    return "Page 1" if pos <= 10 else "Page 2" if pos <= 20 else "Weak (page 3+)"


def match_posts(site_posts, pages):
    out = []
    for p in site_posts:
        rows = [g for g in pages if p["slug"] in g["url"]]
        impr = sum(g["impr"] for g in rows)
        clicks = sum(g["clicks"] for g in rows)
        pos = sum(g["pos"] * g["impr"] for g in rows) / impr if impr else None
        age = (datetime.date.today() - datetime.date.fromisoformat(p["date"])).days
        out.append({**p, "clicks": clicks, "impr": impr, "pos": round(pos, 1) if pos else None,
                    "age": age, "rank": rank_label(impr, pos, age)})
    return out


def make_insights(pp, gsc):
    """Plain-text feedback fed back into the next topic choice (the learning loop)."""
    L = []
    if gsc and gsc.get("countries"):
        L.append("Audience countries (28d clicks): " + ", ".join(f"{c['country']} {c['clicks']}" for c in gsc["countries"][:6]))
    mature = [p for p in pp if p["age"] >= 7]
    if mature:
        top = sorted(mature, key=lambda p: (-p["impr"], -p["clicks"]))[:3]
        low = sorted(mature, key=lambda p: (p["impr"], p["clicks"]))[:3]
        fmt = lambda p: f'"{p["title"]}" ({p["type"]}, {p["region"]}, {p["impr"]} impressions, {p["clicks"]} clicks)'
        L.append("Best posts: " + "; ".join(fmt(p) for p in top))
        L.append("Weakest posts (avoid similar topics): " + "; ".join(fmt(p) for p in low))
        for key in ("type", "region"):
            g = {}
            for p in mature:
                g.setdefault(p[key], []).append(p["impr"])
            L.append(f"Average impressions by {key}: " + ", ".join(
                f"{k} {round(sum(v) / len(v))}" for k, v in sorted(g.items(), key=lambda kv: -sum(kv[1]) / len(kv[1]))))
    if gsc and gsc.get("queries"):
        L.append("Top search queries now: " + ", ".join(q["q"] for q in gsc["queries"][:8]))
    return "\n".join(L) or "Not enough data yet. Explore varied topics."


# ---------- trends + keywords ----------
def rss_items(url):
    code, body, _ = http(url)
    if code != 200:
        return []
    try:
        return list(ET.fromstring(body).iter("item"))
    except Exception:
        return []


def child(it, name):
    for e in it:
        if e.tag.endswith(name):
            return (e.text or "").strip()
    return ""


def collect_trends(regions):
    T, N, F = [], [], []
    for r in regions:
        cc, hl, lang = r["code"], r["hl"], r["lang"]
        for it in rss_items(f"https://trends.google.com/trending/rss?geo={cc}")[:8]:
            t = (it.findtext("title") or "").strip()
            if t:
                T.append(f"[{cc}] {t} (searches: {child(it, 'approx_traffic') or '?'})")
        q = f"hl={hl}&gl={cc}&ceid={cc}:{lang}"
        for it in rss_items("https://news.google.com/rss?" + q)[:5]:
            N.append(f"[{cc}] {(it.findtext('title') or '').strip()}")
        for it in rss_items("https://news.google.com/rss/search?q=fashion+when:3d&" + q)[:4]:
            F.append(f"[{cc}] {(it.findtext('title') or '').strip()}")
    return T, N, F


def autocomplete(seed, cc="US"):
    out = []
    for suffix in ("", " best", " how to", f" {NOW.year}"):
        code, body, _ = http("https://suggestqueries.google.com/complete/search?client=firefox&hl=en&gl="
                             + cc.lower() + "&q=" + urllib.parse.quote(seed + suffix), timeout=20)
        if code == 200:
            try:
                out += json.loads(body.decode("utf-8", "ignore"))[1]
            except Exception:
                pass
    return list(dict.fromkeys(out))[:14]


# ---------- writing ----------
def gemini(prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                       "generationConfig": {"responseMimeType": "application/json", "temperature": 0.8}}).encode()
    hdr = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY}
    for attempt in range(4):
        code, resp, _ = http(url, body, hdr, timeout=120)
        if code == 200:
            try:
                return json.loads(json.loads(resp)["candidates"][0]["content"]["parts"][0]["text"])
            except Exception:
                pass
        time.sleep(20 * (attempt + 1))  # free tier: back off on 429
    raise RuntimeError("Gemini failed after retries (limit reached or bad key)")


def pick_topic(ptype, trends, news, fash, insights, slugs):
    nl = "\n"
    if ptype == "fashion":
        pool = fash[:30] + trends[:40]
        rule = ("Prefer fashion, style, beauty and accessory topics. If a general trend (event, festival, season, "
                "celebrity look) has a fashion angle, use that angle.")
    else:
        pool = trends[:50] + news[:40]
        rule = ("Prefer entertainment, tech, sports, culture, lifestyle and business stories many people are "
                "searching for. Write it as a clear explainer.")
    prompt = f"""You are the editorial strategist of a global English-language fashion and trends website.
Today: {TODAY}. Content type to write now: {ptype}. {rule}

LIVE TRENDS AND HEADLINES (country code in brackets):
{nl.join(pool)}

PERFORMANCE FEEDBACK FROM OUR OWN DATA (learn from it):
{insights}

ALREADY PUBLISHED slugs (do not repeat):
{nl.join('- ' + s for s in slugs[-80:])}

Pick ONE topic with high search demand and viral potential for an international audience
(US, UK, Australia, Canada, Norway, Turkey, Finland, UAE, Pakistan). {SAFE}
Return JSON: type, region (2-letter code of the main angle or GLOBAL), topic (short), seed_keyword (2-4 words
people would search), angle (one sentence)."""
    return gemini(prompt)


def write_article(topic, kws, ptype, ctx):
    nl = "\n"
    shape = ("Sections: What happened, Why it matters, What to know, FAQ." if ptype == "news"
             else "Include practical styling tips and a short FAQ.")
    prompt = f"""Write an original, helpful article of 900-1300 words in Markdown for a global English-language website.
Topic: {topic.get('topic')}. Angle: {topic.get('angle')}. Main region: {topic.get('region')}.
Primary keyword: {topic.get('seed_keyword')}. Weave in these related search phrases naturally (only where they fit):
{', '.join(kws)}

Source headlines (the ONLY facts you may rely on, plus very general well-known knowledge):
{nl.join(ctx[:80])}

Rules:
- Do NOT invent quotes, numbers, prices, dates or claims. If unsure, stay general. Say "according to media reports" when needed.
- {SAFE}
- Use H2/H3 headings, short intro, no H1, no filler, natural human tone. {shape}
Return JSON with keys: title (max 60 chars, includes the primary keyword), slug (kebab-case), description (max 155 chars),
category (one of: Trending, Style Tips, Accessories, Wardrobe Essentials, Sustainable), tags (list of 3-6), body (Markdown)."""
    return gemini(prompt)


def render(post, kw):
    return "\n".join(["---", f"title: {json.dumps(post['title'])}", f"description: {json.dumps(post['description'])}",
                      f"category: {json.dumps(post['category'])}", f"tags: {json.dumps(post.get('tags', []))}",
                      f"keyword: {json.dumps(kw)}", f"date: {json.dumps(TODAY)}", "---", "",
                      post["body"].strip(), ""])


def make_post(s, repo, insights, regions):
    branch, ext = s.get("branch", "main"), s.get("ext", "mdx")
    cdir = s.get("content_dir", "content/posts").strip("/")
    code, prs = gh(f"/repos/{repo}/pulls?state=open&per_page=100")
    if code != 200:
        raise RuntimeError(f"cannot read pull requests of {repo} (HTTP {code}); check the token's access")
    waiting = [x for x in prs if x["head"]["ref"].startswith("auto/post-")]
    if len(waiting) >= int(s.get("max_open_prs", 5)):
        return None, f"Skipped new post: {len(waiting)} drafts are waiting for your review"
    code, files = gh(f"/repos/{repo}/contents/{cdir}?ref={branch}")
    slugs = [f["name"].rsplit(".", 1)[0] for f in files] if code == 200 and isinstance(files, list) else []
    mix = s.get("topic_mix", {"fashion": 0.6, "news": 0.4})
    ptype = random.choices(list(mix), weights=list(mix.values()))[0]
    trends, news, fash = collect_trends(regions)
    if not (trends or news or fash):
        raise RuntimeError("no trending data fetched")
    topic = pick_topic(ptype, trends, news, fash, insights, slugs)
    seed = topic.get("seed_keyword") or topic.get("topic", "fashion")
    reg = str(topic.get("region") or "").upper()
    cc = reg if len(reg) == 2 and reg.isalpha() else "US"
    post = write_article(topic, autocomplete(seed, cc), ptype, trends + news + fash)
    if len(post.get("body", "").split()) < 600:
        raise RuntimeError("generated post too short, not saved")
    slug = re.sub(r"[^a-z0-9-]+", "-", post["slug"].lower()).strip("-")[:70]
    if slug in slugs:
        raise RuntimeError("slug already exists, not saved")
    if ptype == "news":
        post["category"] = s.get("news_category", post["category"])
    code, ref = gh(f"/repos/{repo}/git/ref/heads/{branch}")
    if code != 200:
        raise RuntimeError(f"cannot read branch {branch} (HTTP {code})")
    bname = f"auto/post-{TODAY}-{slug[:30]}"
    code, _ = gh(f"/repos/{repo}/git/refs", "POST", {"ref": "refs/heads/" + bname, "sha": ref["object"]["sha"]})
    if code not in (200, 201):
        raise RuntimeError(f"cannot create branch (HTTP {code}); token needs Contents: write")
    code, _ = gh(f"/repos/{repo}/contents/{cdir}/{slug}.{ext}", "PUT", {
        "message": f"Add post: {post['title']}",
        "content": base64.b64encode(render(post, seed).encode()).decode(), "branch": bname})
    if code not in (200, 201):
        raise RuntimeError(f"cannot write post file (HTTP {code})")
    auto = ptype in s.get("auto_merge_types", [])
    pr = {"title": f"{'Post' if auto else 'Draft post'}: {post['title']}", "head": bname, "base": branch,
          "body": f"Type: {ptype} | Region: {topic.get('region')} | Keyword: {seed}\n\n"
                  "Auto-generated. Check facts before merging.", "draft": not auto}
    code, res = gh(f"/repos/{repo}/pulls", "POST", pr)
    if code == 422 and pr.get("draft"):  # drafts are not allowed on some private repos
        pr.pop("draft")
        code, res = gh(f"/repos/{repo}/pulls", "POST", pr)
    if code not in (200, 201):
        raise RuntimeError(f"cannot open pull request (HTTP {code})")
    merged = False
    if auto:
        merged = gh(f"/repos/{repo}/pulls/{res['number']}/merge", "PUT", {"merge_method": "squash"})[0] == 200
    meta = {"slug": slug, "title": post["title"], "date": TODAY, "type": ptype,
            "region": topic.get("region", "GLOBAL"), "keyword": seed, "pr": res["html_url"], "auto_merged": merged}
    return meta, ("Post published automatically" if merged else "Draft post created")


def pr_stats(repo):
    code, prs = gh(f"/repos/{repo}/pulls?state=all&per_page=100")
    if code != 200 or not isinstance(prs, list):
        return {}
    auto = [x for x in prs if x["head"]["ref"].startswith("auto/post-")]
    cutoff = (NOW - datetime.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    merged = [x for x in auto if x.get("merged_at")]
    return {"drafts_open": sum(1 for x in auto if x["state"] == "open"), "merged_total": len(merged),
            "merged_30d": sum(1 for x in merged if x["merged_at"] >= cutoff)}


# ---------- main ----------
def main():
    hist = jload(DATA / "history.json", {"sites": {}})
    perf = jload(DATA / "perf.json", {"sites": {}})
    posts = jload(DATA / "posts.json", [])
    regions = CONFIG.get("regions") or DEFAULT_REGIONS
    dirty = False
    for s in CONFIG["sites"]:
        name, repo, site = s["name"], norm_repo(s["repo"]), s["site"].rstrip("/")
        sh = hist["sites"].setdefault(name, {"days": []})
        sh.update({"repo": repo, "site": site})
        rec = next((d for d in sh["days"] if d["date"] == TODAY), None)
        full = FORCE or rec is None
        if rec is None:
            rec = {"date": TODAY}
            sh["days"].append(rec)
        sh["days"] = sh["days"][-120:]
        notes = [] if full else rec.get("notes", [])
        sp = perf["sites"].setdefault(name, {})
        mine = [p for p in posts if p["site"] == name]
        ppd = int(s.get("posts_per_day", 1))

        if full:
            dirty = True
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
            pages, hours = [], None
            if GOOGLE_SA and s.get("gsc_property"):
                try:
                    sp["gsc"], pages = gsc_pull(s["gsc_property"])
                except Exception as e:
                    sp["gsc"] = {"ok": False}
                    notes.append(f"Search Console: {e}")
            else:
                notes.append("Search Console not connected (add GOOGLE_SA_JSON secret and gsc_property)")
            if GOOGLE_SA and s.get("ga4_property_id"):
                try:
                    hours = ga4_hours(s["ga4_property_id"], int(s.get("ga4_utc_offset", 0)))
                except Exception as e:
                    notes.append(f"GA4: {e}")
            sp["hours_utc"] = hours
            sp["slots_utc"], sp["peaks_utc"] = make_slots(hours, ppd, s.get("default_peak_hours_utc", [14, 17]))
            sp["posts"] = match_posts(mine, pages)
            sp["insights"] = make_insights(sp["posts"], sp.get("gsc"))
            if GH_TOKEN:
                try:
                    rec.update(pr_stats(repo))
                except Exception as e:
                    notes.append(f"PR stats failed: {type(e).__name__}")

        slots = sp.get("slots_utc") or make_slots(None, ppd, s.get("default_peak_hours_utc", [14, 17]))[0]
        made = len([p for p in mine if p["date"] == TODAY])
        if not (GH_TOKEN and GEMINI_KEY):
            if full:
                notes.append("Add GEMINI_API_KEY and TARGET_REPO_TOKEN secrets to enable auto posts")
        elif made < ppd and (FORCE or NOW.hour >= slots[min(made, len(slots) - 1)]):
            try:
                meta, msg = make_post(s, repo, sp.get("insights", ""), regions)
                if meta:
                    meta["site"] = name
                    posts.append(meta)
                    sp.setdefault("posts", []).append({**meta, "clicks": 0, "impr": 0, "pos": None, "age": 0, "rank": "Too new"})
                    rec["pr_url"] = meta["pr"]
                    rec.update(pr_stats(repo))
                    notes.append(msg)
                    dirty = True
                elif full:
                    notes.append(msg)
            except Exception as e:
                note = f"Post step failed: {e}"
                if note not in notes:
                    notes.append(note)
                    dirty = True
        rec["notes"] = list(dict.fromkeys(notes))
        print(name, "full" if full else "slot-check", json.dumps(rec)[:400])

    if dirty:
        stamp = NOW.isoformat() + "Z"
        hist["updated"] = perf["updated"] = stamp
        jsave(DATA / "history.json", hist)
        jsave(DATA / "perf.json", perf)
        jsave(DATA / "posts.json", posts)


if __name__ == "__main__":
    main()

"""Free LLM fallback chain: Gemini -> Groq -> OpenRouter (free models).

Why: free tiers share capacity with millions of users, so a provider often answers
HTTP 503 ("high demand") or 429 (rate limit) - even when ALL its models are busy at
once. Trying a different provider fixes that. Providers without a key are skipped,
so one key works, three keys is safest. Model names are discovered automatically
(they change often); override with GEMINI_MODELS / GROQ_MODELS / OPENROUTER_MODELS
(comma separated) and LLM_ORDER (default "gemini,groq,openrouter").
"""
import json
import os
import random
import re
import time
import urllib.error
import urllib.request

ORDER = [x.strip() for x in os.environ.get("LLM_ORDER", "gemini,groq,openrouter").split(",") if x.strip()]
KEYS = {"gemini": os.environ.get("GEMINI_API_KEY", ""), "groq": os.environ.get("GROQ_API_KEY", ""),
        "openrouter": os.environ.get("OPENROUTER_API_KEY", "")}
SUFFIX = "\n\nReturn ONLY one valid JSON object. No markdown code fences, no text before or after it."
RETRY_CODES = (0, 429, 500, 502, 503, 504)
_cache = {}


def has_any_key():
    return any(KEYS.get(p) for p in ORDER)


def _req(url, body=None, headers=None, timeout=120):
    h = {"User-Agent": "seo-autopilot", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")
    except Exception as e:
        return 0, str(e)


def parse_json(text):
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(t, strict=False)
    except Exception:
        a, b = t.find("{"), t.rfind("}")
        if a >= 0 and b > a:
            return json.loads(t[a:b + 1], strict=False)
        raise


def _prefer(ids, prefs, limit):
    ranked = sorted(ids, key=lambda i: next((n for n, p in enumerate(prefs) if p in i.lower()), len(prefs)))
    return ranked[:limit]


def _models(prov):
    if prov in _cache:
        return _cache[prov]
    env = os.environ.get(prov.upper() + "_MODELS", "")
    if env:
        _cache[prov] = [m.strip() for m in env.split(",") if m.strip()]
        return _cache[prov]
    key, out = KEYS[prov], []
    if prov == "gemini":
        code, txt = _req("https://generativelanguage.googleapis.com/v1beta/models?pageSize=100",
                         headers={"x-goog-api-key": key})
        if code == 200:
            bad = ("image", "tts", "live", "audio", "robotics", "embedding", "computer", "learnlm")
            ids = [m["name"].split("/")[-1] for m in json.loads(txt).get("models", [])
                   if "generateContent" in m.get("supportedGenerationMethods", [])
                   and "flash" in m["name"] and not any(b in m["name"] for b in bad)]
            full = sorted([i for i in ids if "lite" not in i], reverse=True)
            lite = sorted([i for i in ids if "lite" in i], reverse=True)
            out = (full[:3] + lite[:1])
        out = out or ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
    elif prov == "groq":
        code, txt = _req("https://api.groq.com/openai/v1/models", headers={"Authorization": "Bearer " + key})
        if code == 200:
            bad = ("whisper", "tts", "guard", "orpheus", "playai", "embed", "compound", "safeguard")
            ids = [m["id"] for m in json.loads(txt).get("data", []) if not any(b in m["id"].lower() for b in bad)]
            out = _prefer(ids, ["gpt-oss-120b", "llama-3.3-70b", "qwen", "llama-4", "kimi", "llama"], 3)
        out = out or ["llama-3.3-70b-versatile"]
    else:
        code, txt = _req("https://openrouter.ai/api/v1/models", headers={"Authorization": "Bearer " + key})
        if code == 200:
            ids = [m["id"] for m in json.loads(txt).get("data", [])
                   if str(m.get("pricing", {}).get("prompt")) == "0" and str(m.get("pricing", {}).get("completion")) == "0"
                   and (m.get("context_length") or 0) >= 8000]
            out = _prefer(ids, ["llama-3.3-70b", "gpt-oss", "qwen", "deepseek", "gemma", "mistral"], 4)
        out = out or ["meta-llama/llama-3.3-70b-instruct:free"]
    _cache[prov] = out
    return out


def _call(prov, model, prompt):
    key = KEYS[prov]
    if prov == "gemini":
        code, txt = _req(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                         {"contents": [{"parts": [{"text": prompt}]}],
                          "generationConfig": {"responseMimeType": "application/json", "temperature": 0.8}},
                         {"x-goog-api-key": key})
        if code == 200:
            try:
                return 200, json.loads(txt)["candidates"][0]["content"]["parts"][0]["text"]
            except Exception:
                return 502, ""
        return code, ""
    base = "https://api.groq.com/openai/v1" if prov == "groq" else "https://openrouter.ai/api/v1"
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.8}
    if prov == "groq":
        body["response_format"] = {"type": "json_object"}
    code, txt = _req(base + "/chat/completions", body, {"Authorization": "Bearer " + key, "X-Title": "SEO Autopilot"})
    if code == 200:
        try:
            return 200, json.loads(txt)["choices"][0]["message"]["content"] or ""
        except Exception:
            return 502, ""
    return code, ""


def generate_json(prompt):
    if not has_any_key():
        raise RuntimeError("No LLM key set (add GEMINI_API_KEY, GROQ_API_KEY or OPENROUTER_API_KEY)")
    errors = []
    for rnd in range(2):  # second round after a pause: demand spikes are usually short
        for prov in ORDER:
            if not KEYS.get(prov):
                continue
            try:
                models = _models(prov)
            except Exception:
                models = []
                errors.append(f"{prov}: model list failed")
            for m in models:
                for attempt in range(2):
                    code, text = _call(prov, m, prompt + SUFFIX)
                    if code == 200:
                        try:
                            return parse_json(text)
                        except Exception:
                            errors.append(f"{prov}/{m}: bad JSON")
                            break
                    errors.append(f"{prov}/{m}: HTTP {code}")
                    if code not in RETRY_CODES:
                        break  # bad key / unknown model: do not retry this one
                    time.sleep(3 + random.random() * 4)
        if rnd == 0:
            time.sleep(45)
    raise RuntimeError("All free LLMs failed: " + " | ".join(errors[-8:]))

# SEO Autopilot v2 (free)

Har ghante khud chalta hai: world trends dhoondta hai, best time par post likhta hai,
Pull Request kholta hai. Dashboard GitHub Pages par.

## Purane setup mein kya badla
Naye files purani files par replace kar do: scripts/run.py, docs/index.html, config.json,
.github/workflows/daily.yml, aur docs/data mein perf.json aur posts.json (nai files).
Purane secrets (GEMINI_API_KEY, TARGET_REPO_TOKEN) wahi rehte hain.

## Naya: Google ka data (rankings, viewers, best time) - free
1. console.cloud.google.com par naya project banao.
2. APIs and Services > Library: "Google Search Console API" Enable karo.
   (Optional, best-time ke liye: "Google Analytics Data API" bhi Enable karo.)
3. IAM and Admin > Service Accounts > Create service account (naam: autopilot).
   Phir us account > Keys > Add key > Create new key > JSON. Ek file download hogi.
4. Us service account ki email copy karo (xxx@xxx.iam.gserviceaccount.com).
5. search.google.com/search-console > apni site > Settings > Users and permissions >
   Add user > wohi email, permission "Restricted" (read only) kafi hai.
   (Site abhi verify nahi hai to pehle Search Console mein add + verify karo.)
6. GA4 (optional): Admin > Property access management > wohi email as Viewer.
   Property ID: Admin > Property details. Isay config.json mein "ga4_property_id" mein likho.
   Site mein GA4 code lagaa hona chahiye.
7. GitHub repo > Settings > Secrets > New secret:
   Name: GOOGLE_SA_JSON, Value: us JSON file ka POORA content (paste).
8. config.json mein "gsc_property" likho:
   - Domain property ho to: sc-domain:stylevoguefashion.com
   - URL-prefix property ho to: https://stylevoguefashion.com/
9. Actions > Autopilot > Run workflow (manual run = full report + post abhi).

## Config options (config.json, har site ke liye)
- posts_per_day: roz kitni posts (default 1)
- topic_mix: fashion aur news ka ratio, e.g. {"fashion":0.6,"news":0.4}
- auto_merge_types: [] = har post review ke baad merge. ["fashion"] = fashion posts khud publish.
  News ko auto-merge mat karo, galat fact ka risk zyada hai.
- default_peak_hours_utc: GA4 na ho to ye ghante use hote hain
- ga4_utc_offset: GA4 property ka time zone (Pakistan = 5)
- regions: kaunse mulkon ke trends dekhne hain (top level mein)

## Roz ka kaam
Dashboard dekho, "Review drafts" se post padho, facts check karo, Merge = publish.

## Dhyan
- Search Console ka data 2-3 din late aata hai, aur nayi post ko rank hone mein hafte lagte hain.
  Learning shuru mein slow hogi, 3-4 hafte baad behtar.
- "Learning" ka matlab: system apni posts ke results dekh kar agli topics chunta hai.
  Koi AI model train nahi hota.
- Viral ki guarantee koi nahi de sakta.

## Update 3: celebrities, hot products, auto-removal
- Content types (config "topic_mix"): fashion, products, celebrity, news.
  - celebrity: trending actors/models, sirf PUBLIC kaam aur style (projects, awards, red carpet).
    Personal life, rishte, gossip, afwahein, sehat, legal baatein nahi likhi jati (defamation aur AdSense risk).
  - products: trending product categories ka shopping guide. Prices aur "best seller #1" ke claims nahi hote.
    Affiliate links lagao to config mein "affiliate_disclosure" likho, wo post ke upar lag jayegi.
- Auto-removal ("prune_after_days", default 7): har roz check karta hai. Agar post 7+ din purani hai, Search Console
  mein 0 clicks, 3 se kam impressions, aur (GA4 connected ho to) 0 views, to ek "Remove N low-performing posts"
  Pull Request kholta hai. Post tabhi delete hoti hai jab tum wo PR merge karo.
  - Search Console connected na ho to kuch bhi delete nahi hota.
  - Data 2-3 din late aata hai aur nayi posts ko index hone mein hafte lag sakte hain. 7 din bohot jaldi hai,
    21-30 din behtar hai. Badalne ke liye "prune_after_days" ka number badlo (0 = band).
  - Delete ki hui page par purana link 404 dega. Post wapas chahiye to git history se restore ho jati hai.

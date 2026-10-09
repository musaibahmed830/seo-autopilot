# SEO Autopilot (free)

Roz khud: site audit + speed check + 1 draft post (Gemini free) -> draft Pull Request tumhari site repo mein.
Dashboard: GitHub Pages par.

## Setup (ek baar)
1. GitHub par naya repo banao: `seo-autopilot` (Public rakho, taake Pages free chale. Is repo mein koi secret nahi hota).
2. Is zip ki files upload karo (Add file > Upload files). `.github/workflows/daily.yml` ko "Create new file" se banao: naam mein `.github/workflows/daily.yml` likho aur content paste karo.
3. `config.json` kholo aur apni site ka repo link + site URL likho. Nayi site add karni ho to sites list mein ek aur block daal do.
4. Secrets (Settings > Secrets and variables > Actions > New repository secret):
   - `GEMINI_API_KEY`: aistudio.google.com se free key
   - `TARGET_REPO_TOKEN`: GitHub > Settings > Developer settings > Personal access tokens > Fine-grained tokens. Sirf apni site wali repo select karo. Permissions: Contents = Read and write, Pull requests = Read and write.
   - `PSI_API_KEY` (optional): Google PageSpeed key, na ho to bhi chalta hai.
5. Settings > Actions > General > Workflow permissions: "Read and write permissions" select karo.
6. Settings > Pages > Source: Deploy from a branch, Branch: main, folder: /docs. Save.
7. Actions tab > "Daily autopilot" > Run workflow. 3-5 minute baad dashboard:
   https://TUMHARA-USERNAME.github.io/seo-autopilot/

## Roz ka kaam
Dashboard kholo, "Review drafts" dabao, post padho, theek karo, Merge karo = site par publish.

## Dhyan
- Post file `content/posts/slug.mdx` banti hai (frontmatter: title, description, category, date). Agar tumhari site ka format alag hai to `render()` in scripts/run.py badlo.
- 3 se zyada drafts review ke intezar mein hon to naya draft nahi banta.
- AI ki likhi post ko bina padhe merge mat karo, galat facts site ko nuqsan dete hain.

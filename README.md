# Macro & Markets Dashboard — live web app (Netlify)

Auto-updating, cloud-hosted version of the dashboard. A GitHub Action re-runs
`dashboard.py` every ~15 minutes and deploys the result to Netlify, so anyone
with the link always sees current data — no laptop required.

Your friend just opens the URL. On iPhone: **Share → Add to Home Screen**.
On desktop Chrome/Edge: the **Install** icon in the address bar. Either gives a
standalone app window with the bar-chart icon.

---

## One-time setup (~15 min, you do this once)

### 1. Put this folder on GitHub
```
cd netlify
git init && git add -A && git commit -m "Macro dashboard"
# create an EMPTY repo on github.com (no README), then:
git branch -M main
git remote add origin https://github.com/<you>/macro-dashboard.git
git push -u origin main
```

### 2. Create the Netlify site
1. Sign in at https://app.netlify.com (free; GitHub login is easiest).
2. **Add new site → Deploy manually**, then drag the `public/` folder in once
   just to create the site (it'll show a placeholder until the first Action run).
3. Open **Site configuration → General → Site details** and copy the **Site ID**
   (a.k.a. API ID).
4. Get a personal token: **User settings → Applications →
   Personal access tokens → New access token**. Copy it.

### 3. Add the two secrets to GitHub
In your GitHub repo: **Settings → Secrets and variables → Actions → New
repository secret**. Add:
- `NETLIFY_AUTH_TOKEN` = the token from step 2.4
- `NETLIFY_SITE_ID` = the Site ID from step 2.3

### 4. Kick off the first deploy
GitHub repo → **Actions → Refresh & deploy dashboard → Run workflow**.
After ~1 min your Netlify URL shows the live dashboard. It then refreshes itself
every ~15 min. Done — share the Netlify URL.

> Tip: rename the site under Netlify **Site configuration → Change site name**
> to get a tidy URL like `https://yourname-macro.netlify.app`.

---

## How it works
- `dashboard.py` — the generator (Python standard library only, no API keys).
- `.github/workflows/refresh.yml` — cron (`*/15`) → runs the script → deploys `public/`.
- `public/` — `manifest.webmanifest` + icons (static); `index.html` is generated each run.
- `netlify.toml` — tells Netlify to just serve `public/`; disables HTML caching.

## Notes / caveats
- **Cron timing:** GitHub's free scheduler can lag a few minutes under load, so
  "15 min" is approximate. Fine for macro data.
- **Trading Economics from the cloud:** TE provides the analyst *estimates* and
  the release/meeting *dates*. If TE ever blocks GitHub's datacenter IPs, those
  bits degrade gracefully (no "est", no "Next:" date) — the core market/FRED data
  still renders. Re-running the workflow usually clears a transient block.
- **Simpler alternative:** if you'd rather not deal with Netlify tokens, the same
  Action can publish to **GitHub Pages** instead (no third-party account). Ask and
  I'll swap the deploy step.

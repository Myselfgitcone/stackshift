# Deploying StackShift

Architecture:
- **Railway** hosts the **backend API** (FastAPI + LLM + PDF/DOCX export).
- **Vercel** hosts the **frontend website** (static `frontend/index.html`), which calls the Railway API.

Deploy order matters (each side needs the other's URL): **GitHub → Railway → Vercel → set CORS**.

---

## 1. Push to GitHub

Create an empty repo on github.com (e.g. `stackshift`), then:

```bash
git remote add origin https://github.com/<you>/stackshift.git
git push -u origin main
```

(Already committed locally on `main`.)

---

## 2. Railway — backend API

1. railway.app → **New Project → Deploy from GitHub repo** → pick `stackshift`.
2. Railway auto-detects Python (Nixpacks) and uses `Procfile` / `railway.json`. No config needed.
3. Deploy → copy the public URL, e.g. `https://stackshift-api.up.railway.app`.
4. Test: open `<railway-url>/api/health` → should return `{"status":"ok"}`.

**Env vars (Railway → Variables):**
- `ALLOWED_ORIGINS` = your Vercel URL (set this in step 4, after Vercel is live). Comma-separated for multiple. `*` allows all (fine, since API keys are entered per-request in the UI and never stored).
- *Optional* server-side default keys (only if you want the app usable without users pasting a key): `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `KIMI_API_KEY`.

---

## 3. Vercel — frontend website

1. **First** point the frontend at your Railway API: edit `frontend/index.html`:
   ```html
   <script>window.STACKSHIFT_API_BASE = "https://stackshift-api.up.railway.app";</script>
   ```
   Commit + push.
2. vercel.com → **New Project** → import the same GitHub repo.
3. Settings:
   - **Root Directory** = `frontend`
   - **Framework Preset** = Other
   - **Build Command** = (leave empty) · **Output Directory** = (leave empty)
4. Deploy → copy the URL, e.g. `https://stackshift.vercel.app`.

---

## 4. Wire CORS

Back in **Railway → Variables**, set:
```
ALLOWED_ORIGINS = https://stackshift.vercel.app
```
Redeploy. Now the Vercel site can call the Railway API.

Open your Vercel URL → Provider tab → paste a key → tailor. Done.

---

## Notes

- **Fonts on Railway (Linux):** no Arial installed → PDF falls back to Helvetica (still clean, no embed). To keep Arial exact, drop `Arial.ttf` + `Arial-Bold.ttf` (or `LiberationSans-Regular.ttf` + `LiberationSans-Bold.ttf`) into `backend/fonts/` and push — the exporter auto-detects them.
- **Local dev** still works unchanged: `window.STACKSHIFT_API_BASE = ""` (empty) = same-origin, and the backend serves the frontend at `http://localhost:8000`.
- **Keys are never stored** — entered per-request in the browser, sent to the API for that call only.

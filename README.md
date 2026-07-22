# REZ Tailor

Industry-agnostic resume customizer. Upload a resume + paste a job description →
get a tailored, side-by-side rewrite that adapts language, terminology, and
metrics to *your* field (Healthcare, Manufacturing, Cybersecurity, DevOps,
Finance, Software, …).

## What it does

| Module | Behavior |
|--------|----------|
| **A — Keyword Gap Filler + STAR** | Detects JD keywords missing from your resume, injects them into Skills, and adds **exactly one** metric-driven STAR bullet to your most recent job, phrased in your industry's language. |
| **B — First-Job Tech Substitution** | Rewrites the *oldest* job to use the JD's preferred tech/methodology. Optional toggle rewrites AWS/Azure/on-prem → **GCP** equivalents. |
| **C — Strict Length Enforcement** | Summary 5–6 · Job 1: 9–12 · Job 2: 7–9 · Job 3: 6–8 · Job 4+: 3–4 bullets. Merges/expands to hit the ranges without dropping history. |

## Stack

- **Backend:** Python FastAPI + OpenAI SDK (OpenAI-compatible — works with Azure / OpenRouter / local models via `OPENAI_BASE_URL`).
- **Frontend:** single static HTML page, Tailwind (CDN) + `marked` for markdown rendering.
- **Parsing:** PDF (`pdfplumber`), DOCX (`python-docx`), TXT/MD.

## Run locally

```bash
# 1. install
python -m venv .venv
.venv\Scripts\activate            # Windows PowerShell:  .venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2. configure
copy .env.example .env            # then edit .env and add OPENAI_API_KEY

# 3. launch
uvicorn backend.main:app --reload --port 8000
```

Open <http://localhost:8000>.

## Pipeline

```
upload resume + JD
   -> detect industry/context (JSON)
   -> keyword gap analysis (present vs missing)
   -> tailoring transform (Modules A/B/C)
   -> side-by-side original vs tailored + copy/download
```

## Project layout

```
backend/
  main.py      FastAPI app + /api/tailor pipeline
  prompts.py   system prompts (detect, gap, tailor) with the strict counts
  llm.py       OpenAI client wrapper (chat / chat_json)
  parsing.py   PDF/DOCX/TXT text extraction
frontend/
  index.html   Tailwind UI, toggles, warning banner, side-by-side view
requirements.txt
.env.example
```

## Notes

- **Review Mode warning is intentional.** Modules A & B fabricate plausible
  technical detail (that's the product). Always fact-check before sending.
- Scanned/image-only PDFs won't extract — use a text-based resume.
- Model default is `gpt-4o-mini`; set `REZ_MODEL` for a stronger model.

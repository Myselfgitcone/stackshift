"""
REZ Tailor — FastAPI backend.

Pipeline:
  1. Upload resume (PDF/DOCX/TXT) + paste JD.
  2. Detect industry & context.
  3. Analyze keyword gaps.
  4. Run the tailoring transformation (Modules A/B/C).
  5. Return original + tailored markdown for side-by-side display.

Run:
  uvicorn backend.main:app --reload --port 8000
Then open http://localhost:8000
"""
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()  # read .env before importing modules that read os.getenv

from backend import exporters, llm, prompts  # noqa: E402
from backend.parsing import extract_text  # noqa: E402

app = FastAPI(title="StackShift", version="1.0.0")

# CORS — allow the separately-hosted frontend (Vercel) to call this API.
# Set ALLOWED_ORIGINS in Railway to your Vercel URL(s), comma-separated; "*" allows all.
_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

MAX_BYTES = 5 * 1024 * 1024  # 5 MB upload cap


class TailorResult(BaseModel):
    context: dict
    present_keywords: list
    missing_keywords: list
    original_markdown: str
    tailored_markdown: str
    scores: dict = {}
    toggles: dict


import re as _re  # noqa: E402

_CLOUD_SIG = {
    "AWS": ("aws", "amazon web", "redshift", "cloudformation", " emr", " s3", "ec2", "lambda"),
    "Azure": ("azure", "synapse", "adls", "data factory", "event hubs"),
    "GCP": ("gcp", "google cloud", "bigquery", "dataproc", "dataflow", "pub/sub"),
}


def _detect_cloud(text: str):
    tl = text.lower()
    for cloud, sigs in _CLOUD_SIG.items():
        if any(s in tl for s in sigs):
            return cloud
    return None


def _split_jobs(text: str):
    """Yield (company_first_word_lower, body) per '@ Company' job header."""
    out, company, buf = [], None, []
    for ln in text.splitlines():
        m = _re.search(r"@\s*([A-Za-z0-9&.\-]+)", ln)
        if m and _re.search(r"@\s*[A-Z]", ln):
            if company is not None:
                out.append((company, "\n".join(buf)))
            company, buf = m.group(1).strip().lower(), [ln]
        elif company is not None:
            buf.append(ln)
    if company is not None:
        out.append((company, "\n".join(buf)))
    return out


def _preserve_native_clouds(tailored: str, base_resume: str, target: str, cloud_swap: bool) -> str:
    """Deterministic guard: a job that was NOT cloud-swapped must keep the real
    cloud it used in the base resume. If the tailored job's Technologies Used line
    dropped it, add it back."""
    base_cloud = {c: _detect_cloud(b) for c, b in _split_jobs(base_resume)}
    if not base_cloud:
        return tailored
    lines = tailored.splitlines()
    company, job_idx = None, 0
    swaps = cloud_swap and target in ("AWS", "Azure", "GCP")
    for i, ln in enumerate(lines):
        m = _re.search(r"@\s*([A-Za-z0-9&.\-]+)", ln)
        if m and _re.search(r"@\s*[A-Z]", ln):
            company, job_idx = m.group(1).strip().lower(), job_idx + 1
            continue
        if company and _re.match(r"\s*\*{0,2}technologies used", ln, _re.I):
            real = base_cloud.get(company)
            if not real:
                continue
            if swaps and job_idx <= 2:          # this job was intentionally swapped
                continue
            low = ln.lower()
            if real.lower() in low or any(s in low for s in _CLOUD_SIG[real]):
                continue                        # cloud already present
            lines[i] = ln.rstrip() + f", {real}"  # re-inject the real cloud
    return "\n".join(lines)

# A "number token": 40%, 3x, 500K, $420K, 2TB, 99.9%, 12, 2B+ ...
_NUM = _re.compile(r"\$?\d[\d,.]*\s*(?:%|x|\+|K|M|B|TB|GB|k|hrs?|hours?|min|days?)?", _re.I)


def _num_count(text: str) -> int:
    return len(_NUM.findall(text))


def _destack_metrics(md: str, cheap_kw: dict) -> str:
    """Find bullets with 2+ numbers, ask the cheap model to reword each to keep
    only ONE number, and splice the rewrites back in place. Bullet count and
    structure never change. Best-effort: on any failure, return md unchanged."""
    lines = md.splitlines()
    targets = [
        i for i, ln in enumerate(lines)
        if ln.lstrip().startswith(("- ", "* ")) and _num_count(ln) >= 2
    ]
    if not targets:
        return md
    payload = "\n".join(f"{n+1}. {lines[i].lstrip()[2:].strip()}" for n, i in enumerate(targets))
    try:
        out = llm.chat(prompts.DESTACK_SYSTEM, payload, temperature=0.2, **cheap_kw).strip()
    except Exception:  # noqa: BLE001
        return md
    fixes = [_re.sub(r"^\s*\d+[.)]\s*", "", ln).strip()
             for ln in out.splitlines() if ln.strip()]
    if len(fixes) != len(targets):
        return md  # mismatch -> don't risk misaligned splicing
    for (i, fix) in zip(targets, fixes):
        indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
        marker = lines[i].lstrip()[:2]  # "- " or "* "
        lines[i] = f"{indent}{marker}{fix}"
    return "\n".join(lines)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/providers")
def providers():
    """Provider metadata so the frontend can render each provider window."""
    return {"providers": llm.provider_meta()}


@app.post("/api/models")
async def models(provider: str = Form(...), api_key: str = Form("")):
    """Live model list pulled from the provider's own API — always current."""
    return llm.list_models(provider, api_key or None)


@app.post("/api/fetch-jd")
async def fetch_jd_endpoint(url: str = Form(...)):
    """Fetch a job description from a URL (LinkedIn guest / readability extract)."""
    from backend import jdfetch

    try:
        return jdfetch.fetch_jd(url)
    except RuntimeError as exc:
        raise HTTPException(422, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(422, f"Could not read that link: {exc}")


@app.post("/api/extract")
async def extract(file: UploadFile = File(...)):
    """Extract plain text from an uploaded PDF/DOCX/TXT so the UI can show it."""
    data = await file.read()
    if not data:
        raise HTTPException(400, "Uploaded file is empty.")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "File exceeds the 5 MB limit.")
    try:
        text = extract_text(file.filename, data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(422, f"Could not read the file: {exc}")
    return {"filename": file.filename, "text": text, "chars": len(text)}


_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@app.post("/api/export/docx")
async def export_docx(markdown: str = Form(...), filename: str = Form("tailored_resume")):
    data = exporters.to_docx(markdown)
    return Response(
        content=data,
        media_type=_DOCX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{filename}.docx"'},
    )


@app.post("/api/export/pdf")
async def export_pdf(markdown: str = Form(...), filename: str = Form("tailored_resume")):
    data = exporters.to_pdf(markdown)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}.pdf"'},
    )


@app.post("/api/tailor", response_model=TailorResult)
async def tailor(
    resume_text: str = Form(...),
    job_description: str = Form(...),
    cloud_swap: bool = Form(False),
):
    # Fixed backend config: Anthropic only. Key from server env (never from client).
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise HTTPException(500, "Server is missing ANTHROPIC_API_KEY.")
    tailor_model = os.getenv("TAILOR_MODEL", "claude-sonnet-4-6")
    cheap_mdl = os.getenv("CHEAP_MODEL", "claude-haiku-4-5-20251001")
    llm_kw = {"provider": "anthropic", "api_key": key, "model": tailor_model}   # Sonnet 4-6
    cheap_kw = {"provider": "anthropic", "api_key": key, "model": cheap_mdl}    # Haiku
    # ---- validate input -----------------------------------------------------
    resume_text = resume_text.strip()
    if len(resume_text) < 40:
        raise HTTPException(
            422, "Resume text is too short — upload a file or paste your resume."
        )
    if len(job_description.strip()) < 40:
        raise HTTPException(400, "Job description is too short. Paste the full posting.")

    # ---- 2. combined analysis: target cloud + tools + gap (cheap model) ----
    analyze_args = (prompts.ANALYZE_SYSTEM, prompts.analyze_prompt(resume_text, job_description))
    try:
        context = llm.chat_json(*analyze_args, **cheap_kw)
    except RuntimeError as exc:  # missing API key etc.
        raise HTTPException(500, str(exc))
    except Exception:  # noqa: BLE001 — cheap model unavailable? fall back to main model
        try:
            context = llm.chat_json(*analyze_args, **llm_kw)
        except RuntimeError as exc:
            raise HTTPException(500, str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"Analysis failed: {exc}")
    if not context:
        context = {
            "target_cloud": "None",
            "target_tools": [],
            "industry": "General",
            "metric_style": "quantified impact",
            "present": [],
            "missing": [],
        }
    # Guard: a cloud swap must only fire when the JD LITERALLY names that cloud.
    # Kills phantom swaps (e.g. GCP invented for a cloud-agnostic Netflix JD).
    _tc = (context.get("target_cloud") or "").strip()
    _jd = job_description.lower()
    _cloud_terms = {"AWS": ("aws", "amazon web"), "Azure": ("azure",), "GCP": ("gcp", "google cloud")}
    if _tc in _cloud_terms and not any(term in _jd for term in _cloud_terms[_tc]):
        context["target_cloud"] = "None"

    present = context.get("present", []) or []
    missing = context.get("missing", []) or []

    # ---- 3. dual-mode cloud transformation ---------------------------------
    tailored = llm.chat(
        prompts.TAILOR_SYSTEM,
        prompts.tailor_prompt(
            resume_text,
            job_description,
            context,
            missing,
            cloud_swap,
        ),
        temperature=0.5,
        **llm_kw,
    ).strip()

    # ---- 3a. deterministic guard: non-swapped jobs keep their real base cloud --
    tailored = _preserve_native_clouds(tailored, resume_text, context.get("target_cloud", "None"), cloud_swap)

    # ---- 3b. QA FIXER (cheap model): tech lines, de-stack numbers, strip junk
    # Comprehensive but safe — must keep the same bullet count, else discarded.
    def _bullets(md: str) -> int:
        return sum(1 for ln in md.splitlines() if ln.lstrip().startswith(("- ", "* ")))

    try:
        fixed = llm.chat(
            prompts.QA_FIXER_SYSTEM,
            prompts.qa_fixer_prompt(tailored),
            temperature=0.2,
            **cheap_kw,
        ).strip()
        if (
            fixed.count("\n") > 5
            and len(fixed) > 0.6 * len(tailored)
            and _bullets(fixed) == _bullets(tailored)   # no bullet lost/added
        ):
            tailored = fixed
        # else: fixer misbehaved -> keep the tailor output as-is
    except Exception:  # noqa: BLE001 — best-effort
        pass

    # ---- 4. final check + three-gate score (ATS / recruiter / hiring manager)
    scores = {}
    try:
        scores = llm.chat_json(
            prompts.SCORE_SYSTEM,
            prompts.score_prompt(job_description, tailored),
            **cheap_kw,
        ) or {}
    except Exception:  # noqa: BLE001 — scoring is best-effort
        scores = {}

    return TailorResult(
        context=context,
        present_keywords=present,
        missing_keywords=missing,
        original_markdown=resume_text,
        tailored_markdown=tailored,
        scores=scores,
        toggles={"cloud_swap": cloud_swap},
    )


# ---- static frontend --------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.exception_handler(RuntimeError)
async def runtime_error_handler(request, exc):
    return JSONResponse(status_code=500, content={"detail": str(exc)})

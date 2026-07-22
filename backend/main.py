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
    toggles: dict


import re as _re  # noqa: E402

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
    provider: str = Form("openai"),
    api_key: str = Form(""),
    model: str = Form(""),
):
    llm_kw = {"provider": provider, "api_key": api_key or None, "model": model or None}
    # Analysis + cleanup are mechanical -> run them on the provider's cheap model.
    cheap_kw = {"provider": provider, "api_key": api_key or None, "model": llm.cheap_model(provider)}
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

    # ---- 4. surgical metric clean-up: reword ONLY over-stacked bullets ------
    # Fixes number-stuffing without ever touching bullet count/structure.
    tailored = _destack_metrics(tailored, cheap_kw)

    return TailorResult(
        context=context,
        present_keywords=present,
        missing_keywords=missing,
        original_markdown=resume_text,
        tailored_markdown=tailored,
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

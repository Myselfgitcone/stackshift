"""
Fetch a job description from a URL.

- LinkedIn: uses the public guest jobPosting endpoint (no login needed).
- Everything else: fetch the page and extract the main job-description text,
  stripping nav/footer/scripts so only the real JD reaches the LLM (token-safe).

Hard timeout so a slow/blocking site fails fast and the UI can fall back to
"paste or upload".
"""
import re

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_TIMEOUT = 12.0          # seconds — fail fast
_MAX_CHARS = 12000       # cap so junk pages can't blow up token usage
_MIN_CHARS = 120         # below this = treat as a failed fetch

_LINKEDIN_ID = re.compile(r"(?:jobs/view/|currentJobId=|/view/[^/]*?-)(\d{6,})", re.I)
_DESC_HINTS = ("description", "job-desc", "jobdesc", "posting", "content", "job-details")


def _clean(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)[:_MAX_CHARS].strip()


def _extract_main(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
        tag.decompose()

    # Prefer a container whose id/class hints at a job description.
    best = None
    for el in soup.find_all(["div", "section", "article", "main"]):
        attr = " ".join(filter(None, [el.get("id", ""), " ".join(el.get("class", []))])).lower()
        if any(h in attr for h in _DESC_HINTS):
            txt = el.get_text("\n")
            if best is None or len(txt) > len(best):
                best = txt
    if best and len(best.strip()) >= _MIN_CHARS:
        return _clean(best)

    # Fallback: the single largest text block on the page.
    blocks = [el.get_text("\n") for el in soup.find_all(["article", "main", "div", "section"])]
    blocks.sort(key=len, reverse=True)
    if blocks:
        return _clean(blocks[0])
    return _clean(soup.get_text("\n"))


def _linkedin_text(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    node = soup.find("div", class_=re.compile("show-more-less-html__markup"))
    if node:
        return _clean(node.get_text("\n"))
    return _extract_main(html)


def fetch_jd(url: str) -> dict:
    """Return {'text', 'source', 'note'}. Raises RuntimeError on any failure so
    the endpoint can return a clean 'paste instead' message."""
    import httpx

    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url

    m = _LINKEDIN_ID.search(url)
    is_linkedin = "linkedin.com" in url.lower()
    headers = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}

    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers=headers) as client:
            if is_linkedin and m:
                guest = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{m.group(1)}"
                r = client.get(guest)
                r.raise_for_status()
                text = _linkedin_text(r.text)
                source = "linkedin"
            else:
                r = client.get(url)
                r.raise_for_status()
                text = _extract_main(r.text)
                source = "linkedin" if is_linkedin else "web"
    except Exception as exc:  # noqa: BLE001 — surface as a clean failure
        raise RuntimeError(f"Could not fetch the link ({exc.__class__.__name__}).")

    if len(text) < _MIN_CHARS:
        raise RuntimeError("Fetched page had no readable job text (login wall or blocked).")
    return {"text": text, "source": source, "note": f"{len(text)} chars fetched"}

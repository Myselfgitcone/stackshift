"""
Resume file parsing. Accepts PDF, DOCX, and plain text uploads and returns
clean text. Kept dependency-light and defensive so a bad upload never 500s.
"""
import io


def _from_pdf(data: bytes) -> str:
    import pdfplumber

    text_parts = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            text_parts.append(page.extract_text() or "")
    return "\n".join(text_parts)


def _from_docx(data: bytes) -> str:
    import docx

    document = docx.Document(io.BytesIO(data))
    return "\n".join(p.text for p in document.paragraphs)


def extract_text(filename: str, data: bytes) -> str:
    """Dispatch on file extension. Returns extracted, stripped text."""
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        text = _from_pdf(data)
    elif name.endswith(".docx"):
        text = _from_docx(data)
    elif name.endswith((".txt", ".md")):
        text = data.decode("utf-8", errors="replace")
    else:
        # Last resort: try to decode as text.
        text = data.decode("utf-8", errors="replace")
    return text.strip()

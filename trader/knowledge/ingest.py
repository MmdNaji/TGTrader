"""Turn a PDF, a web page or pasted text into searchable knowledge chunks."""
from __future__ import annotations

import re
from pathlib import Path

from ..db import Database


def read_pdf(path: Path) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    pages = []
    for p in reader.pages:
        try:
            pages.append(p.extract_text() or "")
        except Exception:
            pages.append("")
    return "\n\n".join(pages)


def read_url(url: str) -> tuple[str, str]:
    import httpx
    from bs4 import BeautifulSoup
    r = httpx.get(url, follow_redirects=True, timeout=30,
                  headers={"User-Agent": "Mozilla/5.0 (TGTrader knowledge import)"})
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
        tag.decompose()
    title = (soup.title.string.strip() if soup.title and soup.title.string else url)[:200]
    main = soup.find("article") or soup.find("main") or soup.body or soup
    text = main.get_text("\n")
    return title, text


def clean_text(text: str) -> str:
    text = text.replace("\r", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(text: str, size: int = 1200, overlap: int = 150) -> list[str]:
    """Split on paragraph boundaries into ~size-character chunks with a little overlap."""
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    cur = ""
    for p in paras:
        if len(cur) + len(p) + 2 <= size:
            cur = f"{cur}\n\n{p}" if cur else p
        else:
            if cur:
                chunks.append(cur)
            while len(p) > size:                # a single huge paragraph
                chunks.append(p[:size])
                p = p[size - overlap:]
            cur = p
    if cur:
        chunks.append(cur)
    return chunks


def ingest_text(db: Database, title: str, text: str, kind: str = "text", origin: str = "") -> tuple[int, str]:
    text = clean_text(text)
    if len(text) < 50:
        raise ValueError("the document has almost no text (a scanned PDF needs OCR first)")
    chunks = chunk_text(text)
    doc_id = db.add_doc(title, kind, origin, chunks)
    return doc_id, text


def ingest_pdf(db: Database, path: str | Path) -> tuple[int, str]:
    p = Path(path)
    return ingest_text(db, p.stem, read_pdf(p), kind="pdf", origin=str(p))


def ingest_url(db: Database, url: str) -> tuple[int, str]:
    title, text = read_url(url)
    return ingest_text(db, title, text, kind="url", origin=url)

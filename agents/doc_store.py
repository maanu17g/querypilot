"""
agents/doc_store.py - Document Knowledge Base
----------------------------------------------
Ingests PDF and plain-text files into a ChromaDB collection.
Supports semantic search so the /ask pipeline can pull relevant
document context when the question cannot be fully answered by SQL.

Supported file types: .pdf, .txt, .md

Design notes:
  - Text is split on paragraph / sentence / word boundaries (never mid-word
    unless a single "word" is longer than a whole chunk).
  - Chunk IDs are deterministic (source + index), so re-uploading a file
    replaces its old chunks instead of duplicating them.
  - Chunks are written to ChromaDB in batches.
  - The embedding model is loaded lazily, not at import time.
  - PDF extraction uses PyMuPDF when installed, and falls back to pypdf.
"""

import os
import re
import hashlib
import chromadb
from chromadb.utils import embedding_functions

_DB_PATH = os.path.join(os.path.dirname(__file__), "..", ".chromadb")
_COLLECTION = "documents"
_CHUNK_SIZE = 800    # max characters per chunk (MiniLM handles ~256 tokens)
_CHUNK_OVERLAP = 100    # characters repeated between neighbouring chunks
_MIN_SCORE = 0.3    # ignore search hits less similar than this
_BATCH_SIZE = 100    # chunks per ChromaDB write

# Split on the largest natural boundary first, then fall back to smaller ones.
_SEPARATORS = ["\n\n", "\n", ". ", "? ", "! ", "; ", ", ", " "]

_EMBED_FN = None


def _get_embed_fn():
    """Load the embedding model on first use (it is slow to import)."""
    global _EMBED_FN
    if _EMBED_FN is None:
        _EMBED_FN = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"
        )
    return _EMBED_FN


class DocStore:
    """
    Manages a ChromaDB collection of document chunks.
    Each document is split into overlapping chunks for better retrieval.
    """

    def __init__(self):
        self._client = chromadb.PersistentClient(path=_DB_PATH)
        self._collection = self._client.get_or_create_collection(
            name=_COLLECTION,
            embedding_function=_get_embed_fn(),
            metadata={"hnsw:space": "cosine"},
        )

    # -- ingest -----------------------------------------------------------

    def ingest_file(self, file_path: str, filename: str) -> dict:
        """
        Read a PDF or text file, chunk it, and store in ChromaDB.
        Re-ingesting a file with the same name replaces its old chunks.
        Returns {"chunks": int, "filename": str, "error": None | str}
        """
        source = os.path.basename(filename)
        ext = os.path.splitext(source)[1].lower()
        try:
            if ext == ".pdf":
                text = _extract_pdf(file_path)
            elif ext in (".txt", ".md"):
                with open(file_path, encoding="utf-8-sig", errors="replace") as f:
                    text = f.read()
            else:
                return {"chunks": 0, "filename": source,
                        "error": f"Unsupported file type: {ext}"}

            chunks = _chunk_text(text, _CHUNK_SIZE, _CHUNK_OVERLAP)
            if not chunks:
                if ext == ".pdf":
                    msg = ("No extractable text found. This looks like a "
                           "scanned or image-only PDF (OCR is not supported).")
                else:
                    msg = "File appears to be empty or unreadable."
                return {"chunks": 0, "filename": source, "error": msg}

            self._store_chunks(chunks, source)
            return {"chunks": len(chunks), "filename": source, "error": None}

        except Exception as exc:
            return {"chunks": 0, "filename": source, "error": str(exc)}

    def ingest_text(self, text: str, source_name: str) -> dict:
        """Ingest raw text directly (for internal notes)."""
        try:
            chunks = _chunk_text(text, _CHUNK_SIZE, _CHUNK_OVERLAP)
            if not chunks:
                return {"chunks": 0, "filename": source_name,
                        "error": "Text is empty."}
            self._store_chunks(chunks, source_name)
            return {"chunks": len(chunks), "filename": source_name,
                    "error": None}
        except Exception as exc:
            return {"chunks": 0, "filename": source_name, "error": str(exc)}

    # -- search -----------------------------------------------------------

    def search(self, query: str, n_results: int = 3,
               min_score: float = _MIN_SCORE) -> list[dict]:
        """
        Return the top-N most relevant document chunks for the query.
        Each result: {"text": str, "source": str, "score": float}
        """
        query = (query or "").strip()
        if not query:
            return []

        count = self._collection.count()
        if count == 0:
            return []

        results = self._collection.query(
            query_texts=[query],
            n_results=min(n_results, count),
        )

        out = []
        seen = set()
        for doc, meta, distance in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            score = round(1 - distance, 3)
            if score < min_score or doc in seen:   # too dissimilar / duplicate
                continue
            seen.add(doc)
            out.append({
                "text":   doc,
                "source": (meta or {}).get("source", "unknown"),
                "score":  score,
            })
        return out

    def list_documents(self) -> list[str]:
        """Return unique source file names that have been ingested."""
        if self._collection.count() == 0:
            return []
        results = self._collection.get(include=["metadatas"])
        sources = {m["source"] for m in results["metadatas"]}
        return sorted(sources)

    def delete_document(self, source: str) -> None:
        """Remove every chunk that came from the given source name."""
        self._collection.delete(where={"source": source})

    def count(self) -> int:
        return self._collection.count()

    # -- internal ---------------------------------------------------------

    def _store_chunks(self, chunks: list[str], source: str) -> None:
        """
        Write chunks in batches using deterministic IDs.
        Chunks left over from an earlier, longer version of the same
        document are removed afterwards.
        """
        total = len(chunks)

        for start in range(0, total, _BATCH_SIZE):
            batch = chunks[start:start + _BATCH_SIZE]
            ids, metadatas = [], []
            for offset in range(len(batch)):
                i = start + offset
                ids.append(hashlib.md5(f"{source}:{i}".encode()).hexdigest())
                metadatas.append({
                    "source":       source,
                    "chunk_index":  i,
                    "total_chunks": total,
                })
            self._collection.upsert(
                ids=ids, documents=batch, metadatas=metadatas
            )

        # Drop stale chunks if the new version is shorter than the old one.
        self._collection.delete(where={
            "$and": [
                {"source": source},
                {"chunk_index": {"$gte": total}},
            ]
        })


# -- helpers --------------------------------------------------------------

def _extract_pdf(file_path: str) -> str:
    """
    Extract text from a PDF, page by page.
    A single unreadable page is skipped instead of failing the whole file.
    Raises ValueError for encrypted PDFs.
    """
    pages = []

    try:
        import fitz   # PyMuPDF
    except ImportError:
        fitz = None

    if fitz is not None:
        with fitz.open(file_path) as doc:
            if doc.needs_pass:
                raise ValueError("PDF is encrypted or password-protected.")
            for page in doc:
                try:
                    text = page.get_text("text")
                except Exception:
                    continue
                if text and text.strip():
                    pages.append(text)
    else:
        from pypdf import PdfReader
        reader = PdfReader(file_path)
        if reader.is_encrypted:
            try:
                ok = reader.decrypt("")
            except Exception:
                ok = 0
            if not ok:
                raise ValueError("PDF is encrypted or password-protected.")
        for page in reader.pages:
            try:
                text = page.extract_text()
            except Exception:
                continue
            if text and text.strip():
                pages.append(text)

    return _clean_pdf_text("\n\n".join(pages))


def _clean_pdf_text(text: str) -> str:
    """Tidy typical PDF extraction noise."""
    # Re-join words split by a hyphen at a line break: "infor-\nmation"
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    # Drop lines that are only a page number
    text = re.sub(r"(?m)^[ \t]*\d{1,4}[ \t]*$\n?", "", text)
    # Single line breaks inside a paragraph become spaces
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    return _normalize(text)


def _normalize(text: str) -> str:
    """Normalise line endings and whitespace."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_recursive(text: str, size: int, seps: list[str]) -> list[str]:
    """
    Break text into pieces no longer than `size`, preferring the largest
    natural separator. Separators stay attached to the piece they end.
    """
    if len(text) <= size:
        return [text]
    if not seps:
        return [text[i:i + size] for i in range(0, len(text), size)]

    sep = seps[0]
    parts = text.split(sep)
    if len(parts) == 1:
        return _split_recursive(text, size, seps[1:])

    pieces = []
    for i, part in enumerate(parts):
        piece = part + (sep if i < len(parts) - 1 else "")
        if not piece:
            continue
        if len(piece) > size:
            pieces.extend(_split_recursive(piece, size, seps[1:]))
        else:
            pieces.append(piece)
    return pieces


def _chunk_text(text: str, size: int = _CHUNK_SIZE,
                overlap: int = _CHUNK_OVERLAP) -> list[str]:
    """
    Split text into chunks of at most `size` characters, breaking at
    paragraph / sentence / word boundaries, with about `overlap`
    characters repeated between neighbouring chunks.
    A document shorter than `size` becomes a single chunk.
    """
    text = _normalize(text)
    if not text:
        return []
    if len(text) <= size:
        return [text]

    pieces = _split_recursive(text, size, _SEPARATORS)

    chunks = []
    current = []
    cur_len = 0

    for piece in pieces:
        if cur_len + len(piece) > size and current:
            chunks.append("".join(current).strip())

            # Carry the last few pieces into the next chunk as overlap.
            carry, carry_len = [], 0
            for p in reversed(current):
                if carry_len + len(p) > overlap:
                    break
                carry.insert(0, p)
                carry_len += len(p)
            if carry_len + len(piece) > size:
                carry, carry_len = [], 0

            current, cur_len = carry, carry_len

        current.append(piece)
        cur_len += len(piece)

    tail = "".join(current).strip()
    if tail:
        chunks.append(tail)

    return [c for c in chunks if c]

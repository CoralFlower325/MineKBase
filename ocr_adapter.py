"""Lazy PaddleOCR adapter for source-material derivation."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path


def _create_engine():
    try:
        from paddleocr import PaddleOCR
    except Exception as exc:
        raise RuntimeError(f"PaddleOCR unavailable: {exc}") from exc
    try:
        return PaddleOCR(use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False)
    except TypeError:
        return PaddleOCR(use_angle_cls=True, lang="ch")


def _ocr_image(path: Path, page_no=None, engine=None):
    engine = engine or _create_engine()
    try:
        result = engine.predict(str(path))
    except AttributeError:
        result = engine.ocr(str(path), cls=True)
    rows = []
    for item in result or []:
        payload = item.json if hasattr(item, "json") else item
        if isinstance(payload, str):
            try: payload = json.loads(payload)
            except ValueError: payload = {}
        if isinstance(payload, dict) and isinstance(payload.get("res"), dict): payload = payload["res"]
        texts = payload.get("rec_texts") if isinstance(payload, dict) else None
        boxes = payload.get("rec_boxes") if isinstance(payload, dict) else None
        if isinstance(texts, list):
            for index, text in enumerate(texts):
                text = str(text or "").strip()
                if text: rows.append({"text": text, "page_no": page_no, "bbox": boxes[index] if isinstance(boxes, list) and index < len(boxes) else None})
            continue
        for line in item if isinstance(item, list) else []:
            for entry in line if isinstance(line, list) else []:
                if not isinstance(entry, (list, tuple)) or len(entry) < 2: continue
                box, pair = entry[0], entry[1]
                text = pair[0] if isinstance(pair, (list, tuple)) else pair
                if str(text or "").strip(): rows.append({"text": str(text).strip(), "page_no": page_no, "bbox": box})
    return rows


def extract_document(path: str | Path, kind="image", pages=None):
    """OCR one image or PDF, initializing one engine per document."""
    path = Path(path)
    engine = _create_engine()
    if kind != "pdf" and path.suffix.lower() != ".pdf": return _ocr_image(path, engine=engine)
    try:
        import pypdfium2 as pdfium
        document = pdfium.PdfDocument(str(path))
        with tempfile.TemporaryDirectory(prefix="ocr-") as directory:
            rows = []
            wanted = set(pages) if pages is not None else set(range(1, len(document) + 1))
            for index in range(len(document)):
                if index + 1 not in wanted: continue
                image_path = Path(directory) / f"page-{index + 1}.png"
                document[index].render(scale=2).to_pil().save(image_path)
                rows.extend(_ocr_image(image_path, index + 1, engine))
        return rows
    except ImportError:
        try: import fitz
        except Exception as exc: raise RuntimeError(f"PDF rasterizer unavailable (pypdfium2/fitz): {exc}") from exc
        document = fitz.open(str(path))
        try:
            with tempfile.TemporaryDirectory(prefix="ocr-") as directory:
                rows = []
                wanted = set(pages) if pages is not None else set(range(1, document.page_count + 1))
                for index in range(document.page_count):
                    if index + 1 not in wanted: continue
                    image_path = Path(directory) / f"page-{index + 1}.png"
                    document.load_page(index).get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).save(str(image_path))
                    rows.extend(_ocr_image(image_path, index + 1, engine))
                return rows
        finally: document.close()

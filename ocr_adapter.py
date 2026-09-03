"""Lazy PaddleOCR adapter for image and scanned-PDF source artifacts.

The module deliberately imports no OCR dependency at import time.  Calling
``extract_document`` is the opt-in enrichment step; unavailable optional
dependencies are reported to the caller without affecting the original file.
"""
from __future__ import annotations

import tempfile
from pathlib import Path


def _ocr_image(path: Path, page_no: int | None = None):
    try:
        from paddleocr import PaddleOCR
    except Exception as exc:
        raise RuntimeError(f"PaddleOCR unavailable: {exc}") from exc

    try:
        engine = PaddleOCR(
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
        result = engine.predict(str(path))
    except TypeError:
        # Older PaddleOCR 3.x builds still expose the stable OCR interface.
        engine = PaddleOCR(use_angle_cls=True, lang="ch")
        result = engine.ocr(str(path), cls=True)

    rows = []
    for item in result or []:
        payload = item.json if hasattr(item, "json") else item
        if isinstance(payload, str):
            import json
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        if isinstance(payload, dict) and isinstance(payload.get("res"), dict):
            payload = payload["res"]
        texts = payload.get("rec_texts") if isinstance(payload, dict) else None
        boxes = payload.get("rec_boxes") if isinstance(payload, dict) else None
        if isinstance(texts, list):
            for index, text in enumerate(texts):
                text = str(text or "").strip()
                if not text:
                    continue
                bbox = boxes[index] if isinstance(boxes, list) and index < len(boxes) else None
                rows.append({"text": text, "page_no": page_no, "bbox": bbox})
            continue
        # Legacy OCR shape: [[[[box], (text, score)], ...]]
        for line in item if isinstance(item, list) else []:
            for entry in line if isinstance(line, list) else []:
                if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                    continue
                box, pair = entry[0], entry[1]
                text = pair[0] if isinstance(pair, (list, tuple)) else pair
                text = str(text or "").strip()
                if text:
                    rows.append({"text": text, "page_no": page_no, "bbox": box})
    return rows


def extract_document(path: str | Path, kind: str = "image"):
    """Return OCR rows with text, page_no and bbox.

    Scanned PDFs are rasterized one page at a time with PyMuPDF when present;
    the OCR engine remains PaddleOCR only.  Missing optional packages raise a
    concise error so the caller can persist ``unavailable``/``error`` state.
    """
    path = Path(path)
    if kind == "pdf" or path.suffix.lower() == ".pdf":
        try:
            import fitz
        except Exception as exc:
            raise RuntimeError(f"PDF rasterizer unavailable: {exc}") from exc
        rows = []
        with tempfile.TemporaryDirectory(prefix="ocr-") as directory:
            document = fitz.open(str(path))
            try:
                for page_index in range(document.page_count):
                    pixmap = document.load_page(page_index).get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                    image_path = Path(directory) / f"page-{page_index + 1}.png"
                    pixmap.save(str(image_path))
                    rows.extend(_ocr_image(image_path, page_index + 1))
            finally:
                document.close()
        return rows
    return _ocr_image(path)

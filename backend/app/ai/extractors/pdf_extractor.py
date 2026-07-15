"""
ai/extractors/pdf_extractor.py
------------------------------
Extracts text from PDF files using PyMuPDF (fitz).
Falls back to Tesseract OCR via Pillow if a page contains no text (e.g., scanned images).
"""

import logging
import re
import io
import fitz  # PyMuPDF
from PIL import Image
import pytesseract

logger = logging.getLogger(__name__)

# Tesseract configuration for better OCR results on documents
_TESSERACT_CONFIG = "--psm 3"

def _clean_text(text: str) -> str:
    """Clean extracted text by removing excessive whitespace."""
    if not text:
        return ""
    # Collapse 3 or more newlines into exactly 2
    text = re.sub(r'\n{3,}', '\n\n', text)
    # Trim leading/trailing whitespace
    return text.strip()

def _extract_text_via_ocr(image: Image.Image) -> str:
    """Extract text from a PIL Image using Tesseract OCR."""
    try:
        # Convert to grayscale to improve OCR
        gray_image = image.convert('L')
        text = pytesseract.image_to_string(gray_image, config=_TESSERACT_CONFIG)
        return text
    except Exception as e:
        logger.warning("OCR failed on image: %s", e)
        return ""

def extract_text_from_pdf(file_path: str, max_chars: int = 50000) -> str:
    """
    Extract text from a PDF file page by page.
    If a page yields no text, renders the page to an image and runs OCR.
    
    Args:
        file_path: Path to the PDF file.
        max_chars: Maximum characters to return (truncates if exceeded).
        
    Returns:
        Cleaned text string.
    """
    extracted_text = []
    total_chars = 0
    
    try:
        doc = fitz.open(file_path)
        for page_num in range(len(doc)):
            page = doc[page_num]
            page_text = page.get_text("text").strip()
            
            if not page_text:
                # Fallback: render page to image and use OCR
                logger.debug("Page %d of %s yielded no text, falling back to OCR.", page_num, file_path)
                pix = page.get_pixmap(dpi=300)
                img_data = pix.tobytes("png")
                img = Image.open(io.BytesIO(img_data))
                page_text = _extract_text_via_ocr(img).strip()
            
            if page_text:
                extracted_text.append(page_text)
                total_chars += len(page_text)
                
                if total_chars >= max_chars:
                    logger.warning("Resume text truncated at %d characters for %s", max_chars, file_path)
                    break
                    
    except Exception as e:
        logger.error("Failed to extract text from PDF %s: %s", file_path, e)
        raise
        
    final_text = "\n\n".join(extracted_text)
    return _clean_text(final_text)[:max_chars]

"""
ai/extractors/__init__.py
-------------------------
Unified interface for extracting text from resume files.
"""

from app.ai.extractors.pdf_extractor import extract_text_from_pdf
from app.ai.extractors.docx_extractor import extract_text_from_docx
from app.core.exceptions import BadRequestError

def extract_text(file_path: str, file_type: str) -> str:
    """
    Extract raw text from a given file based on its type.
    
    Args:
        file_path: Absolute or relative path to the file on disk.
        file_type: String indicating file type ('pdf', 'docx', 'txt', 'image').
        
    Returns:
        A cleaned, single Unicode string of the extracted text.
        
    Raises:
        BadRequestError if the file type is unsupported.
    """
    if file_type == "pdf":
        return extract_text_from_pdf(file_path)
    elif file_type == "docx":
        return extract_text_from_docx(file_path)
    elif file_type == "txt":
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    elif file_type == "image":
        # Usually handled by the PDF fallback, but if direct image support is needed:
        from app.ai.extractors.pdf_extractor import _extract_text_via_ocr
        from PIL import Image
        img = Image.open(file_path)
        return _extract_text_via_ocr(img)
    else:
        raise BadRequestError(f"Unsupported file type for extraction: {file_type}")

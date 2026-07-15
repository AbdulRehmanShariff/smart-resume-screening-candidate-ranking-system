"""
ai/extractors/docx_extractor.py
-------------------------------
Extracts text from DOCX files using python-docx.
"""

import logging
import re
import docx
from docx.document import Document
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import _Cell, Table
from docx.text.paragraph import Paragraph

logger = logging.getLogger(__name__)

def _clean_text(text: str) -> str:
    """Clean extracted text by removing excessive whitespace."""
    if not text:
        return ""
    # Collapse 3 or more newlines into exactly 2
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def iter_block_items(parent):
    """
    Generate a reference to each paragraph and table child within *parent*,
    in document order. Each returned value is an instance of either Table or
    Paragraph. *parent* would most commonly be a docx.document.Document object.
    """
    if isinstance(parent, Document):
        parent_elm = parent.element.body
    elif isinstance(parent, _Cell):
        parent_elm = parent._tc
    else:
        raise ValueError("Unsupported parent type for block iteration")
    
    for child in parent_elm.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            yield Table(child, parent)

def extract_text_from_docx(file_path: str, max_chars: int = 50000) -> str:
    """
    Extract text from a DOCX file.
    Reads paragraphs and tables sequentially to preserve document ordering.
    
    Args:
        file_path: Path to the DOCX file.
        max_chars: Maximum characters to return (truncates if exceeded).
        
    Returns:
        Cleaned text string.
    """
    extracted_text = []
    total_chars = 0
    
    try:
        doc = docx.Document(file_path)
        for block in iter_block_items(doc):
            if isinstance(block, Paragraph):
                text = block.text.strip()
                if text:
                    extracted_text.append(text)
                    total_chars += len(text)
            elif isinstance(block, Table):
                for row in block.rows:
                    row_text = []
                    for cell in row.cells:
                        # Extract text from cell, replacing inner newlines with spaces
                        cell_text = cell.text.strip().replace('\n', ' ')
                        if cell_text:
                            row_text.append(cell_text)
                    if row_text:
                        text = " | ".join(row_text)
                        extracted_text.append(text)
                        total_chars += len(text)
            
            if total_chars >= max_chars:
                logger.warning("Resume text truncated at %d characters for %s", max_chars, file_path)
                break
                
    except Exception as e:
        logger.error("Failed to extract text from DOCX %s: %s", file_path, e)
        raise
        
    final_text = "\n\n".join(extracted_text)
    return _clean_text(final_text)[:max_chars]

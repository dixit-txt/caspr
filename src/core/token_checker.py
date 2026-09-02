from pypdf import PdfReader
import tiktoken

from src.config.constants import TOKEN_CHECKER_MODEL

def count_pdf_tokens(pdf_path, model_name=TOKEN_CHECKER_MODEL):
    # Load tokenizer for the model
    enc = tiktoken.encoding_for_model(model_name)

    # Read PDF
    reader = PdfReader(pdf_path)

    full_text = ""
    for page in reader.pages:
        text = page.extract_text()
        if text:
            full_text += text + "\n"

    # Tokenize
    tokens = enc.encode(full_text)

    return len(tokens), full_text

def check_token_limit(pdf_path, model_name=TOKEN_CHECKER_MODEL):
    token_count, text = count_pdf_tokens(pdf_path, model_name)
    return token_count
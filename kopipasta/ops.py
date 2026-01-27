def sanitize_string(text: str) -> str:
    """
    Ensures the string contains valid unicode code points, fixing surrogate pairs
    that might have been introduced by Windows terminal input.
    """
    try:
        return text.encode("utf-16", "surrogatepass").decode("utf-16")
    except Exception:
        return text


def estimate_tokens(char_count: int) -> int:
    """
    Estimates token count based on character count.
    Code files (with whitespace/syntax) average ~3.6 characters per token.
    """
    return int(char_count / 3.6)


def print_char_count(count: int):
    token_estimate = estimate_tokens(count)
    print(
        f"\rCurrent prompt size: {count} characters (~ {token_estimate} tokens)",
        flush=True,
    )

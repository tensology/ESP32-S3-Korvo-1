"""Dedupe overlapping sliding-window ASR: only emit text not already covered by the previous window."""


def sliding_window_text_delta(prev_window_text: str, new_window_text: str, *, max_prev_words: int = 120) -> str:
    """
    Compare two full transcripts of consecutive (overlapping) audio windows.

    Finds the longest word-sequence suffix of *prev* that matches a prefix of *new*,
    then returns the remainder of *new* (original casing) so the caller can append
    it to a running transcript without repeating the overlapped words.

    Matching is case-insensitive; emitted delta preserves spacing/casing from *new_window_text*.
    """
    prev_st = (prev_window_text or "").strip()
    new_st = (new_window_text or "").strip()
    if not new_st:
        return ""
    if not prev_st:
        return new_st

    prev_lower = prev_st.lower().split()
    if len(prev_lower) > max_prev_words:
        prev_lower = prev_lower[-max_prev_words:]
    new_lower = new_st.lower().split()
    if not new_lower:
        return ""

    max_o = min(len(prev_lower), len(new_lower))
    best = 0
    for o in range(max_o, 0, -1):
        if prev_lower[-o:] == new_lower[:o]:
            best = o
            break

    orig_words = new_st.split()
    if best >= len(orig_words):
        return ""
    return " ".join(orig_words[best:]).strip()

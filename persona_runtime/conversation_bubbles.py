"""Create a desktop delivery plan from an intact private-chat reply."""
from unicodedata import category

from .bubble_limits import MAX_BUBBLE_CHARS
from .chat_display import protected_ranges
from .expression_contract import ProtectedSpan
from .expression_protocol import bubble_plan
from .expression_sources import protected
from .scene_style import PAUSES, STOPS


def frames(body):
    literals = [ProtectedSpan(**value) for value in protected(body)]
    safe = bytearray([1]) * (len(body) + 1)
    for left, right in protected_ranges(body) + [(s.start, s.end) for s in literals]:
        # Long prose in brackets or quotation marks can still break at pauses.
        if right-left > MAX_BUBBLE_CHARS and body[left] in '“「『"（([{':
            continue
        safe[left+1:right] = bytes(max(0, right-left-1))
    regional = 0
    for index, ch in enumerate(body):
        is_regional = 0x1F1E6 <= ord(ch) <= 0x1F1FF
        if index and (ch == '\u200d' or body[index-1] == '\u200d'
                      or category(ch).startswith('M') or 0x1F3FB <= ord(ch) <= 0x1F3FF
                      or (ch == '\n' and body[index-1] == '\r')
                      or (is_regional and regional % 2)):
            safe[index] = 0
        regional = regional+1 if is_regional else 0

    stops = {m.end() for m in STOPS.finditer(body)}
    pauses = {m.end() for m in PAUSES.finditer(body)}
    pieces = []
    start = 0
    closing = '，。！？；：、,.!?;:）)]}」』”'
    while start < len(body):
        limit = min(start + MAX_BUBBLE_CHARS, len(body))
        legal = [i for i in range(start+1, limit+1) if safe[i]]
        lines = [i for i in legal if body[i-1] == '\n' and body[start:i].strip()]
        if lines:
            end = lines[0]
        elif limit == len(body):
            end = limit
        elif legal:
            # Prefer complete utterances, then clauses, then safe text boundaries.
            ends = [i for i in legal if i in stops and i-start >= 8]
            if not ends:
                ends = [i for i in legal if i in pauses and i-start >= 8]
            if ends:
                end = ends[-1]
            else:
                remaining=len(body)-start
                count=(remaining+MAX_BUBBLE_CHARS-1)//MAX_BUBBLE_CHARS
                target=start+(remaining+count-1)//count
                end=min(legal,key=lambda i:abs(i-target))
            if not body[end:].strip().strip(closing).strip():
                alternative=[i for i in legal if i<end and body[i:end].strip().strip(closing).strip()]
                if alternative:
                    end=alternative[-1]
        else:
            end = limit + 1
            while not safe[end]:
                end += 1
        piece=body[start:end]
        if not piece.strip() and end<len(body) and not safe[end+1]:
            # Whitespace immediately before an indivisible literal belongs to that fragment.
            end+=1
            while not safe[end]:
                end+=1
            piece=body[start:end]
        if end<len(body) and not body[end:].strip():
            if len(body)-start<=MAX_BUBBLE_CHARS or len(piece)>MAX_BUBBLE_CHARS:
                end=len(body)
                piece=body[start:end]
        pieces.append(piece)
        start=end
    return bubble_plan(body, literals, pieces)

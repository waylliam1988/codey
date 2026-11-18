"""Local lexical associations with time decay; no model or network calls."""

import re
from datetime import datetime

from codey.memory.contracts import MemoryItem


def tokens(text: str) -> frozenset[str]:
    result: set[str] = set()
    for word in re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", text.casefold()):
        if "\u4e00" <= word[0] <= "\u9fff":
            result.update(word[index:index + 2] for index in range(max(1, len(word) - 1)))
        else:
            result.add(word)
    return frozenset(result)


def relevance(query: str, item: MemoryItem, as_of: str) -> float:
    wanted = tokens(query)
    overlap = len(wanted & tokens(item.text)) / max(1, len(wanted))
    age = max(0.0, (datetime.fromisoformat(as_of) - datetime.fromisoformat(item.created_at)).total_seconds())
    return overlap / (1.0 + age / (30 * 86400))

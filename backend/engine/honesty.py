"""Fake-seer-claim detector for the honesty-rule experiment (report §8.4).

Paper context (arXiv:2408.17177 §4.1): under the honesty rule neither
villagers nor werewolves can convey false public information. The single
highest-impact lie channel observed in our cheap-talk games is the fake
seer claim (悍跳/假跳预言家, including reporting invented check results,
金水/查杀). This detector recognises first-person seer claims so the
engine can reject them for non-seer speakers; the real seer is exempt
(callers must check `player.role != Role.SEER` before using this).

All patterns are anchored on the speaker referring to themselves (我),
so third-person discussion ("预言家说查了3号", "他给5号金水") stays
legal — villagers must still be able to discuss and challenge claims.
"""

from __future__ import annotations

import re

# (pattern, short label used in rejection logs)
_FIRST_PERSON_CLAIM_PATTERNS: tuple[tuple[str, str], ...] = (
    # 我[就/才/真的…]是预言家 — enumerated qualifiers so denials
    # ("我不是预言家") stay legal.
    (r"我(?:就|才|真的?|一直|现在|正式|都|可|肯定)?是[^。，！？!?,\n]{0,2}(?:预言家|先知)", "自称预言家"),
    (r"(?:预言家|先知)[^。，！？!?,\n]{0,4}是我", "自称预言家"),
    (r"我[^。，！？!?,\n]{0,4}(?:起)?跳[^。，！？!?,\n]{0,8}(?:预言家|先知)", "跳预言家"),
    (r"我(?:昨天|昨夜|昨晚|前夜|夜里|第[0-9一二三四五六七八九十]+夜)[^。，！？!?,\n]{0,6}(?:查验|检查|查|验)[了过的到]?", "声称查验"),
    (r"我(?:查验|检查)[了过到]?\s*[0-9一二三四五六七八九十]+\s*号", "声称查验"),
    (r"我[查验][了过的到]?\s*[0-9一二三四五六七八九十]+\s*号", "声称查验"),
    (r"我(?:的)?(?:查验|检查)结果", "声称查验"),
    (r"我(?:给|发|报|出)[^。，！？!?,\n]{0,6}(?:金水|查杀)", "发放金水/查杀"),
    (r"我(?:报|给|发|出)?查杀", "发放金水/查杀"),
    (r"(?:金水|查杀)[^。，！？!?,\n]{0,4}是我(?:给)?的", "发放金水/查杀"),
    (r"我(?:的)(?:金水|查杀)", "发放金水/查杀"),
)

_COMPILED = tuple((re.compile(pattern), label) for pattern, label in _FIRST_PERSON_CLAIM_PATTERNS)

# Subjunctive/hypothetical markers — "如果我是预言家…" is not a claim.
# Strip the hypothetical clause (marker up to the first comma/sentence end)
# before checking; remaining clauses still get checked, so
# "就算我是预言家吧，反正我昨晚查了3号" still trips on the second clause.
_HYPOTHETICAL_CLAUSE = re.compile(
    r"(?:如果|要是|假如|若|就算|即使|万一|好比|比如|譬如说)[^。！？!?,，]*[，,。！？!?]?"
)


def detect_fake_seer_claim(text: str) -> str | None:
    """Return a short label for the first fake-seer-claim pattern found, else None."""
    if not text:
        return None
    if _HYPOTHETICAL_CLAUSE.search(text):
        text = _HYPOTHETICAL_CLAUSE.sub("", text)
    for pattern, label in _COMPILED:
        if pattern.search(text):
            return label
    return None

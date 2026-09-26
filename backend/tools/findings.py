"""
Reads the findings table of an inspection report from its extracted text
(Scenario A quality fix). Used for two things:
  - one knowledge-base query per finding (guided flow), and
  - checking that the drafted note keeps the severity the report states.

OCR (psm 4, see documents.OCR_PAGE_CONFIG) gives one line per table text line.
A row starts on a line that ENDS with a severity word (the Severity column is
the last one); the lines after it, up to the next such line, continue the row:

    +14" | Shell course 2, north Wall thinning to 6.1 mm against 8.0 mm nominal High
    side (24% loss). Below 80% of nominal thickness. Plate
    replacement by welding needed.

Row numbers are often misread ("+14"", "oS", "a"), so they are ignored.

KB queries: one per finding row, plus one per sentence of the Recommendation
section. Measured on the demo reports, the damage descriptions alone rarely
reach agent_tools.MIN_KB_SCORE (report 2: best 0.565); the recommended actions
("hot work permit with gas test", "H2S monitoring and breathing apparatus")
are what the SOPs are about (0.60-0.74).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from shared.contracts import Severity

# ---- named constants (no .env key exists for these)
QUERY_MAX_CHARS = 240              # longest per-finding KB query
MAX_KB_QUERIES = 12                # never more KB searches than this per report
MIN_QUERY_WORDS = 4                # shorter recommendation sentences ("Replace the gasket.") are skipped
MIN_ITEM_MATCH = 0.6               # share of a finding's item words that must appear in a report row
MAX_ITEM_WORDS = 10                # a longer "item" means the observation did not align: keep the model's
ITEM_MAX_SHARE = 0.6               # same: the item cannot be most of the row
CONTINUATION_ITEM_WORDS = 2        # item words on the wrapped line ("side", "mixer MX-104", "P-101A discharge")
FINDING_WORDS = ("thickness", "thinning", "corrosion", "pitting", "crack", "leak", "weep", "defect",
                 "damage", "dent", "erosion", "rust", "h2s", "permit", "blind", "lockout", "sludge")

_SEVERITY_END_RE = re.compile(r"\b(low|medium|high|critical)\b[^A-Za-z0-9]*$", re.IGNORECASE)
_FINDINGS_START_RE = re.compile(r"^\W*(?:\d+\s*\.?\s*)?findings\b", re.IGNORECASE)
_TABLE_END_RE = re.compile(r"^\W*(?:\d+\s*\.?\s*)?(recommendation|conclusion)s?\b|fictional demo document|"
                           r"\bpage\s*\d+\s*of\s*\d+|^---\s*page\s+\d+\s*---$", re.IGNORECASE)
_ROW_NUMBER_RE = re.compile(r"^[^A-Za-z]*(?:\b[a-z]{1,2}\b[^A-Za-z]*)?(?=[A-Z])")
# Lines that are document metadata, never a finding (used when no findings table is found).
_HEADER_RE = re.compile(r"report\s*no|date of|inspector|refinery|section\b|^unit\b|page\s*\d+\s*of|"
                        r"fictional demo|signed|reviewed|^\W*\d+\s*\.?\s*background|^\W*scope", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.;])\s+|\n+")
_WORD_RE = re.compile(r"[a-z0-9]+")
_RECOMMENDATION_RE = re.compile(r"^\W*(?:\d+\s*\.?\s*)?recommendations?\b", re.IGNORECASE)
_RECOMMENDATION_END_RE = re.compile(r"estimated cost|^\W*cost\b|signed|fictional demo|page\s*\d+\s*of|"
                                    r"^\W*\d+\s*\.\s*[A-Z]", re.IGNORECASE)
_CLAUSE_SPLIT_RE = re.compile(r"(?<=[.])\s+|\(\s*[a-h]\s*\)")


@dataclass
class ReportRow:
    """One row of the report's findings table as OCR read it."""
    text: str                      # item + observation, row number and severity removed
    severity: Severity
    lines: list[str] = field(default_factory=list)   # the same text as OCR lines (item column wraps)


def _clean_row_start(line: str) -> str:
    text = _ROW_NUMBER_RE.sub("", line, count=1).strip(" |:;")
    first, _, rest = text.partition(" ")
    if rest and len(first) <= 2 and not first.isupper() and not first.isdigit():
        text = rest.strip(" |:;")               # misread row number like "oS" (for "5")
    return text


def parse_finding_rows(text: str) -> list[ReportRow]:
    """Rows of the first findings table in `text` (empty if there is none)."""
    rows: list[ReportRow] = []
    inside = False
    for raw in (text or "").splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        if not inside:
            inside = bool(_FINDINGS_START_RE.match(line))
            continue
        if _TABLE_END_RE.search(line):
            if rows:
                break
            continue
        match = _SEVERITY_END_RE.search(line)
        if match:
            body = _clean_row_start(line[:match.start()])
            rows.append(ReportRow(text=body, severity=Severity(match.group(1).lower()), lines=[body]))
        elif rows:
            rows[-1].text = f"{rows[-1].text} {line.strip(' |:;')}".strip()
            rows[-1].lines.append(line)
    return rows


def _is_header(line: str) -> bool:
    return bool(_HEADER_RE.search(line))


def _sentence_queries(text: str) -> list[str]:
    """Fallback: damage sentences outside header/title/date lines."""
    body = "\n".join(line for line in (text or "").splitlines() if not _is_header(line))
    sentences = [" ".join(s.split()) for s in _SENTENCE_SPLIT_RE.split(body)]
    return [s for s in sentences if s and any(word in s.lower() for word in FINDING_WORDS)]


def finding_queries(text: str) -> list[str]:
    """One query per finding: its item + observation text from the findings table (or damage sentences)."""
    queries = [row.text for row in parse_finding_rows(text)] or _sentence_queries(text)
    return [q[:QUERY_MAX_CHARS].strip() for q in queries if q.strip()]


def recommendation_queries(text: str) -> list[str]:
    """One query per sentence / (a)(b) clause of the Recommendation section."""
    lines: list[str] = []
    inside = False
    for raw in (text or "").splitlines():
        line = " ".join(raw.split())
        if not inside:
            inside = bool(_RECOMMENDATION_RE.match(line))
            if inside:
                lines.append(_RECOMMENDATION_RE.sub("", line))
            continue
        end = _RECOMMENDATION_END_RE.search(line)
        if end:
            lines.append(line[:end.start()])
            break
        lines.append(line)
    # Keep the sentence's full stop: bge-m3 scores "... breathing apparatus." 0.607 but "... apparatus" 0.569.
    parts = (" ".join(p.split()).strip(" ;:") for p in _CLAUSE_SPLIT_RE.split(" ".join(lines)))
    return [p[:QUERY_MAX_CHARS] for p in parts if len(p.split()) >= MIN_QUERY_WORDS]


def kb_queries(text: str) -> list[str]:
    """Finding queries first, then recommendation queries; unique, at most MAX_KB_QUERIES."""
    unique = dict.fromkeys(finding_queries(text) + recommendation_queries(text))
    return list(unique)[:MAX_KB_QUERIES]


def _words(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall((text or "").lower()) if len(w) > 1 or w.isdigit()}


def item_match_score(item: str, row: ReportRow) -> float:
    """Share of the finding item's words that appear in the report row (0..1)."""
    item_words = _words(item)
    if not item_words:
        return 0.0
    return len(item_words & _words(row.text)) / len(item_words)


_OBSERVATION_START_RE = re.compile(r"^[A-Z][a-z]+(?:[-’'][a-z]+)*[.,;:]?$")   # "Wall", "No", "Erosion-corrosion."


def _key(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", word.lower())


def _line_words(line: str) -> list[str]:
    """Words of one OCR line; runs of dots split words ("flange..Area"). Column bars stay as "|"."""
    return (line or "").replace("..", ". ").split()


def _keep(word: str) -> bool:
    key = _key(word)
    return bool(key) and not (len(key) == 1 and key.isalpha() and word.islower())   # stray OCR "i", "_", "+"


def _first_line_item(line: str) -> Optional[list[str]]:
    """Item words at the start of the row's first line: up to the "|" column bar or the first plain
    capitalised word after the first word ("Shell course 2, north | Wall thinning ...")."""
    words = _line_words(line)
    for i, word in enumerate(words):
        if word == "|" or (i > 0 and _OBSERVATION_START_RE.match(word)):
            return [w for w in words[:i] if _keep(w)]
    return None


def _looks_like_observation(word: str, nxt: Optional[str], observation_bigrams: set[tuple[str, str]]) -> bool:
    return (word[0] in "(0123456789" or word.endswith((".", ";")) or bool(_OBSERVATION_START_RE.match(word))
            or (nxt is not None and (_key(word), _key(nxt)) in observation_bigrams))


def _continuation_item(line: str, observation_bigrams: set[tuple[str, str]]) -> list[str]:
    """Leading item words of the first wrapped line ("side", "mixer MX-104", "P-101A discharge"): at most
    CONTINUATION_ITEM_WORDS, stopping where the line reads like observation text: "(24%", "layer.",
    "Attendant", or two words that are neighbours in the model's observation."""
    words = [w for w in _line_words(line) if _keep(w)]
    item = []
    for i, word in enumerate(words[:CONTINUATION_ITEM_WORDS]):
        if _looks_like_observation(word, words[i + 1] if i + 1 < len(words) else None, observation_bigrams):
            break
        item.append(word)
    return item


def _tidy_item(words: list[str]) -> str:
    text = " ".join(w.rstrip(".;:|") for w in words)
    text = re.sub(r"(?<=[A-Za-z])[‘’](?=[A-Za-z])", " ", text)   # OCR reads a space as ’: "Confined’space"
    return text.strip(" ,")


def row_item(row: ReportRow, observation: str) -> Optional[str]:
    """
    The item column of a report row. OCR interleaves the item and observation columns:

        Shell course 2, north Wall thinning to 6.1 mm against 8.0 mm nominal
        side (24% loss). Below 80% of nominal thickness. Plate

    On the first line the observation starts at the first plain capitalised word ("Wall"); tags
    such as N2, MX-104 or FL-3 are not plain words. On wrapped lines the item part is the leading
    words before the line continues the model's observation ("side"). None if this does not give
    a short, plausible item (the caller then keeps the model's item).
    """
    if not row.lines:
        return None
    words = _first_line_item(row.lines[0])
    if not words:
        return None
    obs_keys = [_key(w) for w in _line_words(observation) if _keep(w)]
    bigrams = set(zip(obs_keys, obs_keys[1:]))
    if len(row.lines) > 1:                  # the item column wraps at most once in these tables
        words += _continuation_item(row.lines[1], bigrams)
    if len(words) > MAX_ITEM_WORDS or len(words) > ITEM_MAX_SHARE * len(_line_words(row.text)):
        return None
    item = _tidy_item(words)
    return item if len(item) >= 3 else None


def match_row(item: str, rows: list[ReportRow]) -> Optional[ReportRow]:
    """The report row a finding's item refers to, or None if nothing matches well enough."""
    scored = [(item_match_score(item, row), -i, row) for i, row in enumerate(rows)]
    if not scored:
        return None
    score, _, row = max(scored, key=lambda s: (s[0], s[1]))
    return row if score >= MIN_ITEM_MATCH else None

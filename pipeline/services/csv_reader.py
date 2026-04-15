import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


TEXT_COLUMNS = (
    "text",
    "feedback",
    "comment",
    "comments",
    "content",
    "review",
    "review_text",
    "body",
    "description",
    "message",
    "conteudo",
    "texto",
)
ID_COLUMNS = ("id", "feedback_id", "source_id", "codigo", "reviewid", "review_id")
TARGET_COLUMNS = ("target", "technical_target", "alvo", "alvo_tecnico")
INTENT_COLUMNS = ("intent", "intention", "intencao")


@dataclass(frozen=True)
class CsvFeedback:
    source_id: str
    text: str
    target: str = ""
    intent: str = ""


def count_rows(path: Path, limit: int | None = None) -> int:
    count = 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(8192)
        handle.seek(0)
        reader = csv.DictReader(handle, dialect=_sniff_dialect(sample))
        for row in reader:
            if not any(row.values()):
                continue
            count += 1
            if limit and count >= limit:
                return limit
    return count


def iter_feedback(path: Path, limit: int | None = None) -> Iterable[CsvFeedback]:
    yielded = 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(8192)
        handle.seek(0)
        reader = csv.DictReader(handle, dialect=_sniff_dialect(sample))
        if not reader.fieldnames:
            return

        normalized = {_normalize_column(name): name for name in reader.fieldnames}
        text_column = _first_existing(normalized, TEXT_COLUMNS)
        if text_column is None:
            available = ", ".join(reader.fieldnames)
            raise ValueError(
                "CSV sem coluna de texto reconhecida. Use uma coluna chamada text, feedback, comment, "
                f"content, review, description ou message. Colunas encontradas: {available}"
            )

        id_column = _first_existing(normalized, ID_COLUMNS)
        target_column = _first_existing(normalized, TARGET_COLUMNS)
        intent_column = _first_existing(normalized, INTENT_COLUMNS)

        for index, row in enumerate(reader, start=1):
            if limit and yielded >= limit:
                return
            if not any(row.values()):
                continue
            text = (row.get(text_column) or "").strip()
            if not text:
                continue
            yielded += 1
            yield CsvFeedback(
                source_id=(row.get(id_column) or str(index)).strip() if id_column else str(index),
                text=text,
                target=(row.get(target_column) or "").strip() if target_column else "",
                intent=(row.get(intent_column) or "").strip() if intent_column else "",
            )


def _sniff_dialect(sample: str):
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        return csv.excel


def _normalize_column(name: str) -> str:
    return (name or "").lower().strip().replace("-", "_").replace(" ", "_")


def _first_existing(columns: dict[str, str], candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if candidate in columns:
            return columns[candidate]
    return None

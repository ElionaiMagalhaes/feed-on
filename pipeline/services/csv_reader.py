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


@dataclass(frozen=True)
class CsvInspection:
    total_rows: int
    valid_rows: int
    empty_rows: int
    missing_text_rows: int
    fieldnames: tuple[str, ...]
    text_column: str
    id_column: str
    target_column: str
    intent_column: str
    delimiter: str
    warnings: tuple[str, ...]


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


def inspect_csv(path: Path, limit: int | None = None) -> CsvInspection:
    total_rows = 0
    valid_rows = 0
    empty_rows = 0
    missing_text_rows = 0
    warnings = []

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(8192)
        handle.seek(0)
        dialect = _sniff_dialect(sample)
        reader = csv.DictReader(handle, dialect=dialect)
        fieldnames = tuple(reader.fieldnames or ())
        normalized = {_normalize_column(name): name for name in fieldnames}
        text_column = _first_existing(normalized, TEXT_COLUMNS)
        id_column = _first_existing(normalized, ID_COLUMNS)
        target_column = _first_existing(normalized, TARGET_COLUMNS)
        intent_column = _first_existing(normalized, INTENT_COLUMNS)

        if text_column is None:
            available = ", ".join(fieldnames)
            raise ValueError(
                "CSV sem coluna de texto reconhecida. Use uma coluna chamada text, feedback, comment, "
                f"content, review, description ou message. Colunas encontradas: {available}"
            )

        if id_column is None:
            warnings.append("Nenhuma coluna de ID reconhecida; o numero da linha sera usado como identificador.")
        if target_column is None:
            warnings.append("Nenhuma coluna de alvo tecnico reconhecida; o alvo sera inferido pelo pipeline.")
        if intent_column is None:
            warnings.append("Nenhuma coluna de intencao reconhecida; a intencao sera inferida pelo pipeline.")

        for row in reader:
            if limit and valid_rows >= limit:
                break
            if not any(row.values()):
                empty_rows += 1
                continue
            total_rows += 1
            text = (row.get(text_column) or "").strip()
            if not text:
                missing_text_rows += 1
                continue
            valid_rows += 1

    if empty_rows:
        warnings.append(f"{empty_rows} linhas vazias foram ignoradas.")
    if missing_text_rows:
        warnings.append(f"{missing_text_rows} linhas sem texto foram ignoradas.")

    return CsvInspection(
        total_rows=total_rows,
        valid_rows=valid_rows,
        empty_rows=empty_rows,
        missing_text_rows=missing_text_rows,
        fieldnames=fieldnames,
        text_column=text_column or "",
        id_column=id_column or "",
        target_column=target_column or "",
        intent_column=intent_column or "",
        delimiter=getattr(dialect, "delimiter", ","),
        warnings=tuple(warnings),
    )


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

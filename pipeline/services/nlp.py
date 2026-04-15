import logging
import re
from dataclasses import dataclass
from typing import Sequence

from django.conf import settings
from openai import OpenAI
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class FeedbackAiSchema(BaseModel):
    intention: str = Field(description="Report ou Suggestion")
    sentiment_score: float = Field(description="Valor entre -1.0 e 1.0")
    target_candidate: str = Field(description="Componente de software mencionado")


class FeedbackAiBatchItem(BaseModel):
    row_number: int = Field(description="Numero sequencial do feedback recebido, iniciando em 1")
    intention: str = Field(description="Report ou Suggestion")
    sentiment_score: float = Field(description="Valor entre -1.0 e 1.0")
    target_candidate: str = Field(description="Componente de software mencionado")


class FeedbackAiBatchSchema(BaseModel):
    items: list[FeedbackAiBatchItem]


@dataclass(frozen=True)
class AiAnalysis:
    sentiment_score: float
    intention: str
    target_candidate: str
    provider: str
    raw: dict


@dataclass(frozen=True)
class NlpResult:
    intent: str
    technical_target: str
    sentiment_score: float | None = None
    ai_intent: str = ""
    target_candidate: str = ""
    ai_provider: str = ""
    ai_raw: dict | None = None


BUG_PATTERNS = (r"\bnao funciona\b", r"\berro\b", r"\bfalha\b", r"\bbug\b", r"\bquebra", r"\btrav")
IMPROVEMENT_PATTERNS = (r"\bseria bom\b", r"\bmelhor", r"\bdeveria\b", r"\badicionar\b", r"\bincluir\b", r"\bfiltro\b")
PRIORITY_PATTERNS = (r"\burgente\b", r"\bprioridade\b", r"\bcritico\b", r"\bimportante\b", r"\bessencial\b")

TARGET_RULES = (
    ("UIElement.Button.Save", (r"\bbotao\b", r"\bsalvar\b")),
    ("UIElement.Button.Submit", (r"\bbotao\b", r"\benviar\b")),
    ("UIElement.Filter.Date", (r"\bfiltro\b", r"\bdata\b")),
    ("UIElement.Search", (r"\bbusca\b", r"\bpesquisa\b")),
    ("Feature.Authentication", (r"\blogin\b", r"\bsenha\b", r"\bautentic")),
    ("Feature.Reporting", (r"\brelatorio\b", r"\bdashboard\b")),
    ("Feature.Checkout", (r"\bpagamento\b", r"\bcheckout\b", r"\bcarrinho\b")),
)


def extract_feedback_semantics(text: str, provided_target: str = "", provided_intent: str = "") -> NlpResult:
    return extract_feedback_semantics_batch([_FeedbackInput(text, provided_target, provided_intent)])[0]


def extract_feedback_semantics_batch(feedbacks: Sequence) -> list[NlpResult]:
    texts_for_ai = []
    indexes_for_ai = []
    analyses: dict[int, AiAnalysis] = {}

    for index, feedback in enumerate(feedbacks):
        provided_intent = getattr(feedback, "intent", "") or ""
        normalized = _normalize(getattr(feedback, "text", ""))
        if provided_intent.strip():
            analyses[index] = _analysis_from_provided_intent(provided_intent.strip(), normalized)
        else:
            indexes_for_ai.append(index)
            texts_for_ai.append(getattr(feedback, "text", ""))

    if texts_for_ai:
        ai_batch = analyze_feedback_batch_with_ai(texts_for_ai)
        for offset, original_index in enumerate(indexes_for_ai):
            normalized = _normalize(getattr(feedbacks[original_index], "text", ""))
            if ai_batch and offset < len(ai_batch):
                analyses[original_index] = ai_batch[offset]
            else:
                analyses[original_index] = _fallback_analysis(normalized)

    results = []
    for index, feedback in enumerate(feedbacks):
        text = getattr(feedback, "text", "")
        provided_target = getattr(feedback, "target", "") or ""
        normalized = _normalize(text)
        ai_analysis = analyses[index]
        technical_target = provided_target.strip() or _target_from_candidate(ai_analysis.target_candidate) or _infer_target(normalized)
        feed_on_intention = map_to_feed_on_intention(ai_analysis.intention, ai_analysis.sentiment_score)
        results.append(
            NlpResult(
                intent=feed_on_intention,
                technical_target=technical_target,
                sentiment_score=ai_analysis.sentiment_score,
                ai_intent=ai_analysis.intention,
                target_candidate=ai_analysis.target_candidate,
                ai_provider=ai_analysis.provider,
                ai_raw=ai_analysis.raw,
            )
        )
    return results


def analyze_feedback_with_ai(text: str) -> AiAnalysis | None:
    batch = analyze_feedback_batch_with_ai([text])
    if not batch:
        return None
    return batch[0]


def analyze_feedback_batch_with_ai(texts: Sequence[str]) -> list[AiAnalysis] | None:
    if not texts or not settings.OPENAI_ENABLE_ANALYSIS or not _has_openai_key():
        return None

    client = OpenAI(api_key=_openai_key(), timeout=settings.OPENAI_TIMEOUT_SECONDS)
    rows = [{"row_number": index, "feedback": text[:2000]} for index, text in enumerate(texts, start=1)]
    try:
        response = client.responses.parse(
            model=settings.OPENAI_MODEL,
            input=[
                {
                    "role": "system",
                    "content": (
                        "Voce e um engenheiro de requisitos de software. Analise cada feedback de usuario "
                        "e retorne um item para cada row_number recebido. A intention deve ser Report quando "
                        "o usuario relata problema, falha, bug ou comportamento observado. A intention deve ser "
                        "Suggestion quando o usuario sugere melhoria, novo recurso ou preferencia. O sentiment_score "
                        "deve variar de -1.0 a 1.0. O target_candidate deve ser o componente de software mencionado, "
                        "como Login, Video Player, Search, Payment, Button ou General."
                    ),
                },
                {"role": "user", "content": str(rows)},
            ],
            text_format=FeedbackAiBatchSchema,
        )
        parsed = response.output_parsed
        by_row = {item.row_number: item for item in parsed.items}
        analyses = []
        for row_number in range(1, len(texts) + 1):
            item = by_row.get(row_number)
            if item is None:
                analyses.append(_fallback_analysis(_normalize(texts[row_number - 1])))
                continue
            analyses.append(
                AiAnalysis(
                    sentiment_score=_clamp_sentiment(item.sentiment_score),
                    intention=_normalize_ai_intention(item.intention),
                    target_candidate=(item.target_candidate or "General").strip() or "General",
                    provider="openai",
                    raw={"model": settings.OPENAI_MODEL, "response_id": response.id, "batch_size": len(texts)},
                )
            )
        return analyses
    except Exception as exc:
        logger.warning("Falha na analise OpenAI em lote; usando fallback local: %s", exc)
        return None


def map_to_feed_on_intention(intention: str, sentiment_score: float) -> str:
    if intention == "Report" and sentiment_score < -0.5:
        return "Intention_BugReport"
    return "Intention_Suggestion"


@dataclass(frozen=True)
class _FeedbackInput:
    text: str
    target: str = ""
    intent: str = ""


def _analysis_from_provided_intent(provided_intent: str, text: str) -> AiAnalysis:
    normalized_intent = provided_intent.strip()
    if normalized_intent in {"Report", "Suggestion"}:
        intention = normalized_intent
    elif normalized_intent in {"Intention_BugReport", "BugReport", "PrioritySignal"}:
        intention = "Report"
    else:
        intention = "Suggestion"
    target_candidate = _display_target(_infer_target(text))
    return AiAnalysis(
        sentiment_score=_fallback_sentiment(text),
        intention=intention,
        target_candidate=target_candidate,
        provider="csv",
        raw={"provided_intent": provided_intent},
    )


def _fallback_analysis(text: str) -> AiAnalysis:
    if _matches_any(text, BUG_PATTERNS) or _matches_any(text, PRIORITY_PATTERNS):
        intention = "Report"
    elif _matches_any(text, IMPROVEMENT_PATTERNS):
        intention = "Suggestion"
    else:
        intention = "Suggestion" if _fallback_sentiment(text) >= 0 else "Report"
    target = _infer_target(text)
    return AiAnalysis(
        sentiment_score=_fallback_sentiment(text),
        intention=intention,
        target_candidate=_display_target(target),
        provider="local",
        raw={},
    )


def _fallback_sentiment(text: str) -> float:
    negative_hits = sum(1 for pattern in BUG_PATTERNS + PRIORITY_PATTERNS if re.search(pattern, text))
    positive_hits = sum(1 for pattern in IMPROVEMENT_PATTERNS if re.search(pattern, text))
    if negative_hits:
        return -0.8
    if positive_hits:
        return 0.4
    return 0.0


def _target_from_candidate(candidate: str) -> str:
    normalized = _normalize(candidate or "")
    if not normalized or normalized == "general":
        return ""
    for target, patterns in TARGET_RULES:
        if any(re.search(pattern, normalized) for pattern in patterns):
            return target
    words = [part for part in re.split(r"[^a-zA-Z0-9]+", candidate) if part]
    if not words:
        return ""
    return "Feature." + "".join(word[:1].upper() + word[1:] for word in words)


def _infer_target(text: str) -> str:
    for target, patterns in TARGET_RULES:
        if any(re.search(pattern, text) for pattern in patterns):
            return target
    return "Feature.General"


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def _normalize_ai_intention(value: object) -> str:
    raw = str(value or "").strip().lower()
    if raw == "report":
        return "Report"
    if raw == "suggestion":
        return "Suggestion"
    return "Suggestion"


def _clamp_sentiment(value: object) -> float:
    try:
        sentiment = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(-1.0, min(1.0, sentiment))


def _has_openai_key() -> bool:
    key = _openai_key()
    return bool(key and key != "sua_chave_aqui" and key.startswith("sk-"))


def _openai_key() -> str:
    return (settings.OPENAI_API_KEY or "").strip().strip("'\"")


def _display_target(target: str) -> str:
    return (target or "Feature.General").split(".")[-1]


def _normalize(text: str) -> str:
    replacements = {
        "ã": "a",
        "á": "a",
        "à": "a",
        "â": "a",
        "é": "e",
        "ê": "e",
        "í": "i",
        "ó": "o",
        "ô": "o",
        "õ": "o",
        "ú": "u",
        "ç": "c",
    }
    lowered = text.lower()
    for source, target in replacements.items():
        lowered = lowered.replace(source, target)
    return lowered

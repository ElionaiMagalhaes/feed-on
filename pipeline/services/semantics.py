import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class ResolvedTarget:
    target_type: str
    target_name: str
    matched_expression: str
    source: str
    confidence: float | None = None


@dataclass(frozen=True)
class DerivedConsequence:
    consequence_type: str
    derivation_rule: str
    confidence: float | None = None


SYSTEM_TERMS = (
    ("botao cancelar", "UIElement", "CancelButton"),
    ("botão cancelar", "UIElement", "CancelButton"),
    ("tela", "UIElement", "Screen"),
    ("botao", "UIElement", "Button"),
    ("botão", "UIElement", "Button"),
    ("menu", "UIElement", "Menu"),
    ("certificados", "Feature", "CertificateManagement"),
    ("certificado", "Feature", "CertificateManagement"),
    ("login", "Feature", "Authentication"),
)

GENERAL_TERMS = (
    ("desempenho", "QualityAttribute", "Performance"),
    ("lento", "QualityAttribute", "Performance"),
    ("seguranca", "QualityAttribute", "Security"),
    ("usabilidade", "QualityAttribute", "Usability"),
)


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Cf")
    value = "".join(ch for ch in unicodedata.normalize("NFKD", value.lower()) if not unicodedata.combining(ch))
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def resolve_targets(technical_target, target_candidate, feedback_text, domain_lexicon=None) -> list[ResolvedTarget]:
    results: list[ResolvedTarget] = []
    seen: set[tuple[str, str]] = set()

    def add(target_type, target_name, expression, source, confidence=None):
        key = (normalize_text(target_type), normalize_text(target_name))
        if key not in seen:
            seen.add(key)
            results.append(ResolvedTarget(target_type, target_name, expression, source, confidence))

    for value, source, confidence in ((technical_target, "csv", 1.0), (target_candidate, "llm_target_candidate", .85)):
        if (value or "").strip():
            for part in re.split(r"[|;,]", value):
                part = part.strip()
                if part:
                    target_type, name = _parse_target(part)
                    add(target_type, name, part, source, confidence)

    search_sources = [(feedback_text, "original_text")]
    terms = list(SYSTEM_TERMS)
    if domain_lexicon is not None:
        for term in domain_lexicon.terms.filter(active=True).order_by("-normalized_expression"):
            terms.append((term.expression, term.target_type, term.canonical_name))
        for field, target_type in (("ui_elements", "UIElement"), ("quality_attributes", "QualityAttribute"), ("requirements", "Requirement"), ("processes", "Process")):
            for expression in re.split(r"[,;\n|]", getattr(domain_lexicon, field, "") or ""):
                if expression.strip():
                    terms.append((expression.strip(), target_type, _canonical(expression)))

    normalized_feedback = normalize_text(feedback_text)
    for expression, target_type, name in sorted(terms, key=lambda item: len(normalize_text(item[0])), reverse=True):
        if _contains(normalized_feedback, normalize_text(expression)):
            add(target_type, name, expression, "system_lexicon" if (expression, target_type, name) in SYSTEM_TERMS else "domain_lexicon", .8)
    for expression, target_type, name in GENERAL_TERMS:
        if _contains(normalized_feedback, normalize_text(expression)):
            add(target_type, name, expression, "general_lexicon", .65)

    if not results:
        add("Feature", "General", "", "fallback", None)
    return results


def derive_consequences(ai_intent, mapped_intention, sentiment_score, feedback_text, resolved_targets, target_frequencies, hotspot_min_count=3, priority_keywords=()) -> list[DerivedConsequence]:
    text = normalize_text(feedback_text)
    intent = normalize_text(f"{ai_intent} {mapped_intention}")
    problem = _has_any(text, ("erro", "falha", "defeito", "trav", "nao funciona", "impossivel", "quebrado", "incorreto"))
    suggestion = _has_any(text, ("sugiro", "deveria", "gostaria", "poderia", "incluir", "adicionar", "melhorar", "nova funcionalidade", "prefer"))
    results = []
    if ("report" in intent and problem) or (problem and sentiment_score is not None and sentiment_score <= -.5) or problem:
        results.append(DerivedConsequence("Correction", "technical_problem", .9))
    if "suggest" in intent or suggestion:
        results.append(DerivedConsequence("Improvement", "suggestion_or_evolution", .85))
    configured = tuple(priority_keywords) or ("urgente", "prioridade", "critico", "imediato")
    keyword = next((item for item in configured if normalize_text(item) in text), None)
    hotspot = any(target_frequencies.get(f"{t.target_type}.{t.target_name}", 0) >= hotspot_min_count for t in resolved_targets)
    if keyword:
        results.append(DerivedConsequence("Prioritization", "explicit_urgency" if normalize_text(keyword) in {"urgente", "imediato"} else "criticality_keyword", .9))
    elif hotspot:
        results.append(DerivedConsequence("Prioritization", "target_hotspot", .75))
    return results or [DerivedConsequence("Improvement", "general_evolution", .5)]


def _parse_target(value):
    parts = re.split(r"[._:]", value, maxsplit=1)
    return (parts[0] or "Feature", parts[1] if len(parts) > 1 and parts[1] else _canonical(value))


def _canonical(value):
    return "".join(word.capitalize() for word in normalize_text(value).split()) or "General"


def _contains(text, expression):
    if not expression:
        return False
    singular = expression[:-1] if expression.endswith("s") else expression
    return bool(re.search(rf"(?<!\w)(?:{re.escape(expression)}|{re.escape(singular)}s?)(?!\w)", text))


def _has_any(text: str, terms: Iterable[str]) -> bool:
    return any(normalize_text(term) in text for term in terms)

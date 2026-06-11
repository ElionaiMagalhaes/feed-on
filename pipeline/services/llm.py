import json
import logging
import re
import unicodedata

from django.conf import settings
from openai import OpenAI
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

CATEGORY_FIELDS = {
    "UIElement": "ui_elements",
    "QualityAttribute": "quality_attributes",
    "Requirement": "requirements",
    "Process": "processes",
}

FALLBACK_DOMAIN_KEYWORDS = {
    "UIElement": [
        "botao",
        "tela",
        "menu",
        "icone",
        "campo",
        "formulario",
        "filtro",
        "busca",
        "interface",
    ],
    "QualityAttribute": [
        "lento",
        "travando",
        "erro",
        "falha",
        "carregamento",
        "usabilidade",
        "desempenho",
        "instabilidade",
    ],
    "Requirement": [
        "login",
        "senha",
        "cadastro",
        "perfil",
        "assinatura",
        "pagamento",
        "relatorio",
    ],
    "Process": [
        "atendimento",
        "suporte",
        "contato",
        "ajuda",
        "reembolso",
        "agendamento",
        "cancelamento",
    ],
}


class DomainKeywordsSchema(BaseModel):
    UIElement: list[str] = Field(default_factory=list)
    QualityAttribute: list[str] = Field(default_factory=list)
    Requirement: list[str] = Field(default_factory=list)
    Process: list[str] = Field(default_factory=list)


def generate_domain_keywords(domain_name: str) -> dict[str, list[str]]:
    domain_name = normalize_domain_name(domain_name)
    if not _has_openai_key():
        return FALLBACK_DOMAIN_KEYWORDS

    client = OpenAI(api_key=_openai_key(), timeout=settings.OPENAI_TIMEOUT_SECONDS)
    try:
        response = client.responses.parse(
            model=settings.OPENAI_MODEL,
            input=[
                {
                    "role": "system",
                    "content": (
                        "Voce e um engenheiro de ontologias e requisitos especialista em adaptar a ontologia "
                        "FEED-ON para diferentes dominios de software. Responda somente com JSON puro, sem markdown."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Dominio do software: {domain_name}. Gere termos e jargoes em portugues do Brasil "
                        "que usuarios comuns usariam em avaliacoes de lojas de aplicativos para criticar ou sugerir "
                        "melhorias nesse tipo de software. Retorne exatamente um objeto JSON com as chaves "
                        "UIElement, QualityAttribute, Requirement e Process. Cada chave deve conter uma lista de "
                        "termos curtos, sem frases longas, sem explicacoes e sem duplicatas."
                    ),
                },
            ],
            text_format=DomainKeywordsSchema,
        )
        return normalize_keywords(response.output_parsed.model_dump())
    except Exception as exc:
        logger.warning("Falha ao gerar lexico de dominio via OpenAI para %s: %s", domain_name, exc)
        return FALLBACK_DOMAIN_KEYWORDS


def normalize_domain_name(value: str) -> str:
    normalized = _strip_accents(value or "geral").strip().lower()
    normalized = re.sub(r"[^a-z0-9_ -]+", "", normalized)
    normalized = re.sub(r"[\s-]+", "_", normalized).strip("_")
    return normalized[:100] or "geral"


def normalize_keywords(raw: dict) -> dict[str, list[str]]:
    result = {}
    for category in CATEGORY_FIELDS:
        values = raw.get(category, []) if isinstance(raw, dict) else []
        if isinstance(values, str):
            values = _loads_string_list(values)
        cleaned = []
        seen = set()
        for value in values or []:
            keyword = _clean_keyword(str(value))
            key = keyword.casefold()
            if keyword and key not in seen:
                seen.add(key)
                cleaned.append(keyword)
        result[category] = cleaned
    return result


def keywords_to_storage(values: list[str]) -> str:
    return json.dumps(values, ensure_ascii=False)


def keywords_from_storage(value: str) -> list[str]:
    value = (value or "").strip()
    if not value:
        return []
    return normalize_keywords({"UIElement": _loads_string_list(value)})["UIElement"]


def merge_keyword_lists(*lists: list[str]) -> list[str]:
    merged = []
    seen = set()
    for values in lists:
        for value in values or []:
            keyword = _clean_keyword(str(value))
            key = keyword.casefold()
            if keyword and key not in seen:
                seen.add(key)
                merged.append(keyword)
    return merged


def _loads_string_list(value: str) -> list[str]:
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    except json.JSONDecodeError:
        pass
    return [item.strip() for item in value.split(",") if item.strip()]


def _clean_keyword(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"\s+", " ", value)
    value = value.strip(" .,;:|/\\")
    return value[:80]


def _has_openai_key() -> bool:
    key = _openai_key()
    return bool(key and key != "sua_chave_aqui" and key.startswith("sk-"))


def _openai_key() -> str:
    return (settings.OPENAI_API_KEY or "").strip().strip("'\"")


def _strip_accents(value: str) -> str:
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")

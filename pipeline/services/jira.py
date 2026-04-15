from dataclasses import dataclass

from django.conf import settings


@dataclass(frozen=True)
class JiraResult:
    key: str
    status: str
    raw: dict


def build_jira_payload(record) -> dict:
    issue_type = _issue_type_for_consequence(record.consequence)
    summary = f"[FEED-ON] {record.consequence or 'Feedback'} - {record.inferred_target or record.technical_target}"
    description = (
        f"Feedback ID: {record.source_id}\n\n"
        f"Texto original:\n{record.text}\n\n"
        f"Intencao extraida: {record.ai_intent or '-'}\n"
        f"Sentimento GPT: {_sentiment_display(record.sentiment_score)}\n"
        f"Intencao FEED-ON: {record.intent}\n"
        f"Alvo candidato IA: {record.target_candidate or '-'}\n"
        f"Alvo tecnico inicial: {record.technical_target}\n"
        f"Alvo inferido: {record.inferred_target}\n"
        f"Consequencia derivada: {record.consequence}\n"
        f"Tipo Jira selecionado: {issue_type}\n"
    )
    return {
        "fields": {
            "project": {"key": settings.JIRA_PROJECT_KEY},
            "summary": summary[:255],
            "description": description,
            "issuetype": {"name": issue_type},
            "labels": ["feed-on", _label(record.consequence), _label(record.intent)],
        }
    }


def create_jira_issue(record) -> JiraResult:
    payload = build_jira_payload(record)

    if settings.JIRA_DRY_RUN:
        return JiraResult(
            key=f"DRY-RUN-{record.job_id}-{record.source_id}",
            status="dry_run",
            raw={"dry_run": True, "payload": payload},
        )

    missing = [
        name
        for name, value in {
            "JIRA_URL": settings.JIRA_URL,
            "JIRA_EMAIL": settings.JIRA_EMAIL,
            "JIRA_API_TOKEN": settings.JIRA_API_TOKEN,
            "JIRA_PROJECT_KEY": settings.JIRA_PROJECT_KEY,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"Credenciais Jira incompletas: {', '.join(missing)}")

    from atlassian import Jira

    jira = Jira(url=settings.JIRA_URL, username=settings.JIRA_EMAIL, password=settings.JIRA_API_TOKEN, cloud=True)
    response = jira.issue_create(fields=payload["fields"])
    key = response.get("key") or response.get("id", "")
    if not key:
        raise RuntimeError(f"Jira nao retornou key: {response}")
    return JiraResult(key=key, status="created", raw=response)


def _issue_type_for_consequence(consequence: str) -> str:
    if consequence == "Correction":
        return settings.JIRA_BUG_ISSUE_TYPE
    if consequence == "Improvement":
        return settings.JIRA_IMPROVEMENT_ISSUE_TYPE
    return settings.JIRA_ISSUE_TYPE


def _sentiment_display(value) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}"


def _label(value: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "-" for char in (value or "unknown"))
    return "-".join(part for part in cleaned.split("-") if part)[:50] or "unknown"

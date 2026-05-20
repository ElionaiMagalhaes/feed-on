from dataclasses import dataclass

from django.conf import settings


@dataclass(frozen=True)
class JiraResult:
    key: str
    status: str
    raw: dict


@dataclass(frozen=True)
class JiraConfig:
    server: str
    email: str
    api_token: str
    project_key: str


def build_jira_payload(record) -> dict:
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
    )
    return {
        "fields": {
            "project": {"key": settings.JIRA_PROJECT_KEY},
            "summary": summary[:255],
            "description": description,
            "labels": ["feed-on", _label(record.consequence), _label(record.intent)],
        }
    }


def create_jira_issue(record, config: JiraConfig | None = None) -> JiraResult:
    payload = build_jira_payload(record)

    if settings.JIRA_DRY_RUN:
        return JiraResult(
            key=f"DRY-RUN-{record.job_id}-{record.source_id}",
            status="dry_run",
            raw={"dry_run": True, "payload": payload},
        )

    config = _resolve_config(config)
    _validate_jira_config(config)
    jira = _jira_client(config)
    response = _create_issue_in_backlog(jira, payload["fields"])
    key = getattr(response, "key", "") or getattr(response, "id", "")
    if not key:
        raise RuntimeError(f"Jira nao retornou key: {response}")
    return JiraResult(key=key, status="created", raw={"key": key, "payload": payload})


def criar_ticket_jira(texto_feedback: str, classe_alvo: str, severidade: str, config: JiraConfig | None = None) -> str:
    config = _resolve_config(config)
    fields = {
        "project": {"key": config.project_key},
        "summary": f"[{classe_alvo}] Feedback do Usuario"[:255],
        "description": _manual_issue_description(texto_feedback, classe_alvo, severidade),
    }

    if settings.JIRA_DRY_RUN:
        return f"DRY-RUN-{config.project_key}-{_label(classe_alvo).upper()}"

    _validate_jira_config(config)
    jira = _jira_client(config)
    issue = _create_issue_in_backlog(jira, fields)
    jira_key = getattr(issue, "key", "")
    if not jira_key:
        raise RuntimeError(f"Jira nao retornou key: {issue}")
    return jira_key


def testar_comunicacao_jira(config: JiraConfig) -> dict:
    _validate_jira_config(config)
    jira = _jira_client(config)
    server_info = jira.server_info()
    issue_types = _project_issue_types_for_key(jira, config.project_key)
    if not issue_types:
        raise RuntimeError(
            f"Comunicacao com o Jira estabelecida, mas o projeto '{config.project_key}' nao retornou tipos criaveis. "
            "Verifique a chave do projeto e as permissoes Browse Projects/Create Issues."
        )
    return {
        "server_title": server_info.get("serverTitle", config.server) if isinstance(server_info, dict) else config.server,
        "project_key": config.project_key,
        "issue_types": [_issue_type_value(issue_type, "name") for issue_type in issue_types],
    }


def _jira_client(config: JiraConfig):
    from jira import JIRA

    return JIRA(server=config.server, basic_auth=(config.email, config.api_token))


def settings_jira_config() -> JiraConfig:
    return JiraConfig(
        server=settings.JIRA_SERVER,
        email=settings.JIRA_EMAIL,
        api_token=settings.JIRA_API_TOKEN,
        project_key=settings.JIRA_PROJECT_KEY,
    )


def _resolve_config(config: JiraConfig | None = None) -> JiraConfig:
    return config or settings_jira_config()


def _validate_jira_config(config: JiraConfig) -> None:
    missing = [
        name
        for name, value in {
            "JIRA_SERVER": config.server,
            "JIRA_EMAIL": config.email,
            "JIRA_API_TOKEN": config.api_token,
            "JIRA_PROJECT_KEY": config.project_key,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"Credenciais Jira incompletas: {', '.join(missing)}")


def _default_issue_type_field(jira, project_key: str | None = None) -> dict:
    issue_types = _project_issue_types_for_key(jira, project_key or settings.JIRA_PROJECT_KEY or "FEED")
    if not issue_types:
        return {"name": _default_issue_type_names()[0]}

    by_name = {_issue_type_value(issue_type, "name").strip().casefold(): issue_type for issue_type in issue_types}
    for name in _default_issue_type_names():
        issue_type = by_name.get(name.casefold())
        if issue_type is not None:
            return _issue_type_payload(issue_type)

    non_subtasks = [issue_type for issue_type in issue_types if not _issue_type_is_subtask(issue_type)]
    return _issue_type_payload(non_subtasks[0] if non_subtasks else issue_types[0])


def _create_issue_in_backlog(jira, fields: dict):
    attempts = []
    project_key = (fields.get("project") or {}).get("key") or settings.JIRA_PROJECT_KEY or "FEED"

    candidate_fields = dict(fields)
    candidate_fields["issuetype"] = _default_issue_type_field(jira, project_key)
    attempts.append(candidate_fields)

    for name in _default_issue_type_names():
        candidate_fields = dict(fields)
        candidate_fields["issuetype"] = {"name": name}
        attempts.append(candidate_fields)

    last_error = None
    seen = set()
    for candidate_fields in attempts:
        issue_type_key = tuple(sorted(candidate_fields["issuetype"].items()))
        if issue_type_key in seen:
            continue
        seen.add(issue_type_key)
        try:
            return jira.create_issue(fields=candidate_fields)
        except Exception as exc:
            last_error = exc
            if "issuetype" not in str(exc).lower() and "issue type" not in str(exc).lower():
                raise

    raise RuntimeError(
        "Nao foi possivel criar a tarefa no backlog com nenhum tipo tecnico aceito pelo Jira. "
        f"Tipos tentados: {', '.join(_default_issue_type_names())}. Ultimo erro: {last_error}"
    )


def _project_issue_types(jira) -> list:
    project_key = settings.JIRA_PROJECT_KEY or "FEED"
    return _project_issue_types_for_key(jira, project_key)


def _project_issue_types_for_key(jira, project_key: str) -> list:
    try:
        metadata = jira.createmeta(projectKeys=project_key, expand="projects.issuetypes")
        projects = metadata.get("projects", []) if isinstance(metadata, dict) else []
        for project in projects:
            if str(project.get("key", "")).casefold() == project_key.casefold():
                return list(project.get("issuetypes", []) or [])
        if projects:
            return list(projects[0].get("issuetypes", []) or [])
    except Exception:
        pass

    try:
        project = jira.project(project_key)
        return list(getattr(project, "issueTypes", []) or [])
    except Exception:
        pass

    try:
        projects = list(jira.projects() or [])
        if len(projects) == 1:
            project = jira.project(projects[0].key)
            return list(getattr(project, "issueTypes", []) or [])
    except Exception:
        pass

    return []


def _default_issue_type_names() -> list[str]:
    names = [
        settings.JIRA_ISSUE_TYPE,
        settings.JIRA_IMPROVEMENT_ISSUE_TYPE,
        "Task",
        "Tarefa",
        "Story",
        "Historia",
        "História",
        "Epic",
        "Bug",
        "Backlog",
    ]
    deduped = []
    for name in names:
        if name and name not in deduped:
            deduped.append(name)
    return deduped


def _issue_type_payload(issue_type) -> dict:
    issue_type_id = _issue_type_value(issue_type, "id")
    if issue_type_id:
        return {"id": issue_type_id}
    issue_type_name = _issue_type_value(issue_type, "name")
    if issue_type_name:
        return {"name": issue_type_name}
    raise RuntimeError("O Jira retornou um tipo de issue sem id nem nome.")


def _issue_type_value(issue_type, key: str) -> str:
    if isinstance(issue_type, dict):
        return str(issue_type.get(key) or "")
    return str(getattr(issue_type, key, "") or "")


def _issue_type_is_subtask(issue_type) -> bool:
    if isinstance(issue_type, dict):
        return bool(issue_type.get("subtask"))
    return bool(getattr(issue_type, "subtask", False))


def _manual_issue_description(texto_feedback: str, classe_alvo: str, severidade: str) -> str:
    return (
        f"Feedback do usuario:\n{texto_feedback}\n\n"
        "Classificacao FEED-ON:\n"
        f"- Alvo inferido: {classe_alvo or '-'}\n"
        f"- Consequencia: {severidade or '-'}\n\n"
        "Observacao: item criado no backlog para triagem manual de tipo, prioridade e planejamento no Jira."
    )


def _sentiment_display(value) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}"


def _label(value: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "-" for char in (value or "unknown"))
    return "-".join(part for part in cleaned.split("-") if part)[:50] or "unknown"

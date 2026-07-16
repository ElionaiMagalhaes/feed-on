import logging
import re
import unicodedata
import uuid
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.conf import settings

from pipeline.models import DomainLexicon
from pipeline.services.llm import CATEGORY_FIELDS, keywords_from_storage, normalize_domain_name

logger = logging.getLogger(__name__)


class XsdFloat(float):
    """Python value serialized explicitly as xsd:float by Owlready2."""


def _register_xsd_float_datatype() -> None:
    from owlready2 import declare_datatype

    declare_datatype(
        XsdFloat,
        "http://www.w3.org/2001/XMLSchema#float",
        XsdFloat,
        lambda value: format(float(value), ".9g"),
    )

TARGET_PARENT_RULES = {
    "UIElement.Button.Save": "Feature.Persistence",
    "UIElement.Button.Submit": "Feature.FormSubmission",
    "UIElement.Filter.Date": "Feature.SearchAndFiltering",
    "UIElement.Search": "Feature.SearchAndFiltering",
}
INTENTION_CLASS_MARKERS = {"Intention_BugReport", "Intention_Suggestion", "BugReport", "Suggestion"}
FUNCTIONAL_DEBUG_PROPERTIES = {"hasIntention", "commentText", "sentimentScore", "jiraKey"}
FEED_ON_CLASS_IRIS = {
    "Agent": "FEED-ON::Agent",
    "Client": "FEED-ON::Client",
    "ConsequenceExpected": "FEED-ON::ConsequenceExpected",
    "EndUser": "FEED-ON::EndUser",
    "ExternalAgent": "FEED-ON::ExternalAgent",
    "ExplicitFeedbackElicitationTechnique": "ExplicitFeedbackElicitationTechnique",
    "Feedback": "FEED-ON::Feedback",
    "FeedbackAttribute": "FEED-ON::FeedbackAttribute",
    "ImplicitFeedbackElicitationTechnique": "ImplicitFeedbackElicitationTechnique",
    "InternalAgent": "FEED-ON::InternalAgent",
    "Sentiment": "Sentiment",
    "Target": "FEED-ON::Target",
}
CONSEQUENCE_CLASSES = {"Correction", "Improvement", "Prioritization"}
TARGET_ROOT_CLASSES = {"DataItem", "Feature", "Process", "QualityAttribute", "Requirement", "UIElement"}


def classify_target(text: str, domain_name: str = "geral") -> tuple[str, str]:
    """
    Analisa o texto do feedback e infere a classe e o individuo do Target
    com base no lexico persistente do dominio informado para a FEED-ON.
    """
    text_lower = (text or "").lower()
    normalized_text = _strip_accents(text_lower)
    lexicon = DomainLexicon.objects.filter(domain_name=normalize_domain_name(domain_name)).first()
    if lexicon is None:
        return "Feature", "Feature_General"

    for target_class, field_name in CATEGORY_FIELDS.items():
        for keyword in keywords_from_storage(getattr(lexicon, field_name, "")):
            keyword_regex = _keyword_regex(keyword)
            normalized_regex = _strip_accents(keyword_regex)
            if re.search(keyword_regex, text_lower) or re.search(normalized_regex, normalized_text):
                clean_keyword = _clean_keyword_for_individual(keyword)
                instance_name = f"{target_class}_{clean_keyword.title().replace(' ', '')}"
                return target_class, _safe_name(instance_name)

    return "Feature", "Feature_General"


def atualizar_jira_key_na_ontologia(source_id: str, consequence: str, jira_key: str) -> None:
    from owlready2 import DataProperty, World

    ontology_path = FeedOnOntologyService()._resolve_ontology_path()
    if ontology_path is None:
        raise RuntimeError("Arquivo de ontologia nao encontrado para atualizar jiraKey.")

    world = World()
    try:
        onto = world.get_ontology(str(ontology_path)).load()
    except Exception:
        onto = world.get_ontology(ontology_path.as_uri()).load()

    safe_id = _safe_name(source_id)
    consequence_name = f"{_valid_consequence(consequence)}_{safe_id}"
    consequence_individual = onto.search_one(iri=f"*{consequence_name}")
    if consequence_individual is None:
        raise RuntimeError(f"Individuo de consequencia nao encontrado na ontologia: {consequence_name}")

    with onto:
        jira_key_prop = _data_property_or_create(onto, "jiraKey", DataProperty)

    _set_data_property(consequence_individual, jira_key_prop, jira_key, replace=True)
    onto.save(file=str(ontology_path))


@dataclass(frozen=True)
class OntologyResult:
    inferred_target: str
    consequence: str
    jira_key: str = ""
    warnings: tuple[str, ...] = ()


class FeedOnOntologyService:
    def __init__(self):
        self.world = None
        self.ontology = None
        self.loaded = False
        self.ofn_loaded = False
        self.load_warning = ""
        self.part_of_assertions: dict[str, str] = {}
        self.ontology_path = self._resolve_ontology_path()
        self.feedback_by_source: dict[str, str] = {}
        self.runtime_sources: list[str] = []
        self.last_source_id = ""
        self.current_job_id = ""
        self.assertion_audit = {}
        self._load()

    def prepare_for_job(self, job_id: int | str) -> int:
        self.current_job_id = str(job_id)
        return self.reset_runtime_entities()

    def reset_runtime_entities(self) -> int:
        removed = 0
        for source_id in list(self.runtime_sources):
            feedback = self._feedback_for_source(source_id)
            if feedback is None:
                continue
            logger.info("Limpando individuo runtime antes de novo processamento: %s", feedback.name)
            if _destroy_ontology_entity(feedback):
                removed += 1

        self.runtime_sources.clear()
        self.feedback_by_source.clear()
        self.last_source_id = ""
        return removed

    def interpret(
        self,
        source_id: str,
        text: str,
        intent: str,
        technical_target: str,
        sentiment_score: float | None = None,
        ai_provider: str = "",
        elicitation_technique: str = "",
        agent_pseudonym: str = "",
        agent_role_type: str = "",
        domain_name: str = "geral",
        create_jira_issue: bool = False,
    ) -> OntologyResult:
        warnings = []
        if self.load_warning:
            warnings.append(self.load_warning)

        if (technical_target or "").strip():
            parts = re.split(r"[._:]", technical_target.strip(), maxsplit=1)
            target_class = parts[0] or "Feature"
            target_name = parts[1] if len(parts) > 1 else parts[0]
            target_instance_name = f"{target_class}_{_safe_name(target_name)}"
        else:
            target_class, target_instance_name = classify_target(text, domain_name=domain_name)
        inferred_target = target_instance_name
        consequence = self._derive_consequence(intent, text, sentiment_score)
        jira_key = ""

        if self.loaded:
            try:
                self._instantiate_feedback(
                    source_id,
                    text,
                    intent,
                    target_class,
                    target_instance_name,
                    consequence,
                    sentiment_score,
                    elicitation_technique,
                    agent_pseudonym,
                    agent_role_type,
                    create_jira_issue,
                )
            except Exception as exc:  # pragma: no cover
                message = f"Ontologia carregada, mas a instanciacao falhou para {source_id}: {exc}"
                logger.warning(message)
                feedback = self._feedback_for_source(source_id)
                if feedback is not None:
                    logger.warning(
                        "Snapshot do individuo imediatamente antes da falha de instanciacao (%s): %s",
                        feedback.name,
                        self._individual_snapshot(feedback),
                    )
                warnings.append(message)

        return OntologyResult(
            inferred_target=inferred_target,
            consequence=consequence,
            jira_key=jira_key,
            warnings=tuple(warnings),
        )

    def run_reasoner(self) -> tuple[bool, str]:
        if not self.loaded or not settings.FEED_ON_RUN_REASONER:
            return False, ""

        before = self._assertion_snapshot()
        try:
            if settings.FEED_ON_REASONER.lower() != "pellet":
                return False, "Reasoner diferente de Pellet configurado; etapa pulada."

            from owlready2 import sync_reasoner_pellet

            sync_reasoner_pellet([self.ontology], infer_property_values=True, infer_data_property_values=True, debug=0)
            after = self._assertion_snapshot()
            self.assertion_audit = self._compare_assertions(before, after)
            return True, ""
        except Exception as exc:  # pragma: no cover
            result = self._handle_reasoner_exception(exc)
            after = self._assertion_snapshot()
            self.assertion_audit = self._compare_assertions(before, after)
            return result

    def _assertion_snapshot(self) -> set[tuple[str, str, str, str]]:
        """Return sanitized assertions in the subgraph instantiated for this job."""
        if not self.ontology:
            return set()
        scope = set()
        frontier = [self._feedback_for_source(source) for source in self.runtime_sources]
        frontier = [item for item in frontier if item is not None]
        for _ in range(3):
            next_frontier = []
            for individual in frontier:
                if individual in scope:
                    continue
                scope.add(individual)
                for prop in individual.get_properties():
                    if getattr(prop, "is_a", None) is None:
                        continue
                    try:
                        values = list(prop[individual])
                    except Exception:
                        continue
                    next_frontier.extend(value for value in values if hasattr(value, "iri"))
            frontier = next_frontier

        assertions = set()
        for individual in scope:
            subject = getattr(individual, "iri", getattr(individual, "name", str(individual)))
            for cls in getattr(individual, "is_a", []):
                if hasattr(cls, "iri"):
                    assertions.add(("class", subject, "rdf:type", cls.iri))
            for prop in individual.get_properties():
                predicate = getattr(prop, "iri", getattr(prop, "name", str(prop)))
                try:
                    values = list(prop[individual])
                except Exception:
                    continue
                for value in values:
                    if hasattr(value, "iri"):
                        assertions.add(("object", subject, predicate, value.iri))
                    else:
                        literal = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
                        hashed = hashlib.sha256(literal.encode("utf-8")).hexdigest()
                        assertions.add(("data", subject, predicate, f"sha256:{hashed}"))
        return assertions

    @staticmethod
    def _compare_assertions(before, after) -> dict:
        inferred = sorted(after - before)
        removed = sorted(before - after)
        direct = sorted(before)
        return {
            "scope": "job_runtime_subgraph",
            "literal_values": "sha256_only",
            "assertions_before": len(before),
            "assertions_after": len(after),
            "direct_assertions": len(direct),
            "inferred_assertions": len(inferred),
            "removed_assertions": len(removed),
            "direct_by_kind": _count_assertion_kinds(direct),
            "inferred_by_kind": _count_assertion_kinds(inferred),
            "removed_by_kind": _count_assertion_kinds(removed),
            "direct": [_assertion_dict(item) for item in direct],
            "inferred": [_assertion_dict(item) for item in inferred],
            "removed": [_assertion_dict(item) for item in removed],
        }

    def save(self) -> Path | None:
        if not self.loaded or self.ontology is None or self.ontology_path is None:
            return None
        output_dir = settings.BASE_DIR / "results" / f"job_{self.current_job_id or 'unknown'}"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"FEED-ON-job-{self.current_job_id or 'unknown'}-instantiated.owl"
        self.ontology.save(file=str(output_path))
        return output_path

    def inferred_target_for(self, source_id: str) -> str:
        if self.ontology is None:
            return ""

        feedback = self._feedback_for_source(source_id)
        if feedback is None:
            return ""

        for prop in feedback.get_properties():
            if prop.name != "refersTo":
                continue
            for target in list(prop[feedback]):
                inferred_from_part = self._target_parent_name(target)
                if inferred_from_part:
                    return inferred_from_part
                target_name = getattr(target, "name", "")
                if target_name:
                    return _display_name(target_name)
        return ""

    def consequence_for(self, source_id: str) -> str:
        if self.ontology is None:
            return ""

        feedback = self._feedback_for_source(source_id)
        if feedback is None:
            return ""

        for prop in feedback.get_properties():
            if prop.name != "indicates":
                continue
            for value in list(prop[feedback]):
                names = [value.name] + [cls.name for cls in getattr(value, "is_a", []) if hasattr(cls, "name")]
                for name in names:
                    if name in {"Correction", "Improvement", "Prioritization"}:
                        return name
        return ""

    def _handle_reasoner_exception(self, exc: Exception) -> tuple[bool, str]:
        message = str(exc)
        lower_message = message.lower()

        if "inconsistent" in lower_message:
            self._log_last_feedback_state("Reasoner inconsistente sem candidato claro")
            failure = f"Reasoner falhou por ontologia inconsistente; nenhuma assercao direta foi removida: {exc}"
            logger.warning(failure)
            return False, failure

        warning = f"Reasoner falhou; mantendo inferencia deterministica: {exc}"
        logger.warning(warning)
        return False, warning

    def _remove_inconsistency_candidates(self) -> list[str]:
        suspects = self._find_suspect_feedbacks()
        if not suspects and self.last_source_id:
            last_feedback = self._feedback_for_source(self.last_source_id)
            if last_feedback is not None:
                suspects = [last_feedback]

        removed_names: list[str] = []
        for feedback in suspects:
            logger.warning(
                "Snapshot do individuo imediatamente antes do erro de inconsistencia (%s): %s",
                feedback.name,
                self._individual_snapshot(feedback),
            )
            if _destroy_ontology_entity(feedback):
                removed_names.append(feedback.name)

        if removed_names:
            removed_sources = [source for source, name in self.feedback_by_source.items() if name in removed_names]
            self.runtime_sources = [source for source in self.runtime_sources if source not in removed_sources]
            for source in removed_sources:
                self.feedback_by_source.pop(source, None)

        return removed_names

    def _find_suspect_feedbacks(self) -> list[Any]:
        suspects = []
        for source_id in self.runtime_sources:
            feedback = self._feedback_for_source(source_id)
            if feedback is None:
                continue
            conflicts = self._functional_conflicts(feedback)
            if conflicts:
                logger.warning("Conflitos funcionais detectados em %s: %s", feedback.name, ", ".join(conflicts))
                suspects.append(feedback)
        return suspects

    def _functional_conflicts(self, feedback) -> list[str]:
        conflicts: list[str] = []

        for prop in feedback.get_properties():
            try:
                values = list(prop[feedback])
            except Exception:
                continue
            if len(values) <= 1:
                continue

            if prop.name in FUNCTIONAL_DEBUG_PROPERTIES:
                conflicts.append(f"{prop.name}={len(values)}")
                continue

            is_functional = False
            checker = getattr(prop, "is_functional_for", None)
            if callable(checker):
                try:
                    is_functional = bool(checker(feedback.__class__))
                except Exception:
                    is_functional = False
            if is_functional:
                conflicts.append(f"{prop.name}={len(values)}")

        return conflicts

    def _feedback_for_source(self, source_id: str):
        if self.ontology is None:
            return None

        known_name = self.feedback_by_source.get(source_id)
        if known_name:
            found = self.ontology.search_one(iri=f"*{known_name}")
            if found is not None:
                return found

        safe_name = _safe_name(source_id)
        feedback = self.ontology.search_one(iri=f"*Feedback_{safe_name}")
        if feedback is None:
            feedback = self.ontology.search_one(iri=f"*feedback_{safe_name}")
        return feedback

    def _log_last_feedback_state(self, prefix: str) -> None:
        if not self.last_source_id:
            return

        feedback = self._feedback_for_source(self.last_source_id)
        if feedback is None:
            return

        logger.warning("%s: %s", prefix, self._individual_snapshot(feedback))

    def _individual_snapshot(self, individual) -> str:
        properties = {}
        for prop in individual.get_properties():
            try:
                values = list(prop[individual])
            except Exception:
                values = []
            properties[prop.name] = [self._value_repr(value) for value in values]

        classes = [self._value_repr(cls) for cls in getattr(individual, "is_a", [])]
        return str(
            {
                "individual": getattr(individual, "name", str(individual)),
                "classes": classes,
                "properties": properties,
            }
        )

    def _value_repr(self, value) -> str:
        if hasattr(value, "name"):
            return value.name
        return str(value)

    def _load(self) -> None:
        if self.ontology_path is None:
            self.load_warning = "Arquivo de ontologia nao encontrado; usando inferencia deterministica."
            return

        try:
            from owlready2 import World

            self.world = World()
            try:
                self.ontology = self.world.get_ontology(str(self.ontology_path)).load()
            except Exception:
                self.ontology = self.world.get_ontology(self.ontology_path.as_uri()).load()
            self.loaded = True
            self._validate_required_entities()
        except Exception as exc:  # pragma: no cover
            self._load_ofn_assertions()
            if self.ofn_loaded:
                self.load_warning = (
                    f"Owlready2 nao carregou {self.ontology_path} ({exc}); "
                    "usando axiomas partOf extraidos do OFN e fallback deterministico."
                )
            else:
                self.load_warning = (
                    f"Nao foi possivel carregar {self.ontology_path} com Owlready2 ({exc}); "
                    "usando inferencia deterministica."
                )
            logger.warning(self.load_warning)

    def _validate_required_entities(self) -> None:
        required_classes = {"Feedback", "Target", "ConsequenceExpected"}
        required_properties = {"refersTo", "indicates", "aimsToEvolve", "isProvidedBy"}
        classes = {item.name.lstrip(":") for item in self.ontology.classes()}
        properties = {item.name for item in self.ontology.object_properties()}
        if not required_classes <= classes or not required_properties <= properties:
            raise ValueError("The configured OWL file does not contain the required FEED-ON entities.")

    def metrics(self) -> dict:
        if not self.loaded:
            return {}
        return {
            "version": settings.FEED_ON_ONTOLOGY_VERSION,
            "path": str(self.ontology_path),
            "sha256": hashlib.sha256(self.ontology_path.read_bytes()).hexdigest(),
            "classes": len(list(self.ontology.classes())),
            "object_properties": len(list(self.ontology.object_properties())),
            "data_properties": len(list(self.ontology.data_properties())),
            "individuals": len(list(self.ontology.individuals())),
        }

    def _load_ofn_assertions(self) -> None:
        if self.ontology_path is None or self.ontology_path.suffix.lower() != ".ofn":
            return
        content = self.ontology_path.read_text(encoding="utf-8-sig")
        for child, parent in re.findall(r"ObjectPropertyAssertion\(:partOf\s+:([^\s\)]+)\s+:([^\s\)]+)\)", content):
            self.part_of_assertions[child] = parent
            self.part_of_assertions[_display_name(child)] = _display_name(parent)
        self.ofn_loaded = bool(self.part_of_assertions)

    def _resolve_ontology_path(self) -> Path | None:
        configured = Path(settings.FEED_ON_ONTOLOGY_PATH)
        candidates = [configured if configured.is_absolute() else settings.BASE_DIR / configured]
        candidates.extend([settings.BASE_DIR / "ontology" / "FEED-ON.owl", settings.BASE_DIR / "ontology" / "FEED-ON.ofn"])
        for candidate in candidates:
            if candidate.exists():
                return candidate.resolve()
        return None

    def _instantiate_feedback(
        self,
        source_id: str,
        text: str,
        intent: str,
        target_class_name: str,
        target_instance_name: str,
        consequence: str,
        sentiment_score: float | None = None,
        elicitation_technique: str = "",
        agent_pseudonym: str = "",
        agent_role_type: str = "",
        create_jira_issue: bool = False,
    ) -> str:
        from owlready2 import DataProperty, ObjectProperty, Thing

        onto = self.ontology
        safe_id = _safe_name(source_id or uuid.uuid4().hex)
        feedback_name = f"Feedback_{safe_id}"

        existing_feedback = onto.search_one(iri=f"*{feedback_name}")
        if existing_feedback is not None:
            logger.warning(
                "Feedback %s ja existia; removendo instancia antiga antes de reclassificar.",
                feedback_name,
            )
            logger.warning(
                "Snapshot do individuo antigo antes da substituicao (%s): %s",
                feedback_name,
                self._individual_snapshot(existing_feedback),
            )
            _destroy_ontology_entity(existing_feedback)

        with onto:
            feedback_cls = _class_or_create(onto, "Feedback", Thing, iri_suffix=FEED_ON_CLASS_IRIS["Feedback"])
            target_cls = _class_or_create(onto, target_class_name, Thing)
            consequence_cls = _class_or_create(onto, _valid_consequence(consequence), Thing)
            elicitation_class_name = _elicitation_class_name(elicitation_technique)
            elicitation_cls = _class_or_create(
                onto,
                elicitation_class_name,
                Thing,
                iri_suffix=FEED_ON_CLASS_IRIS[elicitation_class_name],
            )
            agent_class_name = agent_role_type if agent_role_type in {"InternalAgent", "ExternalAgent"} else "Agent"
            agent_cls = _class_or_create(
                onto,
                agent_class_name,
                Thing,
                iri_suffix=FEED_ON_CLASS_IRIS[agent_class_name],
            )
            intention_cls = _class_or_create(onto, "Intention", Thing)
            sentiment_cls = _class_or_create(onto, "Sentiment", Thing, iri_suffix=FEED_ON_CLASS_IRIS["Sentiment"])
            refers_to = _object_property_or_create(onto, "refersTo", ObjectProperty)
            has_intention = _object_property_or_create(onto, "hasIntention", ObjectProperty)
            has_sentiment = _object_property_or_create(onto, "hasSentiment", ObjectProperty)
            indicates = _object_property_or_create(onto, "indicates", ObjectProperty)
            aims_to_evolve = _object_property_or_create(onto, "aimsToEvolve", ObjectProperty)
            is_elicited_through = _object_property_or_create(onto, "isElicitedThrough", ObjectProperty)
            is_provided_by = _object_property_or_create(onto, "isProvidedBy", ObjectProperty)
            comment_text = _data_property_or_create(onto, "commentText", DataProperty)
            is_processed = _data_property_or_create(onto, "isProcessed", DataProperty)
            sentiment_score_prop = _data_property_or_create(onto, "sentimentScore", DataProperty)
            jira_key = _data_property_or_create(onto, "jiraKey", DataProperty)

        feedback = feedback_cls(feedback_name)
        target = _individual_or_create(onto, target_instance_name, target_cls)
        intention = _individual_or_create(onto, _intention_individual_name(intent), intention_cls)
        consequence_individual = _replace_consequence_individual(
            onto,
            f"{_valid_consequence(consequence)}_{safe_id}",
            consequence_cls,
        )
        elicitation = _individual_or_create(onto, _elicitation_individual_name(elicitation_technique), elicitation_cls)
        provider_name = _safe_name(agent_pseudonym or f"Agent_{safe_id}")
        provider = _individual_or_create(onto, provider_name, agent_cls)

        self._clear_previous_intention_classifications(feedback)

        feedback.label = [f"Feedback {source_id}"]
        _set_data_property(feedback, comment_text, text, replace=True)
        _set_data_property(feedback, is_processed, True, replace=True)
        _set_object_property(feedback, has_intention, intention, replace=True)
        _set_object_property(feedback, refers_to, target, replace=True)
        _set_object_property(feedback, indicates, consequence_individual, replace=True)
        _set_object_property(feedback, is_elicited_through, elicitation, replace=True)
        _set_object_property(feedback, is_provided_by, provider, replace=True)

        if sentiment_score is not None:
            _register_xsd_float_datatype()
            sentiment = _replace_sentiment_individual(onto, f"Sentiment_{safe_id}", sentiment_cls)
            _set_data_property(sentiment, sentiment_score_prop, XsdFloat(sentiment_score), replace=True)
            _set_object_property(feedback, has_sentiment, sentiment, replace=True)

        _set_object_property(consequence_individual, aims_to_evolve, target, replace=True)
        created_jira_key = ""
        _set_data_property(consequence_individual, jira_key, "PENDING", replace=True)

        self.feedback_by_source[source_id] = feedback_name
        if source_id not in self.runtime_sources:
            self.runtime_sources.append(source_id)
        self.last_source_id = source_id
        return created_jira_key

    def _clear_previous_intention_classifications(self, feedback) -> None:
        current_classes = list(getattr(feedback, "is_a", []))
        filtered = [
            cls
            for cls in current_classes
            if getattr(cls, "name", "") not in INTENTION_CLASS_MARKERS
        ]

        if len(filtered) != len(current_classes):
            feedback.is_a = filtered

    def _fallback_inferred_target(self, technical_target: str) -> str:
        if technical_target in self.part_of_assertions:
            return self.part_of_assertions[technical_target]
        safe_target = _safe_name(technical_target)
        if safe_target in self.part_of_assertions:
            return self.part_of_assertions[safe_target]
        return TARGET_PARENT_RULES.get(technical_target, technical_target or "Feature.General")

    def _derive_consequence(self, intent: str, text: str, sentiment_score: float | None = None) -> str:
        if sentiment_score is not None:
            return "Correction" if float(sentiment_score) <= -0.5 else "Improvement"
        if intent in {"Intention_BugReport", "BugReport", "Report"}:
            return "Correction"
        if intent in {"Intention_Suggestion", "Suggestion", "FeatureRequest"}:
            return "Improvement"
        normalized = text.lower()
        if any(word in normalized for word in ("erro", "falha", "bug", "nao funciona")):
            return "Correction"
        if any(word in normalized for word in ("urgente", "prioridade", "critico")):
            return "Prioritization"
        return "Improvement"

    def _target_parent_name(self, target) -> str:
        for prop in target.get_properties():
            if prop.name != "partOf":
                continue
            values = list(prop[target])
            if values:
                return _display_name(values[0].name)
        return ""


def _individual_or_create(onto, name: str, cls):
    found = onto.search_one(iri=f"*{name}")
    if found is not None:
        if cls not in getattr(found, "is_a", []):
            found.is_a.append(cls)
        return found
    return cls(name)


def _replace_consequence_individual(onto, name: str, cls):
    found = onto.search_one(iri=f"*{name}")
    if found is not None:
        _destroy_ontology_entity(found)
    return cls(name)


def _replace_sentiment_individual(onto, name: str, cls):
    found = onto.search_one(iri=f"*{name}")
    if found is not None:
        _destroy_ontology_entity(found)
    return cls(name)


def _intention_individual_name(intent: str) -> str:
    if intent in {"Intention_BugReport", "BugReport", "Report"}:
        return "Intention_BugReport"
    if intent in {"Intention_Suggestion", "Suggestion", "FeatureRequest"}:
        return "Intention_Suggestion"
    return _safe_name(intent)


def _elicitation_class_name(provider: str) -> str:
    if str(provider or "").strip().lower() == "implicit":
        return "ImplicitFeedbackElicitationTechnique"
    return "ExplicitFeedbackElicitationTechnique"


def _elicitation_individual_name(provider: str) -> str:
    return _elicitation_class_name(provider).replace("FeedbackElicitationTechnique", "FeedbackElicitation")


def _set_object_property(individual, prop, value, *, replace: bool = False) -> None:
    current = getattr(individual, prop.python_name, None)
    if hasattr(current, "append"):
        if replace:
            current[:] = [value]
            return
        if value not in current:
            current.append(value)
        return
    setattr(individual, prop.python_name, value)


def _set_data_property(individual, prop, value: Any, *, replace: bool = False) -> None:
    current = getattr(individual, prop.python_name, None)
    if hasattr(current, "append"):
        if replace:
            current[:] = [value]
            return
        if value not in current:
            current.append(value)
        return
    setattr(individual, prop.python_name, value)


def _class_or_create(onto, name: str, base, *, iri_suffix: str | None = None):
    safe = _safe_class_name(name)
    if iri_suffix:
        found = _find_entity_by_iri_suffix(onto.classes(), iri_suffix)
        if found is not None:
            return found
    found = getattr(onto, safe, None)
    if found is not None and isinstance(found, type):
        return found
    for cls in onto.classes():
        if cls.name == safe or cls.name.lstrip(":") == safe:
            return cls
    return type(safe, (base,), {"namespace": onto})


def _target_class_for(onto, value: str, base):
    root = _target_root_name(value)
    if root in TARGET_ROOT_CLASSES:
        return _class_or_create(onto, root, base)
    return _class_or_create(onto, "Target", base, iri_suffix=FEED_ON_CLASS_IRIS["Target"])


def _target_root_name(value: str) -> str:
    root = (value or "").split(".", 1)[0]
    return root if root in TARGET_ROOT_CLASSES else ""


def _valid_consequence(value: str) -> str:
    return value if value in CONSEQUENCE_CLASSES else "Improvement"


def _find_entity_by_iri_suffix(entities, iri_suffix: str):
    for entity in entities:
        if getattr(entity, "iri", "").endswith(iri_suffix):
            return entity
    return None


def _object_property_or_create(onto, name: str, base):
    found = getattr(onto, name, None)
    if found is not None:
        return found
    for prop in onto.object_properties():
        if prop.name == name:
            return prop
    return type(name, (base,), {"namespace": onto})


def _data_property_or_create(onto, name: str, base):
    found = getattr(onto, name, None)
    if found is not None:
        return found
    for prop in onto.data_properties():
        if prop.name == name:
            return prop
    return type(name, (base,), {"namespace": onto})


def _destroy_ontology_entity(entity) -> bool:
    try:
        from owlready2 import destroy_entity

        destroy_entity(entity)
        return True
    except Exception:
        namespace = getattr(entity, "namespace", None)
        destroy_method = getattr(namespace, "destroy_entity", None)
        if callable(destroy_method):
            try:
                destroy_method(entity)
                return True
            except Exception:
                return False
        return False


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_]+", "_", _strip_accents(value or "unknown"))
    return cleaned.strip("_") or "unknown"


def _clean_keyword_for_individual(keyword_regex: str) -> str:
    cleaned = keyword_regex.replace(r"\b", "").replace("\\", "").strip()
    return re.sub(r"\s+", " ", cleaned)


def _keyword_regex(keyword: str) -> str:
    escaped = re.escape((keyword or "").strip().lower())
    escaped = escaped.replace(r"\ ", r"\s+")
    return rf"\b{escaped}\b"


def _strip_accents(value: str) -> str:
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")


def _safe_class_name(value: str) -> str:
    safe = _safe_name(_simple_name(value))
    if safe[:1].isdigit():
        safe = f"C_{safe}"
    return safe


def _simple_name(value: str) -> str:
    return (value or "Unknown").split(".")[-1]


def _display_name(value: str) -> str:
    return value.replace("_", ".")


def _count_assertion_kinds(assertions) -> dict[str, int]:
    counts = {"class": 0, "object": 0, "data": 0}
    for kind, *_ in assertions:
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def _assertion_dict(assertion) -> dict[str, str]:
    kind, subject, predicate, value = assertion
    return {"kind": kind, "subject": subject, "predicate": predicate, "value": value}

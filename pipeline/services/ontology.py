import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

TARGET_PARENT_RULES = {
    "UIElement.Button.Save": "Feature.Persistence",
    "UIElement.Button.Submit": "Feature.FormSubmission",
    "UIElement.Filter.Date": "Feature.SearchAndFiltering",
    "UIElement.Search": "Feature.SearchAndFiltering",
}

INTENTION_CLASS_MARKERS = {"Intention_BugReport", "Intention_Suggestion", "BugReport", "Suggestion"}
FUNCTIONAL_DEBUG_PROPERTIES = {"hasIntention", "commentText", "sentimentScore", "jiraKey"}


@dataclass(frozen=True)
class OntologyResult:
    inferred_target: str
    consequence: str
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

    def interpret(self, source_id: str, text: str, intent: str, technical_target: str) -> OntologyResult:
        warnings = []
        if self.load_warning:
            warnings.append(self.load_warning)

        inferred_target = self._fallback_inferred_target(technical_target)
        consequence = self._derive_consequence(intent, text)

        if self.loaded:
            try:
                self._instantiate_feedback(source_id, text, intent, technical_target, inferred_target, consequence)
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

        return OntologyResult(inferred_target=inferred_target, consequence=consequence, warnings=tuple(warnings))

    def run_reasoner(self) -> tuple[bool, str]:
        if not self.loaded or not settings.FEED_ON_RUN_REASONER:
            return False, ""

        try:
            if settings.FEED_ON_REASONER.lower() != "pellet":
                return False, "Reasoner diferente de Pellet configurado; etapa pulada."

            from owlready2 import sync_reasoner_pellet

            sync_reasoner_pellet(infer_property_values=True, infer_data_property_values=True, debug=0)
            return True, ""
        except Exception as exc:  # pragma: no cover
            return self._handle_reasoner_exception(exc)

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
                if getattr(target, "name", "").lower().startswith("feature"):
                    return _display_name(target.name)
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
            removed_entities = self._remove_inconsistency_candidates()
            if removed_entities:
                try:
                    from owlready2 import sync_reasoner_pellet

                    sync_reasoner_pellet(infer_property_values=True, infer_data_property_values=True, debug=0)
                    warning = (
                        "Reasoner detectou inconsistencia e ignorou individuos suspeitos: "
                        f"{', '.join(removed_entities)}"
                    )
                    logger.warning(warning)
                    return True, warning
                except Exception as retry_exc:  # pragma: no cover
                    retry_message = (
                        "Reasoner permaneceu inconsistente apos remover individuos suspeitos: "
                        f"{retry_exc}"
                    )
                    logger.warning(retry_message)
                    return False, retry_message

            self._log_last_feedback_state("Reasoner inconsistente sem candidato claro")
            failure = f"Reasoner falhou por ontologia inconsistente: {exc}"
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
        technical_target: str,
        inferred_target: str,
        consequence: str,
    ) -> None:
        from owlready2 import DataProperty, ObjectProperty, Thing

        onto = self.ontology
        safe_id = _safe_name(source_id)
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
            feedback_cls = _class_or_create(onto, "Feedback", Thing)
            target_cls = _class_or_create(onto, _simple_name(technical_target), Thing)
            inferred_cls = _class_or_create(onto, _simple_name(inferred_target), Thing)
            intention_cls = _class_or_create(onto, "Intention", Thing)
            refers_to = _object_property_or_create(onto, "refersTo", ObjectProperty)
            part_of = _object_property_or_create(onto, "partOf", ObjectProperty)
            has_intention = _object_property_or_create(onto, "hasIntention", ObjectProperty)
            comment_text = _data_property_or_create(onto, "commentText", DataProperty)

        feedback = feedback_cls(feedback_name)
        target = _individual_or_create(onto, _safe_name(technical_target), target_cls)
        inferred = _individual_or_create(onto, _safe_name(inferred_target), inferred_cls)
        intention = _individual_or_create(onto, _intention_individual_name(intent), intention_cls)

        self._clear_previous_intention_classifications(feedback)

        feedback.label = [f"Feedback {source_id}"]
        _set_data_property(feedback, comment_text, text, replace=True)
        _set_object_property(feedback, has_intention, intention, replace=True)
        _set_object_property(feedback, refers_to, target, replace=True)

        if target is not inferred:
            _set_object_property(target, part_of, inferred, replace=False)

        self.feedback_by_source[source_id] = feedback_name
        if source_id not in self.runtime_sources:
            self.runtime_sources.append(source_id)
        self.last_source_id = source_id

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

    def _derive_consequence(self, intent: str, text: str) -> str:
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
        return found
    return cls(name)


def _intention_individual_name(intent: str) -> str:
    if intent in {"Intention_BugReport", "BugReport", "Report"}:
        return "Intention_BugReport"
    if intent in {"Intention_Suggestion", "Suggestion", "FeatureRequest"}:
        return "Intention_Suggestion"
    return _safe_name(intent)


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


def _set_data_property(individual, prop, value: str, *, replace: bool = False) -> None:
    current = getattr(individual, prop.python_name, None)
    if hasattr(current, "append"):
        if replace:
            current[:] = [value]
            return
        if value not in current:
            current.append(value)
        return
    setattr(individual, prop.python_name, value)


def _class_or_create(onto, name: str, base):
    safe = _safe_class_name(name)
    found = getattr(onto, safe, None)
    if found is not None and isinstance(found, type):
        return found
    for cls in onto.classes():
        if cls.name == safe:
            return cls
    return type(safe, (base,), {"namespace": onto})


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
    cleaned = re.sub(r"[^0-9A-Za-z_]+", "_", value or "unknown")
    return cleaned.strip("_") or "unknown"


def _safe_class_name(value: str) -> str:
    safe = _safe_name(_simple_name(value))
    if safe[:1].isdigit():
        safe = f"C_{safe}"
    return safe


def _simple_name(value: str) -> str:
    return (value or "Unknown").split(".")[-1]


def _display_name(value: str) -> str:
    return value.replace("_", ".")

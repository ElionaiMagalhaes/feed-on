import tempfile
from pathlib import Path

from django.test import SimpleTestCase, TestCase, override_settings

from pipeline.models import DomainLexicon, DomainLexiconTerm, FeedbackAgent, ProcessingJob
from pipeline.services.csv_reader import iter_feedback
from pipeline.services.processor import _agent_for
from pipeline.services.semantics import derive_consequences, normalize_text, resolve_targets
from pipeline.services.experiment import initialize_manifest, lexicon_manifest
from pipeline.services.ontology import FeedOnOntologyService


class CsvReaderTests(SimpleTestCase):
    def _read(self, content):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", encoding="utf-8-sig", newline="", delete=False) as handle:
            handle.write(content)
            path = Path(handle.name)
        try:
            return list(iter_feedback(path))
        finally:
            path.unlink(missing_ok=True)

    def test_username_invisible_header_description_and_missing_text(self):
        rows = self._read("user\u200bname,description\nalice,Falha no login\nbob,\n")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].agent_identifier, "alice")

    def test_combined_anonymized_agent_header(self):
        row = self._read("Agente/usuário,Feedback\nAnonimo-01,Falha no login\n")[0]
        self.assertEqual(row.agent_identifier, "Anonimo-01")

    def test_context_only_contains_real_values(self):
        row = self._read("description,browser\nErro,Firefox\n")[0]
        self.assertEqual(row.context, {"browser": "Firefox"})

    def test_headerless_xlsx_maps_first_column_to_anonymized_agent(self):
        from openpyxl import Workbook

        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as handle:
            path = Path(handle.name)
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.append(("Agent_Anon_01", "O botao nao funciona e precisa ser corrigido"))
        worksheet.append(("Agent_Anon_01", "A tela deveria ter um filtro adicional"))
        workbook.save(path)
        workbook.close()
        try:
            rows = list(iter_feedback(path))
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual([row.agent_identifier for row in rows], ["Agent_Anon_01", "Agent_Anon_01"])
        self.assertTrue(all(row.text for row in rows))


class OntologyAssertionAuditTests(SimpleTestCase):
    def test_assertion_diff_separates_direct_inferred_and_removed(self):
        direct = {
            ("object", "feedback:1", "refersTo", "target:1"),
            ("class", "feedback:1", "rdf:type", "Feedback"),
        }
        after = {
            ("class", "feedback:1", "rdf:type", "Feedback"),
            ("object", "feedback:1", "refersTo", "target:2"),
        }
        audit = FeedOnOntologyService._compare_assertions(direct, after)
        self.assertEqual(audit["direct_assertions"], 2)
        self.assertEqual(audit["inferred_assertions"], 1)
        self.assertEqual(audit["removed_assertions"], 1)
        self.assertEqual(audit["inferred"][0]["value"], "target:2")


@override_settings(
    APPLICATION_VERSION="1.1.0", JIRA_DRY_RUN=True,
    FEED_ON_AGENT_HASH_SALT="must-not-appear", OPENAI_API_KEY="must-not-appear",
)
class ExperimentManifestTests(TestCase):
    def test_manifest_is_complete_and_does_not_store_secrets(self):
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as handle:
            handle.write(b"anonymous-dataset")
            path = Path(handle.name)
        job = ProcessingJob.objects.create(original_filename=path.name, upload=str(path))
        try:
            manifest = initialize_manifest(job, path)
        finally:
            path.unlink(missing_ok=True)
        serialized = str(manifest)
        self.assertEqual(manifest["application"]["version"], "1.1.0")
        self.assertTrue(manifest["configuration"]["jira_dry_run"])
        self.assertIn("sha256", manifest["dataset"])
        self.assertNotIn("must-not-appear", serialized)

    def test_lexicon_snapshot_is_hashed_and_frozen(self):
        lexicon = DomainLexicon.objects.create(domain_name="academic", ui_elements="botao")
        snapshot = lexicon_manifest(lexicon)
        self.assertTrue(snapshot["frozen"])
        self.assertEqual(len(snapshot["sha256"]), 64)


class SemanticTests(TestCase):
    def setUp(self):
        self.lexicon = DomainLexicon.objects.create(domain_name="teste")
        DomainLexiconTerm.objects.create(lexicon=self.lexicon, expression="certidão", normalized_expression="certidao", canonical_name="CertificateManagement", target_type="Feature", source="manual")

    def test_csv_target_has_priority_and_specific_removes_fallback(self):
        targets = resolve_targets("UIElement.SaveButton", "login", "erro", self.lexicon)
        self.assertEqual(targets[0].source, "csv")
        self.assertFalse(any(item.target_name == "General" for item in targets))

    def test_candidate_original_multi_accent_and_synonym(self):
        targets = resolve_targets("", "Feature.Authentication", "Na tela de CERTIDÃO, o botão cancelar falha", self.lexicon)
        self.assertEqual(targets[0].source, "llm_target_candidate")
        self.assertGreaterEqual(len(targets), 3)
        self.assertIn("CertificateManagement", [item.target_name for item in targets])

    def test_missing_term_falls_back(self):
        self.assertEqual(resolve_targets("", "", "xyz", self.lexicon)[0].source, "fallback")

    def test_consequences_are_independent_and_hotspot_reachable(self):
        targets = resolve_targets("Feature.Login", "", "", self.lexicon)
        values = derive_consequences("Suggestion", "", -.8, "Urgente: falha; deveria melhorar", targets, {"Feature.Login": 3}, 3)
        self.assertEqual({x.consequence_type for x in values}, {"Correction", "Improvement", "Prioritization"})

    def test_negative_without_problem_is_not_correction(self):
        values = derive_consequences("Report", "", -.9, "Não gostei", [], {}, 3)
        self.assertNotIn("Correction", [x.consequence_type for x in values])


@override_settings(FEED_ON_AGENT_HASH_SALT="test-salt")
class AgentTests(TestCase):
    def setUp(self):
        self.job = ProcessingJob.objects.create(original_filename="a.csv", upload="a.csv")

    def test_same_username_reuses_pseudonym_and_unknown_role_is_generic(self):
        first = _agent_for(self.job, "Pessoa@Example.com", "")
        second = _agent_for(self.job, "pessoa@example.com", "")
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.pseudonym, "Agent_001")
        self.assertEqual(first.role_type, "")
        self.assertNotIn("Pessoa", first.source_hash)

    def test_different_username_creates_different_agents(self):
        self.assertNotEqual(_agent_for(self.job, "a", "").pk, _agent_for(self.job, "b", "").pk)

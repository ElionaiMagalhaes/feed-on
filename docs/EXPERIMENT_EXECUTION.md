# Execucao definitiva do experimento FEED-ON

- [ ] codigo commitado
- [ ] git status limpo
- [ ] versao da aplicacao definida
- [ ] OWL validado
- [ ] hash do OWL registrado
- [ ] lexico congelado
- [ ] prompt versionado
- [ ] modelo OpenAI registrado
- [ ] dataset anonimizado
- [ ] dry run Jira confirmado
- [ ] Pellet habilitado
- [ ] logs preservados
- [ ] manifesto experimental gerado

## Procedimento

1. Configure o ambiente a partir de `.env.example`, usando um salt estavel e secreto.
2. Execute `python manage.py migrate`, `python manage.py check` e `python manage.py test pipeline`.
3. Confirme `JIRA_DRY_RUN=true` e valide o OWL sem modificar o arquivo de referencia.
   A execucao FEED-ON 1.1.0 usa `ontology/FEED-ON-v0.2.owl`, versao ontologica 0.2.
4. Envie CSV, XLSX ou XLSM anonimizado. Comentarios humanos em planilha sao elicitacao explicita; origem desconhecida nao deve ser promovida a explicita.
5. Ao concluir, arquive `ProcessingJob.metadata`, logs e `results/job_<id>/FEED-ON-job-<id>-instantiated.owl`.
   O pipeline tambem gera `experimental-manifest.json` e `owl-assertion-audit.json` no mesmo diretorio.
6. Submeta alvos, consequencias, hotspots e payloads Jira a validacao humana antes de qualquer acao externa.

Contexto de coleta (arquivo, dominio e job) pertence ao manifesto. `FeedbackContext` representa somente dados reais da ocorrencia, como horario, dispositivo, navegador, tela ou ambiente.

## Auditoria do Pellet

O reasoner e executado uma unica vez, depois de todos os lotes terem sido instanciados. O arquivo `owl-assertion-audit.json` compara o subgrafo operacional do job imediatamente antes e depois do Pellet:

- `direct`: assercoes presentes antes do reasoner;
- `inferred`: assercoes acrescentadas pelo Pellet;
- `removed`: assercoes que deixaram de existir durante a classificacao;
- `*_by_kind`: totais separados em classe, propriedade de objeto e propriedade de dados.

Literais nao sao copiados para a auditoria: somente seu SHA-256 e preservado. IRIs de classes, propriedades e individuos operacionais permanecem para permitir rastreabilidade. Os totais e o caminho do arquivo tambem ficam em `ProcessingJob.metadata.reasoner` e no manifesto experimental.

## Tipagem e canonicalizacao de alvos

A extracao `feed-on-semantic-v2-target-typing` retorna separadamente o nome candidato e um tipo restrito a `Feature`, `UIElement`, `Requirement`, `Process`, `DataItem`, `QualityAttribute` ou `Target`. `Target` indica resolucao nominal sem evidencia suficiente para especializacao ontologica. O mapa `feed-on-target-map-v1` unifica, antes dos hotspots, equivalencias como button/botao, screen/tela, report/relatorio e certificate/certificado.

`Intention_BugReport` gera `Correction` pela regra `bug_report_intention`; Correction, Improvement e Prioritization continuam independentes e podem coexistir.

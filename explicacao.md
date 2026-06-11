# Explicacao tecnica do FEED-ON

## Visao geral

O FEED-ON e uma aplicacao Django que transforma feedbacks de usuarios em registros semanticamente classificados e rastreaveis. O pipeline combina IA, ontologia OWL e integracao Jira para converter comentarios livres em informacoes estruturadas: intencao, sentimento, alvo tecnico, alvo ontologico inferido e consequencia esperada.

## Entrada do pipeline

O usuario envia um arquivo pela tela de upload. Sao aceitos `.csv`, `.xlsx` e `.xlsm`.

O upload tambem possui o campo `Dominio do software`, usado para orientar a adaptacao do pipeline ao contexto analisado. Exemplos de dominio:

- `delivery`
- `gestao escolar`
- `telemedicina`
- `juridico`

Se o usuario nao informar um dominio especifico, o sistema usa `geral`.

## Criacao do job

A requisicao chega em `create_job`, em `pipeline/views.py`. Essa funcao:

- valida se o arquivo e suportado;
- confere o tamanho maximo;
- le o limite opcional de linhas;
- normaliza o dominio informado;
- cria um `ProcessingJob`;
- inicia o processamento via Celery ou thread local.

Na tela de upload, os jobs recentes sao links para o dashboard ja filtrado pelo job selecionado. Jobs com falha podem ser removidos em lote pela acao `Limpar falhas`, que apaga os registros e arquivos associados apenas dos jobs falhos do usuario autenticado.

O modelo `ProcessingJob`, em `pipeline/models.py`, representa o lote enviado. Ele guarda o arquivo, status, progresso, limite de linhas, dominio, metadados e erros.

## Leitura do arquivo

A leitura acontece em `pipeline/services/csv_reader.py`.

Para CSV, o leitor detecta automaticamente separadores como virgula, ponto e virgula, tab ou pipe.

Para Excel, o leitor usa `openpyxl`, abre a primeira aba ativa e usa a primeira linha nao vazia como cabecalho quando houver nomes reconhecidos. Se a planilha nao tiver cabecalho, o sistema analisa as primeiras linhas e infere a coluna com maior conteudo textual como texto do feedback.

O leitor produz objetos `CsvFeedback` com:

- `source_id`;
- `text`;
- `target`;
- `intent`.

Essa estrutura interna e a mesma para CSV e Excel.

## Mecanismo de adaptacao dinamica de contexto

Antes de instanciar a ontologia, o pipeline prepara um lexico de dominio em `pipeline/services/processor.py`.

O modelo `DomainLexicon`, em `pipeline/models.py`, persiste uma base de conhecimento por dominio:

- `domain_name`;
- `ui_elements`;
- `quality_attributes`;
- `requirements`;
- `processes`.

Quando um job inicia, `_prepare_domain_lexicon` verifica se o dominio ja existe.

Se nao existir, o sistema chama `generate_domain_keywords`, em `pipeline/services/llm.py`. Essa funcao usa a API da OpenAI para agir como um engenheiro de ontologias especializado no dominio informado e gerar jargoes em PT-BR para quatro categorias da FEED-ON:

- `UIElement`;
- `QualityAttribute`;
- `Requirement`;
- `Process`.

O retorno esperado e um JSON puro, sem markdown, contendo listas de termos por categoria. Esses termos sao normalizados e salvos em `DomainLexicon`.

Se o dominio ja existir, o pipeline reutiliza o lexico salvo. Opcionalmente, quando `FEED_ON_LEXICON_REFRESH_EXISTING=true`, o sistema chama novamente a OpenAI, une os termos antigos e novos com eliminacao de duplicatas e atualiza o registro.

Se a OpenAI falhar ou nao estiver configurada, o pipeline usa um lexico geral de fallback para manter o processamento ativo.

## Analise de IA dos feedbacks

A analise individual dos feedbacks esta em `pipeline/services/nlp.py`.

Quando `OPENAI_API_KEY` esta configurada, `extract_feedback_semantics_batch` chama a OpenAI para retornar:

- intencao bruta: `Report` ou `Suggestion`;
- sentimento entre `-1` e `1`;
- alvo candidato citado no comentario.

Depois, `map_to_feed_on_intention` converte a intencao para a FEED-ON:

```text
Report + sentimento < -0.5 -> Intention_BugReport
caso contrario -> Intention_Suggestion
```

Se a IA falhar, o sistema usa fallback local de regras para sentimento e intencao.

## Classificacao ontologica de alvos

A classificacao de alvo fica em `pipeline/services/ontology.py`, na funcao `classify_target(text, domain_name)`.

Antes, essa funcao dependia de um dicionario estatico de palavras-chave. Agora ela consulta `DomainLexicon` no banco de dados para o dominio do job.

O fluxo e:

1. normaliza o dominio;
2. busca o lexico persistido;
3. carrega os termos das categorias `UIElement`, `QualityAttribute`, `Requirement` e `Process`;
4. converte cada termo em expressao regular com fronteira de palavra inteira;
5. executa `re.search` no texto original e no texto sem acentos;
6. quando encontra match, cria o individuo ontologico no formato `Classe_Termo`;
7. se nao houver match, usa o fallback `Feature`, `Feature_General`.

Assim, o mesmo pipeline pode se adaptar a softwares de dominios diferentes sem alterar o codigo-fonte para cada dominio.

## Instanciacao da ontologia

O servico `FeedOnOntologyService` carrega a ontologia FEED-ON com Owlready2.

Para cada feedback, `interpret` recebe:

- texto;
- intencao;
- alvo tecnico;
- sentimento;
- provedor da IA;
- dominio.

Depois, o servico:

- classifica o alvo com base no lexico do dominio;
- deriva a consequencia esperada;
- cria individuos OWL;
- relaciona feedback, intencao, sentimento, alvo e consequencia.

As principais relacoes criadas sao:

```text
Feedback hasIntention Intention
Feedback hasSentiment Sentiment
Feedback refersTo Target
Feedback indicates ConsequenceExpected
ConsequenceExpected aimsToEvolve Target
Feedback isProvidedBy ExternalAgent
Feedback isElicitedThrough ExplicitFeedbackElicitationTechnique
```

## Reasoner

Depois de instanciar os feedbacks, o pipeline pode executar Pellet via Owlready2.

O objetivo e enriquecer inferencias, especialmente em relacoes como:

```text
Feedback refersTo Target
Target partOf Feature
```

Assim, um feedback sobre um elemento especifico pode ser associado a uma feature mais ampla.

## Saida do pipeline

Ao final, o sistema gera:

- registros `FeedbackRecord` no banco;
- eventos `PipelineEvent`;
- metadados de progresso;
- individuos na ontologia OWL;
- dados para dashboard;
- exportacao CSV;
- relatorio DOCX;
- possibilidade de exportacao manual ao Jira.

## Formulacao cientifica

O FEED-ON pode ser descrito como um pipeline de enriquecimento semantico de feedback textual com adaptacao dinamica de contexto. A IA atua em duas camadas: primeiro, na geracao de um lexico especifico para o dominio do software; depois, na extracao semantica de intencao, sentimento e alvo candidato dos feedbacks. A ontologia FEED-ON atua na camada de representacao formal, convertendo os resultados em individuos e relacoes OWL. O mecanismo de reasoner permite inferencias adicionais sobre alvos e consequencias. O resultado e uma base de feedbacks rastreavel, semanticamente classificada e adaptavel a diferentes dominios de software.

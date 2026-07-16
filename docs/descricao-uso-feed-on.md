# Descricao do uso da aplicacao FEED-ON

> **Registro historico (pre-1.1.0).** Este documento descreve o comportamento e o job #23 anteriores a revisao experimental FEED-ON 1.1.0. Os numeros abaixo nao devem ser usados como resultado definitivo. Para a nova execucao, utilize `docs/EXPERIMENT_EXECUTION.md`, `experimental-manifest.json` e `owl-assertion-audit.json` gerados pelo job congelado.

Este documento descreve a aplicacao FEED-ON desenvolvida no projeto local e reune informacoes tecnicas e operacionais para apoiar a escrita do capitulo do artigo sobre o uso da aplicacao. A descricao foi elaborada a partir da analise do codigo-fonte, dos modelos de dados, dos servicos do pipeline e dos registros existentes no banco local `db.sqlite3`.

## Visao geral

A aplicacao FEED-ON foi desenvolvida como um ambiente web para colocar em pratica a ontologia de feedback proposta no trabalho. Sua finalidade e transformar comentarios textuais de usuarios em dados estruturados, semanticamente classificados e rastreaveis. Para isso, a aplicacao combina processamento de arquivos, analise semantica por inteligencia artificial, instanciacao de uma ontologia OWL, inferencia ontologica e exportacao de resultados para artefatos de apoio a analise e gestao de requisitos.

O sistema foi implementado em Python com Django. O app principal chama-se `pipeline` e concentra as funcionalidades de upload, criacao de jobs, leitura dos feedbacks, processamento semantico, persistencia dos resultados, dashboard, exportacoes e integracao com Jira. A ontologia usada pela aplicacao fica na pasta `ontology`, com o arquivo `FEED-ON.owl`. O pipeline tenta carregar essa ontologia com Owlready2 e executar o reasoner Pellet quando a configuracao permite.

A aplicacao tambem possui autenticacao com Google por meio de `django-allauth`, separando jobs, dashboards e exportacoes por usuario autenticado. Em ambiente local, o sistema pode executar o processamento em thread local quando `CELERY_TASK_ALWAYS_EAGER=true`; em producao, o projeto esta preparado para usar Celery com Redis.

## Uso geral da aplicacao

O uso da aplicacao inicia na tela de upload, acessada por usuarios autenticados. Nessa tela, o usuario envia um arquivo contendo feedbacks textuais. Sao aceitos arquivos nos formatos `.csv`, `.xlsx` e `.xlsm`. O arquivo pode conter colunas explicitas para identificador, texto, alvo tecnico e intencao, mas a aplicacao tambem possui mecanismos de inferencia quando esses campos nao estao presentes.

No formulario de upload, o usuario informa tambem o dominio do software analisado, por exemplo `academico`, `delivery`, `gestao escolar` ou `telemedicina`. Esse campo e usado para adaptar semanticamente a classificacao dos alvos dos feedbacks. O usuario pode ainda limitar a quantidade de registros processados, o que facilita testes com bases grandes.

Apos o envio, a aplicacao cria um job de processamento (`ProcessingJob`) e passa a acompanhar seu status. A interface exibe a fase atual, o percentual de progresso, os eventos gerados pelo pipeline, avisos de leitura do arquivo e uma previa dos feedbacks ja processados.

## Fluxo detalhado do pipeline

O pipeline FEED-ON e organizado em etapas registradas no campo `metadata` do job. Essas etapas aparecem na interface como uma linha do tempo do processamento. No codigo, as etapas principais sao:

1. `Upload recebido`
2. `Leitura e validacao do arquivo`
3. `Analise IA / fallback local`
4. `Instanciacao FEED-ON`
5. `Reasoner ontologico`
6. `Exportacao manual Jira`
7. `Finalizacao`

### 1. Recebimento do upload e criacao do job

A requisicao de upload e tratada pela funcao `create_job`, em `pipeline/views.py`. Essa funcao valida se o arquivo tem formato aceito, verifica o tamanho maximo permitido, interpreta o limite de linhas informado pelo usuario, normaliza o nome do dominio e cria um registro `ProcessingJob`.

O modelo `ProcessingJob` armazena informacoes do lote processado, como nome original do arquivo, arquivo enviado, dominio, status, total de linhas, linhas processadas, limite de linhas, quantidade de tickets Jira criados, fase atual, mensagens de erro, metadados e datas de inicio e termino.

Depois de criado, o job e enviado ao processamento. Quando Celery esta configurado para execucao real, a tarefa e despachada para o worker. Quando a configuracao local usa `CELERY_TASK_ALWAYS_EAGER=true`, a aplicacao inicia uma thread local para processar o job sem depender de Redis.

### 2. Leitura e validacao do arquivo

A leitura e feita pelo servico `pipeline/services/csv_reader.py`. Apesar do nome do arquivo, o leitor aceita tanto CSV quanto planilhas Excel.

Para arquivos CSV, o sistema detecta automaticamente o separador entre virgula, ponto e virgula, tabulacao e pipe. Para planilhas Excel, o sistema usa `openpyxl`, abre a primeira aba ativa e procura uma linha de cabecalho reconhecida.

As colunas de texto reconhecidas incluem nomes como `text`, `feedback`, `comment`, `content`, `review`, `description`, `message`, `texto` e `conteudo`. Tambem sao reconhecidas colunas de identificador, alvo tecnico e intencao, como `id`, `feedback_id`, `target`, `technical_target`, `intent` e `intention`.

Quando a planilha nao possui cabecalho reconhecido, a aplicacao analisa as primeiras linhas e infere qual coluna contem maior conteudo textual. Isso permite processar bases pouco padronizadas, como planilhas exportadas manualmente. Linhas vazias ou sem texto sao ignoradas e registradas como avisos do job.

O resultado dessa etapa e uma sequencia de objetos internos `CsvFeedback`, contendo:

- `source_id`: identificador do feedback, extraido do arquivo ou gerado pela posicao da linha;
- `text`: texto original do feedback;
- `target`: alvo tecnico informado no arquivo, quando existir;
- `intent`: intencao informada no arquivo, quando existir.

### 3. Preparacao do lexico de dominio

Antes da analise individual dos feedbacks, o pipeline prepara um lexico especifico para o dominio informado. Essa etapa ocorre na funcao `_prepare_domain_lexicon`, em `pipeline/services/processor.py`.

O modelo `DomainLexicon` persiste termos associados a quatro categorias alinhadas a FEED-ON:

- `UIElement`;
- `QualityAttribute`;
- `Requirement`;
- `Process`.

Se o dominio informado ainda nao existir no banco, a aplicacao chama o servico `generate_domain_keywords`, em `pipeline/services/llm.py`, para gerar termos e jargoes em portugues brasileiro relacionados ao dominio. Quando a chave da OpenAI esta configurada, a geracao e feita por IA. Caso contrario, o sistema usa um lexico geral de fallback para manter o pipeline operacional.

Se o dominio ja existir, o lexico salvo e reutilizado. Opcionalmente, a variavel `FEED_ON_LEXICON_REFRESH_EXISTING=true` permite enriquecer automaticamente dominios ja conhecidos, combinando termos antigos e novos sem duplicatas.

Esse mecanismo e importante porque permite que a aplicacao adapte a classificacao dos alvos sem alterar o codigo-fonte para cada novo dominio de software.

### 4. Analise semantica por IA ou fallback local

A analise semantica individual dos feedbacks ocorre em `pipeline/services/nlp.py`. O pipeline processa os feedbacks em lotes, de acordo com `FEEDBACK_CHUNK_SIZE`.

Quando a API da OpenAI esta disponivel, a funcao `extract_feedback_semantics_batch` envia os textos para analise e espera uma resposta estruturada com:

- intencao bruta: `Report` ou `Suggestion`;
- escore de sentimento entre `-1.0` e `1.0`;
- alvo candidato mencionado no feedback.

Em seguida, a aplicacao mapeia a intencao bruta para uma intencao da FEED-ON. A regra implementada e:

```text
Report com sentimento menor que -0.5 -> Intention_BugReport
Demais casos -> Intention_Suggestion
```

Se o arquivo enviado ja possuir uma coluna de intencao, o sistema aproveita essa informacao e marca o provedor da analise como `csv`. Se a API da OpenAI falhar ou nao estiver configurada, a aplicacao usa um fallback local baseado em padroes textuais. Esse fallback identifica sinais de erro, falha, bug, travamento, sugestao, melhoria, urgencia e prioridade.

O resultado dessa etapa e salvo no modelo `FeedbackRecord`, nos campos:

- `intent`: intencao FEED-ON;
- `ai_intent`: intencao bruta extraida;
- `sentiment_score`: escore de sentimento;
- `ai_provider`: origem da analise, como `openai`, `csv` ou `local`;
- `target_candidate`: alvo candidato indicado pela IA;
- `technical_target`: alvo tecnico inicial, vindo do arquivo ou inferido.

### 5. Classificacao ontologica do alvo

A classificacao do alvo e realizada no servico `pipeline/services/ontology.py`, especialmente pela funcao `classify_target`. O texto do feedback e comparado com os termos do lexico de dominio persistido. O sistema busca correspondencias com expressoes regulares, considerando tambem uma versao sem acentos do texto.

Quando encontra um termo do lexico, o sistema retorna a classe ontologica correspondente e cria um nome de individuo. Por exemplo, um feedback que menciona `botao` pode ser classificado como `UIElement.Botao`. Quando nenhum termo e encontrado, o fallback e `Feature.General`.

Essa etapa evidencia a funcao do lexico de dominio: aproximar termos usados pelos usuarios comuns das categorias formais da ontologia FEED-ON.

### 6. Instanciacao da ontologia FEED-ON

Depois de extrair intencao, sentimento e alvo, o pipeline instancia os resultados na ontologia OWL. Essa responsabilidade fica no servico `FeedOnOntologyService`.

Para cada feedback, o metodo `interpret` recebe o identificador, texto, intencao, alvo tecnico, sentimento, provedor da IA e dominio. Em seguida, o servico:

- classifica o alvo com base no lexico do dominio;
- deriva a consequencia esperada;
- cria ou reutiliza classes e propriedades OWL quando necessario;
- cria individuos para feedback, alvo, intencao, sentimento, consequencia, agente e tecnica de elicitacao;
- estabelece relacoes entre esses individuos.

As principais relacoes instanciadas sao:

```text
Feedback hasIntention Intention
Feedback hasSentiment Sentiment
Feedback refersTo Target
Feedback indicates ConsequenceExpected
ConsequenceExpected aimsToEvolve Target
Feedback isProvidedBy ExternalAgent
Feedback isElicitedThrough ExplicitFeedbackElicitationTechnique
```

A consequencia esperada e derivada principalmente da intencao e do sentimento. Quando o sentimento e menor ou igual a `-0.5`, a consequencia tende a ser `Correction`. Nos demais casos, a consequencia padrao e `Improvement`. O codigo tambem possui suporte a `Prioritization` para casos marcados por termos como urgencia, prioridade ou criticidade, especialmente quando o sentimento nao esta disponivel.

### 7. Execucao do reasoner ontologico

Depois de instanciar um lote de feedbacks, o pipeline tenta executar o reasoner Pellet por meio do Owlready2, quando `FEED_ON_RUN_REASONER=true` e a ontologia foi carregada corretamente.

O objetivo dessa etapa e enriquecer as inferencias, especialmente em relacoes como `partOf`. Assim, um feedback associado a um elemento especifico de interface ou requisito pode ser relacionado a uma feature mais ampla. Caso o reasoner falhe, a aplicacao registra um aviso e mantem as inferencias deterministicamente obtidas, sem interromper o processamento.

### 8. Persistencia dos resultados

Ao final de cada lote, os registros sao persistidos na tabela `FeedbackRecord`. Cada registro armazena o texto original e os resultados semanticos extraidos. O job tambem atualiza o numero de linhas processadas e a fase atual.

O pipeline registra eventos em `PipelineEvent`, incluindo avisos sobre ausencia de cabecalho, ausencia de colunas de ID, alvo ou intencao, criacao ou reutilizacao do lexico, execucao do reasoner e finalizacao do processamento.

### 9. Dashboard, filtros e exportacoes

Com o job concluido, a aplicacao disponibiliza os resultados no dashboard. O dashboard permite selecionar o job analisado e aplicar filtros por sentimento e consequencia.

Os indicadores exibidos incluem:

- total de feedbacks analisados;
- percentual de sentimentos negativos;
- quantidade de tickets Jira gerados ou simulados;
- distribuicao percentual de consequencias;
- top features criticas associadas a `Correction`;
- media de sentimento por categoria;
- tabela de feedbacks disponiveis para exportacao.

A aplicacao tambem fornece uma tela de resultados detalhados, com paginacao, contendo o texto original, sentimento, alvo da IA, alvo inferido, consequencia e link Jira quando houver.

Os resultados podem ser exportados em CSV e em DOCX. A exportacao DOCX gera um relatorio executivo com indicadores globais, distribuicao de consequencias, features criticas, media de sentimento por categoria e top 10 issues criticos.

### 10. Exportacao manual para Jira

A integracao com Jira e feita de forma manual no dashboard. O usuario configura a URL do Jira, e-mail, token de API e chave do projeto. Depois, seleciona os feedbacks que deseja transformar em itens de backlog.

Cada item exportado inclui o texto original do feedback e sua classificacao FEED-ON, incluindo alvo inferido e consequencia. Por seguranca, a configuracao padrao usa `JIRA_DRY_RUN=true`, simulando a criacao dos tickets sem enviar dados reais ao Jira. Quando a exportacao real e habilitada, a aplicacao cria issues no projeto configurado e grava a chave Jira no registro do feedback e, quando possivel, na ontologia.

## Experimento com feedbacks do EMES

No banco local ha um processamento real do arquivo `Feedbacks EMES usado no experimento.xlsx`. Esse processamento foi registrado como job `#23`.

Dados do processamento:

| Campo | Valor |
|---|---|
| Job | `#23` |
| Arquivo | `Feedbacks EMES usado no experimento.xlsx` |
| Dominio informado | `academico` |
| Status | `completed` |
| Data de criacao | `11/06/2026 12:26:31` |
| Data de termino | `11/06/2026 12:27:13` |
| Feedbacks validos | `51` |
| Feedbacks processados | `51` |
| Provedor da analise semantica | `openai` |
| Tickets Jira criados | `0` |

A planilha processada nao possuia cabecalho reconhecido. Por isso, o FEED-ON inferiu automaticamente que a segunda coluna era a coluna textual. Como tambem nao havia colunas reconhecidas de ID, alvo tecnico ou intencao, o sistema usou o numero da linha como identificador e inferiu os demais atributos.

Durante o processamento, o sistema criou o lexico do dominio `academico`. O lexico gerado incluiu termos como:

- `UIElement`: menu, botao, tela de login, pagina inicial, notificacoes, tabela de notas, perfil do usuario, ferramenta de busca, formulario de cadastro, graficos de desempenho;
- `QualityAttribute`: usabilidade, desempenho, estabilidade, seguranca, acessibilidade, experiencia do usuario, atualizacoes, compatibilidade, design, suporte tecnico;
- `Requirement`: cadastro facil, menu intuitivo, relatorios claros, notificacoes em tempo real, acesso offline, integracao com redes sociais, personalizacao de perfil, feedback de desempenho, historico de atividades, suporte a multiplos idiomas;
- `Process`: cadastro de usuario, login, navegacao, atualizacao de dados, consulta de notas, geracao de relatorios, solicitacao de suporte, feedback do usuario, publicacao de conteudo, avaliacao de cursos.

## Resultados do processamento EMES

Os 51 feedbacks foram classificados em duas consequencias principais:

| Consequencia | Quantidade | Percentual |
|---|---:|---:|
| `Improvement` | 33 | 64,7% |
| `Correction` | 18 | 35,3% |
| `Prioritization` | 0 | 0,0% |

Quanto ao sentimento:

| Categoria | Quantidade |
|---|---:|
| Negativo | 18 |
| Neutro | 0 |
| Positivo | 33 |

O sentimento medio geral foi aproximadamente `0,088`, indicando um conjunto levemente positivo, embora mais de um terco dos registros tenha sido classificado como demanda de correcao.

Quanto aos alvos inferidos:

| Alvo inferido | Quantidade |
|---|---:|
| `Feature.General` | 48 |
| `UIElement.Botao` | 2 |
| `QualityAttribute.Seguranca` | 1 |

Esse resultado mostra que a aplicacao conseguiu classificar todos os feedbacks quanto a intencao, sentimento e consequencia, mas a granularidade dos alvos ontologicos ficou concentrada em `Feature.General`. Isso sugere que, para dominios especificos como o sistema EMES, a qualidade da inferencia de alvo pode ser melhorada com enriquecimento do lexico de dominio, incluindo termos mais proximos do vocabulario real usado nos feedbacks, como inscricao, certificado, cronograma, carga horaria, instrutores, agendamento de salas, curso, eventos e planilha de lancamento.

Exemplos de classificacao gerados pelo FEED-ON:

| ID | Feedback resumido | Intencao IA | Sentimento | Alvo candidato | Alvo inferido | Consequencia |
|---|---|---|---:|---|---|---|
| 1 | Na inscricao, nao foi possivel cadastrar o setor. | `Report` | -0,80 | Inscricao | `Feature.General` | `Correction` |
| 2 | No formulario de frequencia, tambem nao foi possivel cadastrar o setor. | `Report` | -0,80 | Formulario de Frequencia | `Feature.General` | `Correction` |
| 3 | Nao foi possivel visualizar os modelos de certificado. | `Report` | -0,90 | Modelo de Certificado | `Feature.General` | `Correction` |
| 4 | Demanda de agendamento de salas: colocar capacidade de cada sala. | `Suggestion` | 0,70 | Agendamento de Salas | `Feature.General` | `Improvement` |
| 5 | Ver possibilidade de eliminar o 00 da carga horaria. | `Suggestion` | 0,50 | Carga Horaria | `Feature.General` | `Improvement` |
| 8 | Na tela curso/eventos, nao ha um botao cancelar para voltar a pagina anterior. | `Suggestion` | 0,60 | Tela de Curso/Eventos | `UIElement.Botao` | `Improvement` |

## Artefatos produzidos pela aplicacao

Ao final de um processamento, o FEED-ON produz diferentes artefatos:

- registros estruturados no banco de dados, por meio do modelo `FeedbackRecord`;
- eventos de acompanhamento do pipeline, por meio do modelo `PipelineEvent`;
- individuos OWL instanciados na ontologia FEED-ON;
- dashboard analitico com graficos e indicadores;
- tabela detalhada de resultados;
- exportacao CSV;
- relatorio executivo DOCX;
- possibilidade de criacao manual de itens no Jira.

No caso do job EMES analisado, nenhum ticket Jira real foi criado. Os registros permaneceram com status pendente de exportacao, o que e coerente com o uso experimental da aplicacao.

## Interpretacao para o artigo

O FEED-ON pode ser descrito no artigo como um pipeline de enriquecimento semantico de feedback textual orientado por ontologia. A aplicacao atua como uma prova de conceito operacional da ontologia, demonstrando como comentarios livres de usuarios podem ser convertidos em representacoes estruturadas e analisaveis.

O papel da IA no fluxo e duplo. Primeiro, ela apoia a adaptacao de dominio, gerando um lexico inicial para categorias ontologicas relevantes. Segundo, ela realiza a extracao semantica dos feedbacks, identificando intencao, sentimento e alvo candidato. A ontologia FEED-ON atua como camada de representacao formal, organizando esses resultados em individuos, classes e relacoes. O reasoner ontologico, quando executado com sucesso, permite enriquecer as associacoes entre alvos especificos e features mais amplas.

O experimento com os feedbacks do EMES mostra que a aplicacao conseguiu processar automaticamente a base de dados e produzir uma classificacao inicial util para analise. Os resultados tambem indicam uma limitacao relevante: a identificacao granular dos alvos depende da qualidade e abrangencia do lexico de dominio. Assim, uma etapa de curadoria ou enriquecimento do lexico pode aumentar a precisao da classificacao ontologica em usos futuros.

Em termos praticos, a aplicacao oferece suporte a um fluxo de engenharia de requisitos e gestao de backlog: coleta de feedbacks, classificacao semantica, identificacao de demandas de correcao ou melhoria, priorizacao visual por dashboard, exportacao de relatorios e transformacao manual de feedbacks em tarefas Jira.

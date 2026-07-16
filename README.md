# feed-on

Aplicacao web em Python/Django para transformar feedbacks de usuarios em tarefas rastreaveis no Jira usando a ontologia FEED-ON.

## Setup

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python manage.py migrate
python manage.py runserver
```

## Deploy no Railway

O projeto inclui `railway.toml` para o Railway instalar as dependencias com Nixpacks, executar `collectstatic`, aplicar migrations e iniciar o Django com Gunicorn usando a porta injetada em `PORT`. O arquivo `nixpacks.toml` adiciona `jdk17` ao ambiente para permitir a execucao do reasoner Pellet usado pelo Owlready2.

Configure no Railway:

```env
SECRET_KEY=gere-uma-chave-segura
DEBUG=false
GOOGLE_CLIENT_ID=client-id-do-google
GOOGLE_CLIENT_SECRET=client-secret-do-google
OPENAI_API_KEY=sua_chave_aqui
JIRA_SERVER=https://your-domain.atlassian.net
JIRA_EMAIL=you@example.com
JIRA_API_TOKEN=...
JIRA_PROJECT_KEY=FEED
JIRA_DRY_RUN=true
FEED_ON_LEXICON_REFRESH_EXISTING=false
```

Se usar o plugin MySQL do Railway, o Django le automaticamente `MYSQL_URL` ou as variaveis `MYSQLHOST`, `MYSQLPORT`, `MYSQLUSER`, `MYSQLPASSWORD` e `MYSQLDATABASE`. Se usar Redis no Railway, `REDIS_URL` tambem e reconhecida automaticamente para Celery.

`RAILWAY_PUBLIC_DOMAIN` e usado para preencher `ALLOWED_HOSTS` e `CSRF_TRUSTED_ORIGINS` automaticamente. Se preferir configurar manualmente, use:

```env
ALLOWED_HOSTS=seu-dominio.up.railway.app
CSRF_TRUSTED_ORIGINS=https://seu-dominio.up.railway.app
```

Para processamento assincrono real, crie um segundo servico no Railway apontando para o mesmo repositorio com o start command:

```bash
celery -A feed_on worker -l info
```

Sem esse worker, configure `CELERY_TASK_ALWAYS_EAGER=true` para executar as tarefas no proprio processo web.

### Login com Google

Crie uma credencial OAuth no Google Cloud Console para uma aplicacao web e adicione a URL de callback:

```text
https://seu-dominio.up.railway.app/accounts/google/login/callback/
```

Depois configure `GOOGLE_CLIENT_ID` e `GOOGLE_CLIENT_SECRET` no Railway. O sistema usa a conta Google para separar jobs, uploads, dashboards e exportacoes por usuario.

Para processamento assincrono, suba Redis e rode um worker:

```powershell
celery -A feed_on worker -l info -P solo
```

No Windows, `-P solo` evita problemas comuns do pool prefork.

## MySQL

Configure o banco definido no `.env`:

```sql
CREATE DATABASE feed_on CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'feed_on'@'localhost' IDENTIFIED BY 'feed_on_password';
GRANT ALL PRIVILEGES ON feed_on.* TO 'feed_on'@'localhost';
FLUSH PRIVILEGES;
```

## Ontologia

Coloque preferencialmente o arquivo em `ontology/FEED-ON.owl`, ou ajuste `FEED_ON_ONTOLOGY_PATH` no `.env`. O `.ofn` tambem pode ficar na pasta como referencia.

O servico tenta carregar o arquivo com Owlready2 e executar Pellet. Em producao no Railway, o Java e instalado via `nixpacks.toml` (`jdk17`). Caso o arquivo ainda nao exista, o formato nao seja aceito pelo loader local ou Java/Pellet falhe, o job registra um aviso e usa inferencia deterministica de fallback para manter o fluxo de upload, classificacao e Jira funcionando.

Se o `.ofn` nao carregar no Owlready2 da sua instalacao, exporte tambem uma versao `.owl` RDF/XML ou OWL/XML e aponte `FEED_ON_ONTOLOGY_PATH` para ela.

### Lexico dinamico por dominio

No upload, informe o dominio do software analisado, por exemplo `delivery`, `gestao escolar` ou `telemedicina`. O pipeline normaliza esse valor e consulta a tabela `DomainLexicon`.

Se o dominio ainda nao existir, o FEED-ON chama a OpenAI para gerar termos em PT-BR nas categorias ontologicas `UIElement`, `QualityAttribute`, `Requirement` e `Process`, salva os termos no banco e usa esse lexico para classificar os alvos dos feedbacks. Se a OpenAI nao estiver disponivel, o job usa um lexico geral de fallback para nao interromper o processamento.

Por padrao, dominios ja conhecidos reutilizam o lexico persistido. Para enriquecer automaticamente dominios existentes a cada novo upload, configure:

```env
FEED_ON_LEXICON_REFRESH_EXISTING=true
```

Em producao no Railway, mantenha `OPENAI_API_KEY`, execute as migrations e deixe `FEED_ON_LEXICON_REFRESH_EXISTING=false` caso queira evitar custo/latencia repetidos em dominios ja aprendidos.

## Arquivo de feedback esperado

```csv
id,text
1,"O botao salvar nao funciona na tela de pedidos"
2,"Seria bom filtrar os resultados por data"
```

O upload aceita arquivos `.csv`, `.xlsx` e `.xlsm`. Para planilhas Excel, o pipeline le a primeira aba ativa e usa a primeira linha nao vazia como cabecalho quando ela tiver nomes reconhecidos. Se a planilha nao tiver cabecalho, a coluna com maior conteudo textual e inferida automaticamente como texto do feedback. Tambem sao aceitas colunas como `feedback_id`, `reviewId`, `comment`, `content`, `review`, `description`, `message`, `target`, `intent` e `technical_target`. O leitor detecta automaticamente CSV separado por virgula, ponto e virgula, tab ou pipe.


## OpenAI

### Fluxo GPT para intencao FEED-ON

Quando `OPENAI_API_KEY` estiver configurada, a etapa `AI-based Processing` chama o GPT pelo SDK oficial `openai` antes de instanciar a ontologia. A resposta esperada e JSON estruturado com:

- `sentiment`: numero entre `-1` e `1`.
- `intention`: `Report` ou `Suggestion`.
- `target_candidate`: componente mencionado, por exemplo `Login` ou `Video Player`.

A aplicacao aplica a heuristica:

- `Report` com `sentiment < -0.5` vira `Intention_BugReport`.
- `Suggestion` ou `sentiment >= 0` vira `Intention_Suggestion`.

Esses valores sao salvos em `FeedbackRecord` junto com `ai_intent`, `sentiment_score`, `target_candidate` e `ai_provider`, e sao usados na instanciacao FEED-ON. Se a API falhar ou a chave nao estiver configurada, o pipeline usa fallback local e registra `ai_provider=local`.
A chave da API deve ficar somente no `.env`, nunca direto no codigo:

```env
OPENAI_API_KEY='sua_chave_aqui'
```

O Django carrega essa variavel em `feed_on/settings.py` com `python-dotenv` e disponibiliza o valor em `settings.OPENAI_API_KEY`.

## Jira

Por seguranca, `JIRA_DRY_RUN=true` vem habilitado. Quando estiver pronto para criar issues reais, configure:

```env
JIRA_DRY_RUN=false
JIRA_URL=https://your-domain.atlassian.net
JIRA_EMAIL=you@example.com
JIRA_API_TOKEN=...
JIRA_PROJECT_KEY=FEED
```



## Processamento parcial e cancelamento

A tela possui um campo `Quantidade para processar`, preenchido com `1000` por padrao para testes com datasets grandes. Deixe vazio para processar o arquivo inteiro.

Durante um job ativo, use `Cancelar processamento` para marcar o job como cancelado. O worker verifica esse sinal entre registros, chunks, reasoner e envio ao Jira. Se um processamento antigo tiver sido iniciado antes desta funcionalidade, reinicie o servidor para interromper a thread antiga.

Na tela de upload, a lista de jobs recentes permite abrir diretamente o dashboard filtrado para um job especifico. A mesma area possui a acao `Limpar falhas`, que remove em lote os jobs do usuario autenticado que terminaram com status `failed`.

## Relatorio executivo

A exportacao DOCX gera um relatorio executivo com tabelas para indicadores globais, distribuicao de consequencias, features criticas, sentimento por categoria e Top 10 Critical Issues. Os percentuais de consequencia sao calculados por contagem real sobre o total de feedbacks analisados no relatorio. Antes de inserir o texto original nas tabelas, o sistema sanitiza residuos de parsing como colchetes, aspas duplicadas e delimitadores. Feedbacks ainda nao exportados ao Jira aparecem como `Pendente de Exportacao`.




No Jira, Correction vira issue type configurado em JIRA_BUG_ISSUE_TYPE (Bug por padrao). Improvement vira JIRA_IMPROVEMENT_ISSUE_TYPE (Story por padrao).

# Modelo operacional FEED-ON

A aplicacao usa `ontology/FEED-ON-v0.2.owl` como ontologia de referencia versionada e cria apenas individuos operacionais em uma copia por job, em `results/job_<id>/FEED-ON-job-<id>-instantiated.owl`. O pipeline e: ingestao CSV/XLSX/XLSM, pseudonimizacao, analise OpenAI ou local, resolucao de alvos, derivacao independente de consequencias, instanciacao OWL, Pellet opcional e preparacao manual para Jira.

Um feedback pode possuir varios `FeedbackTarget` e `FeedbackConsequence`; `inferred_target` e `consequence` permanecem temporariamente como espelhos do item principal para compatibilidade. A resolucao prioriza alvo tecnico, candidato do LLM, texto e lexicos; `Feature.General` somente aparece sem evidencias especificas. Correction, Improvement e Prioritization nao sao exclusivas.

A extracao `feed-on-semantic-v2-target-typing` separa resolucao nominal de tipagem ontologica. Tipos sem evidencia permanecem `Target`; sinonimos PT/EN sao canonicalizados pelo mapa `feed-on-target-map-v1` antes da contagem de hotspots. `Intention_BugReport` implica Correction sem bloquear Improvement ou Prioritization.

Identificadores de pessoas sao normalizados e convertidos em hash com `FEED_ON_AGENT_HASH_SALT`; somente pseudonimos por job sao expostos. Papeis desconhecidos permanecem `Agent`, sem inferencia automatica de agente externo. `ai_provider` descreve quem analisou; `elicitation_technique` descreve a origem do feedback. Contexto de ocorrencia somente e criado quando a fonte fornece dados reais.

O `ProcessingJob.metadata` registra ontologia, reasoner e metricas do experimento sem chaves, tokens, nomes ou e-mails. Pellet usa o `World`/ontologia carregado e `FEED_ON_REASONER_FAIL_FAST=false` preserva resultados deterministicos em falhas.

Cada job concluido grava `experimental-manifest.json`, `owl-assertion-audit.json` e o OWL instanciado em `results/job_<id>/`. A auditoria separa as assercoes diretas (snapshot anterior ao Pellet) das inferidas (diferenca posterior ao Pellet); valores literais aparecem apenas como hashes SHA-256.

## Testes e novo experimento

Execute `python manage.py test pipeline` e `python manage.py check`. Para um experimento, configure `.env`, aplique `python manage.py migrate`, confirme Jira dry-run, envie um dataset anonimizado pela interface e preserve o manifesto e o OWL gerados. Consulte `docs/EXPERIMENT_EXECUTION.md`.

Limitacoes: sinonimos dependem do lexico congelado; papeis e contexto nao sao inferidos sem metadados; classificacoes automaticas exigem validacao humana; Pellet requer Java compativel.

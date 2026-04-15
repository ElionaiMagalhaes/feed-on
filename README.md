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

O servico tenta carregar o arquivo com Owlready2 e executar Pellet. Caso o arquivo ainda nao exista, o formato nao seja aceito pelo loader local ou Java/Pellet falhe, o job registra um aviso e usa inferencia deterministica de fallback para manter o fluxo de upload, classificacao e Jira funcionando.

Se o `.ofn` nao carregar no Owlready2 da sua instalacao, exporte tambem uma versao `.owl` RDF/XML ou OWL/XML e aponte `FEED_ON_ONTOLOGY_PATH` para ela.

## CSV esperado

```csv
id,text
1,"O botao salvar nao funciona na tela de pedidos"
2,"Seria bom filtrar os resultados por data"
```

Tambem sao aceitas colunas como `feedback_id`, `reviewId`, `comment`, `content`, `review`, `description`, `message`, `target`, `intent` e `technical_target`. O leitor detecta automaticamente CSV separado por virgula, ponto e virgula, tab ou pipe.


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

A tela possui um campo `Quantidade para processar`, preenchido com `1000` por padrao para testes com datasets grandes. Deixe vazio para processar o CSV inteiro.

Durante um job ativo, use `Cancelar processamento` para marcar o job como cancelado. O worker verifica esse sinal entre registros, chunks, reasoner e envio ao Jira. Se um processamento antigo tiver sido iniciado antes desta funcionalidade, reinicie o servidor para interromper a thread antiga.




No Jira, Correction vira issue type configurado em JIRA_BUG_ISSUE_TYPE (Bug por padrao). Improvement vira JIRA_IMPROVEMENT_ISSUE_TYPE (Story por padrao).


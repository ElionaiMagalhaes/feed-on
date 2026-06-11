# Changelog

## 2026-06-11

- Adicionado suporte a upload de arquivos `.xlsx` e `.xlsm`, alem de `.csv`.
- Adicionada inferencia automatica da coluna de texto em planilhas Excel sem cabecalho reconhecido.
- Adicionado botao explicito `Selecionar arquivo` na tela de upload, mantendo drag-and-drop.
- Adicionado campo `Dominio do software` no upload para orientar a adaptacao semantica do pipeline.
- Criado modelo `DomainLexicon` para persistir o lexico de dominio usado pela ontologia FEED-ON.
- Criado servico `pipeline/services/llm.py` para gerar termos de dominio via OpenAI em PT-BR nas categorias `UIElement`, `QualityAttribute`, `Requirement` e `Process`.
- Refatorada a classificacao de alvos em `ontology.py` para consultar o lexico persistido em banco, removendo o mapa estatico de palavras-chave.
- Adicionada migration `0006_processingjob_domainlexicon.py` com `ProcessingJob.domain_name` e tabela `DomainLexicon`.
- Adicionada variavel `FEED_ON_LEXICON_REFRESH_EXISTING` para controlar enriquecimento automatico de dominios ja conhecidos.
- Atualizados `README.md`, `.env.example`, `requirements.txt` e `explicacao.md` para o novo fluxo.
- Atualizada a tela de upload para permitir limpar jobs com falha e abrir diretamente o dashboard de um job recente.
- Refatorado o relatorio executivo DOCX para usar tabelas, corrigir percentuais por contagem real de consequencias, sanitizar textos de feedback e exibir status Jira como `Pendente de Exportacao` quando ainda nao houver chave gerada.
- Adicionado `nixpacks.toml` com `jdk17` para disponibilizar Java no Railway e permitir a execucao do reasoner Pellet/Owlready2 em producao.

# Investigação técnica — uso de Ministral 3-8B / 14B no tradutor

## Evidências atuais no repositório
- Já existe integração OpenAI-compatible para tradução (`TranslateGemmaTranslator`).
- Pipeline possui pontos seguros para inserir pós-edição (`translated_texts`) e QA final (`run_qa_scan`).
- QA já gera `work/qa_report.json`, o que facilita revisão por LLM.

## Oportunidades de robustez (incrementais)
1. **Pós-edição opcional por LLM (implementado):** revisar blocos traduzidos sem trocar provider principal.
2. **Resumo de QA com LLM (implementado):** adicionar diagnóstico textual no relatório sem alterar gates já existentes.
3. **Gate por score QA (já implementado):** combinar score heurístico + revisão LLM para triagem.
4. **Lista do-not-translate + LLM:** reforçar preservação de siglas/comandos durante pós-edição.
5. **Canary mode por página:** habilitar LLM só em páginas de maior risco (`top_risky_pages`).

## Viabilidade de Ministral 3-8B / 14B
- **NÃO CONSTA** integração explícita com um endpoint "Ministral" no projeto base.
- Viável via endpoint OpenAI-compatible (`/chat/completions`) com configuração:
  - `llm_assist.base_url`
  - `llm_assist.model`
- Recomendações:
  - **3-8B**: pós-edição de blocos curtos/médios e resumo QA (custo/latência menor).
  - **14B**: análise de QA mais rica e casos técnicos ambíguos (latência/custo maior).

## Modo seguro recomendado
- `llm_assist.enabled=false` por padrão.
- Ativar primeiro apenas:
  - `post_edit_enabled=true`
  - `post_edit_max_blocks_per_page` baixo (ex.: 10-30)
- Habilitar `qa_review_enabled` depois de validar estabilidade.

## Critérios de validação
- Não reduzir score de preservação de números/unidades/referências.
- Não aumentar incidência de `unchanged_ratio` anômalo.
- Comparar páginas críticas antes/depois (`top_risky_pages`).

# Auditoria Forense v2 — Tradução EN→PT-BR e Preservação de PDF

## 1) Mapa do pipeline (evidências)

| Etapa | Arquivos/Funções | Evidência exata | Leitura técnica |
|---|---|---|---|
| Entrada e orquestração | `app/cli.py::build_parser`, `app/pipeline.py::run_pipeline` | flags `--pdf`, `--render-mode`, `--qa-report`, `--qa-threshold`, `--audit-mode` + `run_pipeline(...)` | A CLI direciona todas as decisões para config/overrides e executa pipeline por página. |
| Detecção de tipo de página | `app/detect.py::detect_page_features` (consumido em `pipeline.py`) | uso de `PageType.NATIVE/HYBRID/SCANNED` | O tipo de página decide extração nativa e OCR de fallback/figuras. |
| Extração de texto nativo | `app/extract.py::extract_native_text_blocks` | `page.get_text("blocks")` + `page.get_text("words")` + `cover_rects` | Há segmentação com split para blocos esparsos e cobertura por linha/palavra. |
| OCR | `app/ocr.py::ocr_image_to_blocks` | `pytesseract.image_to_data`, `group_mode_scanned`, `group_mode_images` | OCR usa agrupamento diferenciado para scan e figuras, com filtros de ruído. |
| OCR em figuras | `app/pipeline.py` | `mask_out_rects_pt(...)` + `translate_images` | Em páginas híbridas, mascara texto nativo e roda OCR para captar texto dentro de imagens. |
| Tradução e cache | `app/translate.py::translate_many_with_cache`, `app/cache.py` | `provider`, `entity_mode`, `glossary`, `do_not_translate_terms` | Tradução por batch com cache e placeholders para proteção de termos críticos. |
| Pós-edição PT-BR | `app/translate.py::ptbr_postprocess` + `postprocess_translation` | `_PTBR_RULES` + normalização de pontuação | Ajustes determinísticos sem reformulação agressiva do texto técnico. |
| Assistência LLM opcional | `app/llm_assist.py`, `app/pipeline.py` | `llm_assist.enabled`, `post_edit_enabled`, `validate_post_edit_candidate` | Pós-edição opt-in com guard de números/unidades/referências/placeholders. |
| Render PDF | `app/render.py` | `create_translated_page_pdf_overlay`, `create_translated_page_pdf_overlay_original` | Dois modos de overlay, com cobertura e escolha de cor para legibilidade. |
| Preservação estrutural | `app/pipeline.py::_preserve_pdf_features` | `set_toc`, `set_page_labels`, `insert_link`, `set_metadata` | Mantém estrutura navegável do PDF original após merge. |
| QA e relatórios | `app/qa.py::run_qa_scan` | `qa_report.json`, `qa_report.txt`, `top_risky_pages`, `llm_review` | Scanner com score por página e opcional resumo LLM, sem alterar PDF. |

---

## 2) Achados auditáveis (30)

### A. Tradução e consistência terminológica

1. **Glossário simples ainda sem metadados de domínio/prioridade.**
   - Evidência: `load_glossary` aceita dict simples termo→termo.
   - Risco: Baixo | Impacto: 5
   - Melhoria: schema enriquecido (domain, priority, case_sensitive, lock).
   - Validação: testes de conflito de termos por domínio.

2. **`do_not_translate` existe e está integrado, porém sem níveis por contexto (CLI/código/marca).**
   - Evidência: `do_not_translate.yaml` lista plana; proteção por regex literal.
   - Risco: Baixo | Impacto: 4
   - Melhoria: permitir `category` e `scope` por entrada.
   - Validação: testes por categoria e priorização.

3. **Regras PT-BR são úteis, mas podem colidir com jargão em blocos muito técnicos.**
   - Evidência: `_PTBR_RULES` fixas em `translate.py`.
   - Risco: Médio | Impacto: 3
   - Melhoria: gate por densidade de tokens técnicos no bloco.
   - Validação: corpus técnico sintético sem alteração de termos bloqueados.

4. **Proteção de unidades foi ampliada (positivo), mas sem matriz de cobertura automatizada.**
   - Evidência: regex com `kV`, `mA`, `MPa`, `N·m`, etc.
   - Risco: Baixo | Impacto: 4
   - Melhoria: suíte parametrizada com 100+ padrões.
   - Validação: `pytest` parametrizado de roundtrip.

5. **Não há classificador de “texto não traduzível” por bloco (apenas regras).**
   - Evidência: ausência de função/classificador dedicado em `translate.py`.
   - Risco: Médio | Impacto: 4
   - Melhoria: heurística por razão alfanumérica + símbolos + comprimento.
   - Validação: benchmark em exemplos de comandos/códigos.

### B. Guardas de segurança semântica

6. **Guarda LLM já protege números/unidades/referências e ZXQ (ponto forte).**
   - Evidência: `validate_post_edit_candidate` em `app/llm_assist.py`.
   - Risco: Baixo | Impacto: 5
   - Próximo passo: incluir também preservação de `%`, `+/-`, ranges e datas como campos separados.
   - Validação: testes unitários específicos por categoria.

7. **Rejeições de pós-edição são registradas, mas sem score agregado de rejeição por página.**
   - Evidência: `llm_post_edit_rejected_reasons` no log.
   - Risco: Baixo | Impacto: 3
   - Melhoria: métrica `%rejected_post_edit` no QA final.
   - Validação: assert em `qa_report.json`.

8. **QA score atual não inclui métricas explícitas de divergência numérica do original/traduzido por bloco.**
   - Evidência: score atual baseado em status/warnings/errors/high_unchanged.
   - Risco: Médio | Impacto: 5
   - Melhoria: `numeric_diff_guard` no QA com severidade dedicada.
   - Validação: cenários com número alterado devem elevar score.

9. **Sem detector específico para inversão semântica curta (ex.: negação).**
   - Evidência: não há regra para `not/no/without` vs `não/sem`.
   - Risco: Médio | Impacto: 4
   - Melhoria: checklist lexical de negação/comparativos.
   - Validação: casos sintéticos com frases de manutenção/segurança.

10. **`qa_fail_score_threshold` existe (positivo), mas não há perfil default por tipo de documento.**
    - Evidência: config única no `pipeline`.
    - Risco: Baixo | Impacto: 3
    - Melhoria: presets `manual_tecnico`, `catálogo`, `procedimento`.
    - Validação: regressão com docs de amostra.

### C. Segmentação (principal risco de qualidade)

11. **Extração nativa já divide blocos esparsos, mas sem limite de fragmentação global por página.**
    - Evidência: `split_sparse_blocks` por bloco em `extract.py`.
    - Risco: Médio | Impacto: 5
    - Melhoria: meta `short_block_ratio` com merge posterior.
    - Validação: métrica por página antes/depois.

12. **OCR line/paragraph está bem configurado, porém falta heurística explícita para tabela em OCR.**
    - Evidência: `group_mode` só line/paragraph.
    - Risco: Médio | Impacto: 5
    - Melhoria: detector `table_like` por colunas recorrentes em X.
    - Validação: teste com tabela sintética OCR.

13. **Chunking textual ainda depende de pontuação geral, sem awareness de listas técnicas.**
    - Evidência: `_SENT_SPLIT` em `translate.py`.
    - Risco: Médio | Impacto: 4
    - Melhoria: split especial para itens numerados e bullets.
    - Validação: preservar estrutura de lista no output.

14. **Retry de unchanged é útil, mas sem ranking de candidatos por “traduzibilidade”.**
    - Evidência: critérios atuais em `pipeline.py`.
    - Risco: Baixo | Impacto: 3
    - Melhoria: score de prioridade (tamanho + alfabético + penalidade técnica).
    - Validação: maior taxa de changed com baixa regressão.

15. **TOC/leader dots tem proteção regex, mas sem estágio dedicado de reconstrução de alinhamento.**
    - Evidência: `_LEADER_DOTS_PATTERN`.
    - Risco: Médio | Impacto: 4
    - Melhoria: pós-process TOC preservando coluna de página.
    - Validação: testes com `..... 123` e níveis hierárquicos.

### D. PDF visual (layout, imagem, tabela)

16. **`pdf_overlay_original` preserva vetorial (ponto forte), porém sem recomendação automática por tipo de página.**
    - Evidência: escolha é manual por `render.mode`.
    - Risco: Baixo | Impacto: 4
    - Melhoria: auto-switch para `pdf_overlay_original` quando possível.
    - Validação: comparação de tamanho e nitidez.

17. **Covers têm opacidade/config detalhada, mas sem detector de overflow textual após insert_textbox.**
    - Evidência: `_fit_textbox` reduz fonte, sem relatório de corte residual.
    - Risco: Médio | Impacto: 4
    - Melhoria: registrar blocos com `rc < 0` na tentativa final.
    - Validação: flag no log + QA score.

18. **Imagens estão protegidas via fundo original/raster, porém sem teste de regressão de DPI/compressão.**
    - Evidência: não há teste dedicado em `tests/`.
    - Risco: Médio | Impacto: 3
    - Melhoria: teste de tamanho/qualidade com fixture de imagem.
    - Validação: limite máximo de degradação e tamanho.

19. **Sem fallback formal para tabela longa (manter original + nota).**
    - Evidência: inexistência de modo tabela dedicado.
    - Risco: Médio | Impacto: 5
    - Melhoria: fallback por região table-like quando overflow alto.
    - Validação: nenhuma sobreposição em tabela crítica.

20. **Preservação de links/TOC está best-effort, mas faltam métricas comparativas entrada/saída.**
    - Evidência: `_preserve_pdf_features` com `try/except` silencioso.
    - Risco: Médio | Impacto: 4
    - Melhoria: relatório de contagem de links/bookmarks preservados.
    - Validação: assert de cobertura mínima estrutural.

### E. Operação, robustez e DX

21. **Sem lint formal configurado (NÃO CONSTA).**
    - Evidência: `requirements-dev.txt` contém apenas `pytest`; busca por ruff/flake8 não retorna.
    - Risco: Baixo | Impacto: 3
    - Melhoria: adicionar `ruff` mínimo.
    - Validação: `ruff check app tests` no CI.

22. **Testes unitários evoluíram (ponto forte), porém faltam testes de integração com PDF real de 2–3 páginas.**
    - Evidência: testes atuais focam unidades.
    - Risco: Médio | Impacto: 4
    - Melhoria: E2E curto acionando pipeline completo.
    - Validação: gerar PDF de saída e QA report em teste de smoke.

23. **Script PowerShell está rico em parâmetros (ponto forte), mas sem perfil pronto para 500+ páginas com presets.**
    - Evidência: muitos parâmetros manuais em `setup_and_translate_windows.ps1`.
    - Risco: Baixo | Impacto: 3
    - Melhoria: presets `fast`, `balanced`, `quality`.
    - Validação: execução bem-sucedida por preset.

24. **Ausência de versionamento explícito do esquema de `qa_report.json`.**
    - Evidência: report sem campo `schema_version`.
    - Risco: Baixo | Impacto: 3
    - Melhoria: adicionar `schema_version` para compatibilidade futura.
    - Validação: teste de contrato JSON.

25. **Não há comando único “audit mode full” documentado para QA + logs + limites.**
    - Evidência: flags existem, mas sem macro oficial.
    - Risco: Baixo | Impacto: 2
    - Melhoria: script `run_audit.ps1`.
    - Validação: gera pacote com logs e relatório consolidado.

26. **LLM assist usa endpoint OpenAI-compatible (bom), mas sem timeout/retry específico por tipo de chamada.**
    - Evidência: `_chat` sem retry/backoff dedicado.
    - Risco: Médio | Impacto: 3
    - Melhoria: retry exponencial para 429/5xx.
    - Validação: teste com mock de falha transitória.

27. **Resumo LLM de QA é best-effort (bom), mas sem “source-citation” interna dos itens críticos.**
    - Evidência: `llm_review` contém resumo/ações/confidence.
    - Risco: Baixo | Impacto: 2
    - Melhoria: incluir IDs de páginas/blocos citados.
    - Validação: checar referências no JSON.

28. **Cache version existe (positivo), mas não inclui explicitamente versão do schema QA/LLM.**
    - Evidência: `cache_version` em `config.yaml`.
    - Risco: Baixo | Impacto: 2
    - Melhoria: separar `pipeline_version`, `qa_schema_version`.
    - Validação: invalidação controlada em migrações.

29. **Sem perfil de confidencialidade para dados enviados ao LLM externo.**
    - Evidência: `llm_assist` não define política de redaction.
    - Risco: Médio | Impacto: 4
    - Melhoria: opção `redact_before_llm` para dados sensíveis.
    - Validação: teste de mascaramento antes de `_chat`.

30. **Não há relatório consolidado final em Markdown por execução (apenas txt/json QA).**
    - Evidência: `qa_report.json` e `qa_report.txt`.
    - Risco: Baixo | Impacto: 2
    - Melhoria: `qa_report.md` com top riscos e próximos passos.
    - Validação: presença do arquivo ao final.

---

## 3) Plano incremental recomendado (baixo risco)

### Quick wins (5)
1. Adicionar `schema_version` em `qa_report.json`.
2. Incluir `%llm_post_edit_rejected` no sumário QA.
3. Criar preset PowerShell (`fast/balanced/quality`).
4. Adicionar `run_audit.ps1` para execução padronizada.
5. Introduzir `ruff` com regras mínimas.

### Estruturais (8)
1. Detector `table_like` para OCR/nativo.
2. Guard numérico de QA por bloco.
3. Métrica de overflow de textbox no render.
4. Preservação comparativa de links/bookmarks no QA.
5. Presets de threshold QA por domínio.
6. Teste E2E curto de pipeline completo.
7. Contrato versionado do schema QA.
8. Política de redaction para LLM externo.

### Avançadas (5)
1. Fallback seguro de tabela (original + nota).
2. Rebuild de TOC com leader dots alinhado.
3. Benchmark automático de qualidade por release.
4. Retry/backoff sofisticado para `llm_assist`.
5. Seleção dinâmica de blocos para pós-edição por risco.

---

## 4) Viabilidade atual de Ministral 3-8B / 14B

- **NÃO CONSTA** provider “Ministral” nativo dedicado no pipeline.
- **CONSTA** caminho OpenAI-compatible para LLM assist (pós-edição/QA), com configuração em `llm_assist`.
- Recomendação de adoção:
  - iniciar com 3B/8B em pós-edição limitada (custo/latência);
  - promover 14B apenas para páginas de maior risco (top_risky_pages);
  - manter guardas ativos (números/unidades/referências/ZXQ) e gate QA.

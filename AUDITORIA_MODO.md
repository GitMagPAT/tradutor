# Auditoria Forense — Qualidade de Tradução e Preservação de PDF

## (1) MAPA DO PIPELINE (com evidência)

| Etapa | Arquivo | Função/Classe | Termo Exato Encontrado | Observação |
|---|---|---|---|---|
| Orquestração fim-a-fim | `app/pipeline.py` | `run_pipeline` | `run_pipeline` | Fluxo principal por página, incluindo detecção, extração/OCR, tradução, render e QA. |
| Detecção de tipo de página | `app/pipeline.py`, `app/detect.py` | `detect_page_features` | `PageType.NATIVE`, `PageType.HYBRID`, `PageType.SCANNED` | Decide quando usar extração nativa e/ou OCR. |
| Extração de texto nativo | `app/extract.py` | `extract_native_text_blocks` | `page.get_text("blocks")`, `page.get_text("words")` | Extrai blocos vetoriais e cria `cover_rects` por linha/palavra. |
| OCR | `app/ocr.py` | `ocr_image_to_blocks` | `pytesseract.image_to_data`, `group_mode` | OCR com agrupamento por `paragraph` ou `line`; suporte a `return_word_boxes`. |
| OCR em imagens de páginas híbridas | `app/pipeline.py` | trecho OCR com máscara | `mask_out_rects_pt`, `translate_images` | Mascara texto nativo e OCR no restante para captar texto em figuras. |
| Segmentação/agrupamento OCR | `app/ocr.py` | `ocr_image_to_blocks` | `group_mode_scanned`, `group_mode_images`, `cluster_sparse_lines` | Segmentação diferenciada para texto corrido e diagramas. |
| Tradução (providers) | `app/translate.py` | `build_translator` + classes provider | `provider: "opusmt"`, `LibreTranslateTranslator`, `TranslateGemmaTranslator`, `MyMemoryTranslator` | Suporta provedores local/HTTP e fallback de provider. |
| Proteção pré-tradução | `app/translate.py` | `protect_entities`, `protect_glossary_terms` | `ENTITY_PATTERNS_DEFAULT`, `ENTITY_PATTERNS_RELAXED`, `ZXQ` | Protege entidades/tokens e glossário antes de enviar ao tradutor. |
| Pós-tradução/restauração | `app/translate.py` | `restore_placeholders`, `ptbr_postprocess` | `_TOKEN_FUZZY_RE`, `_TOKEN_SPACED_RE` | Restaura placeholders mesmo com corrupção parcial e aplica ajustes PT-BR. |
| Render/overlay PDF | `app/render.py` | `create_translated_page_pdf_overlay`, `create_translated_page_pdf_overlay_original` | `page.show_pdf_page`, `insert_image`, `cover_rects` | Dois modos: fundo rasterizado (`pdf_overlay`) ou página original (`pdf_overlay_original`). |
| Preservação estrutural | `app/pipeline.py` | `_preserve_pdf_features` | `set_toc`, `set_metadata`, `insert_link`, `set_page_labels` | Reaplica TOC/bookmarks, metadados e links após merge. |
| QA automático | `app/qa.py` | `run_qa_scan` | `qa_report.json`, `qa_report.txt`, `qa_fail_on_zxq` | Scanner pós-processamento com regras e gate de falha. |
| CLI/config | `app/cli.py`, `config.yaml` | `build_parser` | `--render-mode`, `--ocr-timeout-sec`, `--no-translate-images`, `glossary_path`, `entity_mode` | Flags e configuração central de comportamento. |

## (2) MATRIZ DE EVIDÊNCIAS E ACHADOS (mínimo 20)

| ID | Achado | Evidência (Arquivo:linha / função) | Termo Exato Encontrado | Risco | Impacto (1–5) | Correção proposta (objetiva) | Como validar (critério + teste) |
|---|---|---|---|---|---:|---|---|
| A01 | Falta de formato rico de glossário (apenas mapeamento simples). | `app/translate.py` / `load_glossary` | `esperado um dict (mapeamento termo->termo)` | Baixo | 5 | Evoluir `glossary.yaml` para schema com campos: `source`, `target`, `case_sensitive`, `do_not_translate`, `domain`, `notes`. | Teste unitário de parse e aplicação por prioridade/domínio; golden test com conflitos de termos. |
| A02 | Não há lista explícita `do-not-translate` separada. | `app/translate.py` / proteção via regex genérica | `ENTITY_PATTERNS_*` (sem lista dedicada) | Baixo | 5 | Criar `do_not_translate.yaml` (siglas, comandos, nomes de produto) e aplicar máscara antes de `protect_entities`. | Teste: string com siglas/comandos não deve mudar após ciclo tradução/restauração. |
| A03 | Glossário não distingue domínio técnico. | `app/translate.py` / `protect_glossary_terms` | `items = sorted(glossary.items(), key=lambda kv: len(kv[0]), reverse=True)` | Baixo | 4 | Introduzir `domain_profile` (ex.: elétrica, manutenção) no config e filtrar entradas ativas. | Teste parametrizado por domínio com termos ambíguos. |
| A04 | Regras PT-BR podem alterar texto técnico sem contexto. | `app/translate.py` / `ptbr_postprocess` | `_PTBR_RULES` | Médio | 3 | Aplicar pós-edição só quando `source="native"` e bloco sem unidades/códigos sensíveis; adicionar gate `skip_postprocess_if_protected_ratio>0`. | Teste de regressão com frases técnicas contendo termos próximos a regras ortográficas. |
| A05 | Proteção de unidades é parcial (não cobre várias unidades técnicas). | `app/translate.py` / regex unidade | `(?:kg|g|mg|lb|m|cm|mm|km|mi|°C|°F|%)` | Baixo | 5 | Expandir regex para `kV`, `V`, `A`, `mA`, `Hz`, `N·m`, `bar`, `MPa`, `rpm`, etc.; centralizar lista em config. | Teste unitário: nenhuma unidade permitida pode ser alterada na tradução. |
| A06 | Não há validador explícito de diffs críticos (número/unidade/sinal) por bloco. | `app/qa.py` | inexistência de regra dedicada | Médio | 5 | Adicionar `qa_numeric_guard`: compara tokens numéricos/unidades/sinais entre original e traduzido em `work/logs`. | `--qa-report` deve listar blocos com `number_mismatch`, `unit_mismatch`, `sign_mismatch`. |
| A07 | QA atual cobre ZXQ e unchanged ratio, mas sem score por bloco/página 0–100. | `app/qa.py` / `run_qa_scan` | `high_unchanged_ratio`, `zxq_leak` | Baixo | 4 | Incluir score ponderado por severidade e top N páginas críticas. | Snapshot de `qa_report.json` com `score_page` e ordenação decrescente de risco. |
| A08 | Não há CLI específica para forçar relatório QA customizado. | `app/cli.py` | sem `--qa-report` | Baixo | 3 | Adicionar `--qa-report <path>` e `--qa-threshold`. | Teste CLI: criação de arquivo em caminho customizado e retorno não-zero acima de threshold. |
| A09 | Segmentação ainda pode gerar fragmentos curtos (perda de contexto). | `app/extract.py`, `app/ocr.py` | `min_chars_block: int = 5`, `if len(paragraph_text) < 2` | Médio | 5 | Criar merge inteligente de blocos adjacentes (mesma linha/coluna) com limiar mínimo (ex.: <18 chars). | Métrica automática: taxa de blocos curtos por página antes/depois. |
| A10 | Falta heurística explícita para TOC/table-like na segmentação. | `app/translate.py` e `app/extract.py` | proteção de `_LEADER_DOTS_PATTERN` apenas | Médio | 4 | Detectar padrões `leader dots`, múltiplas colunas e separadores; tratar como segmento estruturado (não frase livre). | Teste com exemplos TOC: preservar alinhamento, número e pontilhado. |
| A11 | Tratamento de tabela é indireto, sem detector formal de tabela. | `app/extract.py` + render | ausência de função `detect_tables` | Médio | 5 | Implementar heurística de tabela por alinhamento X recorrente, densidade de linhas e bbox; modo célula-a-célula. | Caso teste com tabela técnica: sem overflow e sem perda de grade visual. |
| A12 | Sem fallback “manter original + nota” para tabela com overflow alto. | `app/render.py` | sem fallback por bloco/tabela | Baixo | 4 | Se estimativa de expansão exceder largura/altura da célula, manter texto original e anexar nota traduzida lateral/rodapé. | Teste visual com célula longa e assert de ausência de sobreposição. |
| A13 | Risco de reencode de imagem no modo `pdf_overlay` (perda de qualidade/tamanho). | `app/render.py` / `pil_to_bytes` | `img.save(... format="JPEG" ...)` | Médio | 4 | Recomendar `pdf_overlay_original` default para preservar ativos; em `pdf_overlay`, permitir PNG seletivo para áreas técnicas. | A/B de tamanho + PSNR visual em página com diagrama. |
| A14 | `pdf_overlay_original` já preserva página vetorial, mas fallback cai para raster. | `app/render.py` / `create_translated_page_pdf_overlay_original` | `except Exception: ... insert_image` | Baixo | 3 | Logar causa de fallback e marcar página com risco visual alto no QA. | QA deve apontar `render_fallback_raster=true` por página. |
| A15 | Cobertura pode apagar traços finos em desenhos mesmo com mitigação. | `app/render.py` | `cover_pad_pt`, `cover_pad_pt_ocr`, `cover_rects` | Médio | 4 | Adicionar detector de proximidade de linhas gráficas (contraste/edge) e reduzir `cover_opacity` localmente. | Validação visual: reduzir casos de “caixa branca” em linhas técnicas. |
| A16 | Preservação TOC estrutural existe, mas sem teste de regressão dedicado. | `app/pipeline.py` / `_preserve_pdf_features` | `dst.set_toc(toc)` | Baixo | 4 | Adicionar teste que compara profundidade/quantidade de TOC e labels entre PDF entrada/saída. | Teste automatizado com fixture PDF contendo TOC multinível. |
| A17 | Links são copiados em best-effort, mas erros silenciosos podem passar. | `app/pipeline.py` / `_preserve_pdf_features` | vários `except Exception: pass` | Médio | 3 | Contabilizar links copiados por página e emitir warning quando cair abaixo do original. | QA estrutural: `%links_preservados` por documento. |
| A18 | `chunk_text` é simples e pode quebrar semântica em sentenças técnicas longas. | `app/translate.py` / `chunk_text` | `_SENT_SPLIT = re.compile(r"(?<=[\.!\?])\s+")` | Médio | 4 | Adicionar chunking por pontuação técnica (`;`, `:`) e proteção de enumeradores/listas. | Testes de chunking com texto técnico e tabelas inline. |
| A19 | Sem métrica explícita de “perda de conteúdo” entre original/traduzido. | `app/qa.py` | ausência de comparador lexical | Médio | 5 | Implementar métrica de cobertura (`len_tokens_traduzidos/len_tokens_origem`) e repetição anômala. | QA: flag para blocos com cobertura < limiar ou repetição > limiar. |
| A20 | Logs de bloco são opcionais e default é falso, reduzindo auditabilidade. | `config.yaml`, `app/pipeline.py` | `log_blocks: false` | Baixo | 4 | Habilitar `log_blocks` no modo auditor/CI e limitar amostragem para custo baixo. | Pipeline deve gerar evidências por bloco em execução de auditoria. |
| A21 | Não há lint configurado no repositório. | evidência de arquivos de projeto | `requirements-dev.txt` só contém `pytest` | Baixo | 3 | Mínimo viável: adicionar `ruff` com regras básicas e comando único no CI local. | `ruff check app tests` sem erros críticos. |
| A22 | Testes existentes não cobrem render/ocr/pipeline completos. | `tests/` | `test_translate.py`, `test_utils.py` apenas | Médio | 4 | Criar testes de integração leve (1 PDF de amostra) cobrindo `run_pipeline` e `run_qa_scan`. | `pytest -q` deve incluir cenário E2E curto (1–2 páginas). |

### Especificação proposta de glossário (entregável A)

```yaml
version: 1
entries:
  - source: "control panel"
    target: "painel de controle"
    domain: ["industrial", "maintenance"]
    case_sensitive: false
    do_not_translate: false
    priority: 100
    notes: "termo preferencial"
```

Formato de regras:
- `priority`: resolve conflito de sobreposição de termos.
- `do_not_translate`: mantém token original.
- `domain`: ativa por perfil em `config.yaml`.

Exemplo de 30 entradas genéricas EN→PT-BR (sem depender do PDF):
`control panel`, `power supply`, `circuit breaker`, `ground`, `wiring`, `maintenance`, `inspection`, `torque`, `voltage`, `current`, `frequency`, `setpoint`, `shutdown`, `startup`, `safety`, `warning`, `caution`, `manual mode`, `automatic mode`, `sensor`, `actuator`, `calibration`, `alignment`, `fault`, `diagnostics`, `replacement`, `assembly`, `disassembly`, `operating conditions`, `reference`.

Integração sugerida no código:
- `app/translate.py`: estender `load_glossary`/`protect_glossary_terms` para schema rico.
- `app/pipeline.py`: carregar `domain_profile` de config e passar para `translate_many_with_cache`.

### Guards pré/pós-tradução (entregável B)

1. **Pré**: mascarar números/unidades/sinais/refs (`Fig. 3-2`, `Tabela 5`, datas, tags) antes da tradução.
2. **Pós**: restaurar placeholders e validar diffs críticos.
3. **Validador**: gerar por bloco `critical_diffs: [number_mismatch, unit_mismatch, sign_mismatch, ref_mismatch]`.
4. **Relatório**: agregar por página (`risk_score`) e por documento.

### Segmentação (entregável C)

Critérios propostos:
- Merge de blocos com `<18` caracteres quando adjacentes no mesmo eixo e sem pontuação final forte.
- Não traduzir bloco isolado de 1–2 caracteres (já há filtro parcial no OCR).
- Detectar texto tipo tabela/TOC por múltiplos alinhamentos X e leader dots.

Validação automática:
- `short_block_ratio = blocos(<18 chars)/total` por página.
- Meta: reduzir `short_block_ratio` sem elevar overflow visual.

### Pós-edição (entregável D)

Ordem proposta:
1. proteção de tokens
2. aplicação de glossário + do-not-translate
3. tradução
4. restauração
5. revisão leve PT-BR (guardada por critérios)

Pular pós-edição quando:
- bloco contém alta densidade de tokens protegidos;
- bloco marcado como tabela/código/comando;
- QA detecta risco de alteração semântica.

### QA semântico/regressão (entregável E)

Esquema JSON proposto:

```json
{
  "summary": {"pages_total": 0, "risk_pages_top": []},
  "pages": [
    {
      "page": 0,
      "score": 0,
      "reasons": ["number_mismatch"],
      "blocks": [
        {"id": "nat_0000_001", "score": 72, "reasons": ["unit_mismatch"], "snippet": "..."}
      ]
    }
  ]
}
```

Scoring 0–100 sugerido:
- +40 `number_mismatch`
- +30 `unit_mismatch`
- +25 `sign_mismatch`
- +20 `zxq_leak`
- +10 `high_unchanged_ratio`
- clamp em 100.

CLI sugerida:
- `--qa-report work/qa_report.json`
- `--qa-threshold 70` (falha CI acima do limiar)

## (3) CHECKLIST DE QUALIDADE FINAL (execução)

1. Validar consistência terminológica (glossário + do-not-translate).
2. Verificar preservação de números/unidades/sinais.
3. Auditar blocos muito curtos e merges aplicados.
4. Revisar páginas com maior `risk_score`.
5. Confirmar ausência de `ZXQ` no PDF final.
6. Checar TOC: níveis, leader dots, paginação.
7. Checar tabelas: grid visual, sem overflow de célula.
8. Checar imagens/diagramas: sem degradação perceptível.
9. Verificar overlay: sem texto sobre figura indevida.
10. Conferir links/bookmarks/metadados preservados.
11. Validar log de fallback raster por página.
12. Rodar suíte de testes automatizados.

## (4) ROADMAP + 3 PRs (baixo risco)

### Roadmap

**Quick wins (5):**
1. Adicionar `do_not_translate.yaml` e parser simples.
2. Expandir regex de unidades técnicas.
3. Implementar score QA 0–100 por página.
4. Expor `--qa-report` no CLI.
5. Habilitar modo auditor para `log_blocks=true`.

**Estruturais (8):**
1. Evoluir schema de glossário para entradas ricas.
2. Adicionar perfil de domínio em config.
3. Implementar guard numérico pós-tradução.
4. Detector table-like (alinhamento e densidade).
5. Estratégia de tradução de tabela por célula/linha.
6. Métrica de cobertura semântica no QA.
7. Testes de regressão de TOC/bookmarks/links.
8. Teste de integração E2E curto (PDF amostra).

**Avançadas (5):**
1. Otimizador adaptativo de cobertura por contraste/edge.
2. Detector de overflow de textbox com fallback inteligente.
3. Relatório QA markdown com top N páginas críticas.
4. Benchmark automático de qualidade (antes/depois).
5. Modo “safe table fallback” (original + nota).

### 3 PRs de baixo risco

**PR 1 — QA Observability**
- Escopo: score por página/bloco e CLI `--qa-report`.
- Arquivos: `app/qa.py`, `app/cli.py`, `config.yaml`, `README.md`.
- Checklist: schema JSON, ranking top N, opção de caminho customizado.
- Testes: unitários de scoring + CLI.
- Aceite: relatório gerado e navegável, sem alterar output visual do PDF.

**PR 2 — Terminologia controlada**
- Escopo: `do_not_translate` + evolução compatível de glossário.
- Arquivos: `app/translate.py`, `config.yaml`, `glossary_example.yaml`, `README.md`.
- Checklist: backward compatibility com glossário atual, prioridade por termo.
- Testes: aplicação de termos, case sensitivity, exclusões.
- Aceite: consistência terminológica maior sem quebrar providers atuais.

**PR 3 — Segmentação segura para tabela/TOC**
- Escopo: heurísticas table-like/TOC e merge de fragmentos curtos.
- Arquivos: `app/extract.py`, `app/ocr.py`, `app/pipeline.py`, `app/render.py`.
- Checklist: detector simples de baixo risco, flags de config para ligar/desligar.
- Testes: casos sintéticos para leader dots, colunas e blocos curtos.
- Aceite: redução de fragmentação e menor incidência de overflow/overlay indevido.

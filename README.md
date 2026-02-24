# PDF Translate pt-BR (Windows) — v0.3.6

## Novo padrão (gratuito e mais leve que TranslateGemma): Opus-MT local (HuggingFace)

- Provider: `opusmt` (roda **localmente** via `transformers` + `torch`).
- Modelo padrão: `Helsinki-NLP/opus-mt-tc-big-en-pt`.
- Alvo PT-BR real via token `>>pob<<` (o projeto usa o alias interno `pb`).
- Não precisa Docker para traduzir (Docker só é necessário se você escolher `libretranslate` ou `translategemma`).

Este projeto pega um **PDF em inglês** (com texto copiável, scan, ou híbrido), processa **página a página** e gera um **novo PDF traduzido para Português (PT-BR)** (alvo interno `pb`), preservando o layout **o máximo possível**.



## Novidades desta versão (v0.3.6)

- **Opus-MT (HuggingFace) como padrão**: melhor qualidade que LibreTranslate em muitos casos, sem depender de servidor HTTP.
- **PT-BR de verdade**: usando token `>>pob<<` no modelo OPUS-MT (quando `target_lang=pb`).

- **Sumário/Índice (TOC) com leader dots**: melhor preservação e tradução em trechos do tipo "... ... ...  123".
- **Retradução de blocos unchanged (retry de baixo risco)**: quando um bloco parece "traduzível" mas retornou igual, tentamos novamente com um modo de proteção de entidades mais leve.
- **QA Scanner (auditoria automática)**: ao final gera `work/qa_report.json` e aponta possíveis problemas (incluindo vazamento de tokens `ZXQ`).

- **Correção crítica de placeholders ZXQ**: restauração de tokens agora também lida com **tokens espaçados** (ex.: `Z X Q ENT ...`).
- **`translator.entity_mode` (default: `relaxed`)**: modo configurável de proteção de entidades (mais fluência com baixo risco).
- **Pós-processamento determinístico**: pequenos ajustes de pontuação/whitespace e preservação de CAIXA ALTA em títulos curtos.

- **Imagens/diagramas com menos “lacunas”**: OCR em imagens agora guarda as bboxes por palavra e usa essas sub-bboxes para “apagar” o texto original com mais precisão (reduz retângulos grandes cobrindo figuras).
- **Render em 2 passadas (cover → texto)**: evita casos em que um cover posterior acabava “tapando” texto traduzido anterior.
- **Workdir isolado por PDF + manifest de segurança**: evita misturar páginas quando você roda PDFs diferentes com `--resume` (problema que podia gerar um PDF final “misturado”).  
- **Placeholders robustos** para números/códigos (evita aparecer `__ENT_...` no PDF final).
- **Overlay mais inteligente em fundos escuros**: caixas de tradução respeitam melhor banners/figuras escuras (texto muda automaticamente para branco quando necessário).

---
✅ Funciona com:
- PDF com texto copiável (texto “nativo”)
- PDF digitalizado (scan/imagem)
- PDF híbrido (texto + figuras com texto)
- Tradução de texto “dentro de imagens” (via OCR em cima da página, quando habilitado)

---

## Como funciona (bem resumido)

Para cada página:

1. **Renderiza a página em imagem** (em DPI configurável).
2. **Detecta** se existe texto copiável (nativo) e extrai blocos com posições.
3. **OCR (Tesseract)**:
   - Se a página é scan: OCR completo.
   - Se a página tem texto nativo: opcionalmente faz OCR em cima da página **mas mascarando** o texto nativo (para capturar texto dentro de figuras/imagens).
4. **Tradução** (por blocos) usando:
   - **LibreTranslate local** (recomendado) — API gratuita local
   - ou **MyMemory** (gratuito, porém com limites)
5. Gera uma página de saída em PDF com:
   - **Fundo** = (a) imagem rasterizada da página (modo `pdf_overlay`) **ou** (b) a própria página original (modo `pdf_overlay_original`)
   - **Texto traduzido** sobreposto (vetorial, selecionável)

---

# ✅ Caminho mais fácil (recomendado): PowerShell “1 comando”

## 0) Descompactar o ZIP

1. Baixe o `.zip`.
2. Clique com botão direito → **Extrair tudo**.
3. Coloque a pasta extraída em um lugar simples, por exemplo:
- `C:\Users\SEU_USUARIO\Downloads\pdf_translate_ptbr_v0.3.5`

## 1) Colocar o PDF na pasta input

- Copie o seu PDF em inglês para:
  - `input\meu_arquivo.pdf`

✅ Para um teste rápido, já incluí um PDF pequeno de exemplo em inglês:
- `input\prototype_english.pdf`

## 2) Rodar o script (setup + tradução)

## 2.1) (Opcional) Verifique seu ambiente com o Doctor

Se você estiver com problemas (Tesseract não encontrado, LibreTranslate não responde, etc.), rode:

```powershell
# Na pasta do projeto
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
python -m app --doctor
```

Ele checa:
- Python + dependências (PyMuPDF/Pillow/pytesseract)
- Se o Tesseract está instalado e acessível
- Se o `TESSDATA_PREFIX` está OK
- Se o LibreTranslate responde em `/health` (se você estiver usando ele)


Abra o **PowerShell** e rode exatamente isto:

```powershell
# 0) Ir para a pasta do projeto
$BASE = "$env:USERPROFILE\Downloads\pdf_translate_ptbr_v0.3.5"
Set-Location $BASE

# 1) Liberar execução só nesta sessão
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force

# 2) Rodar setup + OCR + tradução
.\setup_and_translate_windows.ps1 `
  -Pdf "input\meu_arquivo.pdf" `
  -StartLibreTranslateDocker `
  -LibreTranslateWaitSeconds 900 `
  -LibreTranslateLoadOnly "en,pt" `
  -PersistToolsPath
```

### O que esse comando faz?
- Cria o `.venv`
- Instala dependências Python (`requirements.txt`)
- Instala **Tesseract** via `winget` (se não tiver)
- Baixa `tessdata` local (para `eng` + `osd`)
- (Opcional) Sobe **LibreTranslate** via Docker (se você tiver Docker Desktop)
- Roda o pipeline e gera o PDF traduzido em `output\..._ptbr.pdf`

✅ Ao final, você verá algo como:
- **Saída:** `...\output\meu_arquivo_ptbr.pdf`

---

# 🚀 Se você NÃO tem Docker (LibreTranslate), ainda dá para rodar

## Opção A) Subir LibreTranslate via Python (sem Docker)

1. Rode uma vez o `setup_and_translate_windows.ps1` (ele cria `.venv` e instala deps)
2. Em outro PowerShell:

```powershell
Set-Location $BASE
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
.\scripts\start_libretranslate_python.ps1
```

Ele vai subir o servidor local em `http://127.0.0.1:5000`.

Depois rode novamente o `setup_and_translate_windows.ps1`.

---

## Opção B) Usar MyMemory (gratuito, mas LIMITADO)

⚠️ **Para 500 páginas, NÃO recomendo** — você provavelmente vai atingir limites diários.

Mesmo assim, para testar rápido:

```powershell
.\setup_and_translate_windows.ps1 -Pdf "input\meu_arquivo.pdf" -Translator "mymemory"
```

---

# ⚙️ Ajustes importantes (qualidade vs tamanho/velocidade)

## DPI (recomendado: 200)

- `-Dpi 150` → mais rápido / PDF menor / OCR pior
- `-Dpi 200` → equilíbrio (recomendado)
- `-Dpi 300` → OCR melhor / mais lento / PDF maior

Exemplo:

```powershell
.\setup_and_translate_windows.ps1 -Pdf "input\meu.pdf" -Dpi 300 -StartLibreTranslateDocker
```

## RenderMode (tamanho do PDF de saída)

Se o seu **PDF de saída ficar muito maior** que o PDF de entrada, use este modo:

- `pdf_overlay_original` (recomendado): **preserva o PDF original** como fundo e só sobrepõe a tradução.

Ele normalmente gera arquivos **bem menores** e mantém qualidade vetorial.

Exemplo:

```powershell
.\setup_and_translate_windows.ps1 `
  -Pdf "input\meu.pdf" `
  -RenderMode pdf_overlay_original `
  -StartLibreTranslateDocker
```

Outros modos:

- `pdf_overlay`: rasteriza o fundo em uma imagem (tende a ficar **maior**, mas é o mais previsível).
- `raster`: o resultado final vira uma imagem (não selecionável). Use só se estiver com problemas no modo PDF.

## OCR timeout (evitar travar)

Se o processo **travar** em alguma página, você pode limitar quanto tempo o Tesseract pode gastar por página:

```powershell
.\setup_and_translate_windows.ps1 `
  -Pdf "input\meu.pdf" `
  -OcrTimeoutSec 180 `
  -StartLibreTranslateDocker
```

Se uma página estourar o timeout, ela pode cair em modo fallback (e ficará registrada em `work\logs`).

## Traduzir texto dentro de imagens (figuras)

Por padrão, o projeto tenta capturar texto dentro de imagens com OCR mascarado.

Se você quiser DESLIGAR isso:

```powershell
.\setup_and_translate_windows.ps1 -Pdf "input\meu.pdf" -NoTranslateImages
```

---

# 🧠 Glossário (para consistência de termos)

1. Copie `glossary_example.yaml` para `glossary.yaml`
2. Ajuste os termos que você quer “forçar”
3. Rode normalmente

Isso ajuda muito em documentos técnicos para manter termos consistentes.

---

# 🧰 Rodar pelo PyCharm (passo a passo para leigos)

## Passo 1) Abrir o projeto
1. Abra o **PyCharm**
2. Clique em **File → Open**
3. Selecione a pasta do projeto:
  - `pdf_translate_ptbr_v0.3.5`
4. Clique em **OK**

## Passo 2) Configurar o Python do projeto (venv)
O jeito mais fácil é primeiro rodar o `setup_and_translate_windows.ps1` (ele cria `.venv`).

Depois no PyCharm:

1. **File → Settings**
2. **Project: ... → Python Interpreter**
3. Clique na engrenagem ⚙️ → **Add**
4. Escolha **Existing environment**
5. Aponte para:
  - `...\pdf_translate_ptbr_v0.3.5\.venv\Scripts\python.exe`
6. Clique em **OK**

## Passo 3) Criar uma configuração de execução
1. No topo, clique em **Add Configuration...**
2. Clique em **+** → **Python**
3. Em **Module name**, coloque:
   - `app`
4. Em **Parameters**, coloque algo assim:

```
--pdf input\meu_arquivo.pdf --out output\meu_arquivo_ptbr.pdf --dpi 200 --translator libretranslate --libretranslate-url http://127.0.0.1:5000
```

5. Em **Working directory**, selecione a pasta do projeto.
6. Clique em **Apply** e **OK**
7. Clique em **Run**

---

# 📁 Onde ver os resultados

- PDF final:
  - `output\<nome>_ptbr.pdf`

- Intermediários:
  - `work\out_pages\page_0000.pdf` (um PDF por página)
  - `work\logs\page_0000.json` (log por página)
  - `work\cache.sqlite` (cache de tradução)

Se algo der errado no meio, você pode rodar de novo e ele continua (resume).

---

# 🧯 Troubleshooting


## Execução segura (evita PSReadLine + repara conflito automaticamente)

## Runner antigo com `@argsList` (erro no parâmetro Translator)

Se o comando abaixo retornar resultado:

```powershell
Select-String -Path .\run_windows_safe.ps1 -Pattern '@argsList'
```

seu `run_windows_safe.ps1` está desatualizado e vai falhar com erro de binding (`-Out` virando valor de `-Translator`).

Atualize **os dois scripts** e valide:

```powershell
git fetch origin
git checkout origin/main -- run_windows_safe.ps1 setup_and_translate_windows.ps1
Select-String -Path .\run_windows_safe.ps1 -Pattern '@setupParams'
Select-String -Path .\run_windows_safe.ps1 -Pattern '@argsList'
```

Resultado esperado:
- `@setupParams` aparece;
- `@argsList` não aparece.

Depois rode novamente:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_windows_safe.ps1 -Base "C:\Users\08292421394\Downloads\pdf_translate_ptbr_v0.3.6" -Pdf "input\meu_arquivo.pdf" -Out "output\meu_arquivo_ptbr_final.pdf" -Translator "opusmt" -RenderMode "pdf_overlay" -StartPage 0 -EndPage 22
```

Se você estiver travado com erro do **PSReadLine** ao colar comandos longos e/ou `<<<<<<<` no `setup_and_translate_windows.ps1`, use o runner seguro:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_windows_safe.ps1 -Base "C:\Users\08292421394\Downloads\pdf_translate_ptbr_v0.3.6" -Pdf "input\meu_arquivo.pdf" -Translator "opusmt" -RenderMode "pdf_overlay" -StartPage 0 -EndPage 22
```

Esse script:
- evita comando multilinha com crase (causa comum do bug do PSReadLine);
- detecta `<<<<<<<`/`=======`/`>>>>>>>` em `setup_and_translate_windows.ps1`;
- tenta reparar automaticamente com `git restore` (HEAD) e fallback `origin/main`;
- só então chama o setup normal com parâmetros curtos e estáveis.

## Erro do PSReadLine ao colar comando grande

Se aparecer erro `System.ArgumentOutOfRangeException` do **PSReadLine** ao colar um comando muito longo com crases (```),
use uma destas opções de baixo risco:

1. Colar o comando em blocos menores;
2. Salvar os parâmetros em um arquivo `.ps1` e executar o arquivo;
3. Executar versão curta:

```powershell
.\setup_and_translate_windows.ps1 -Pdf "input\meu_arquivo.pdf" -Out "output\meu_arquivo_ptbr.pdf" -Translator "opusmt" -RenderMode "pdf_overlay_original" -PersistToolsPath
```

## “winget não encontrado”
- Atualize/instale **App Installer** (Microsoft Store)
- Ou instale manualmente Python e Tesseract

## Erro com `<<<<<<<` no `setup_and_translate_windows.ps1` (conflito Git)

Se o PowerShell mostrar erro na linha com `<<<<<<< ...`, o arquivo foi salvo com conflito de merge não resolvido.

1. No repositório local, rode:

```powershell
git fetch origin
git checkout origin/main -- setup_and_translate_windows.ps1
```

2. Confirme que não restaram marcadores:

```powershell
Select-String -Path .\setup_and_translate_windows.ps1 -Pattern '^(<<<<<<< |=======|>>>>>>> )'
```

3. Rode o script novamente.

> Dica: evite editar o arquivo manualmente quando estiver com conflito; restaure do `origin/main` primeiro.

## “IndentationError” / “SyntaxError” em `app/qa.py` (ou outro módulo)

Se aparecer erro de sintaxe/indentação durante o QA (`IndentationError`, `SyntaxError`), isso indica arquivo com conflito/edição quebrada.

1. Atualize seu branch com `origin/main`.
2. Rode validação rápida:

```powershell
python -m py_compile app\qa.py app\pipeline.py app\translate.py
```

3. Execute novamente o script de tradução.

> O `setup_and_translate_windows.ps1` já faz validação de sintaxe antes de iniciar o pipeline.

## Saída com inglês + português misturado (sombra do texto original)

Se ainda aparecer “sombra” do inglês em páginas textuais, mantenha:

- `-RenderMode "pdf_overlay_original"` para páginas com imagens/vetores;
- e deixe `render.auto_rasterize_text_pages_in_overlay_original: true` no `config.yaml` (padrão), para converter automaticamente páginas sem imagem para `pdf_overlay` e reduzir mistura EN/PT.
- o pipeline também faz um retry de blocos que saíram ainda “english-heavy” (`pipeline.retranslate_english_heavy: true`).

Se quiser máxima força em todo o documento, use direto:

```powershell
.\setup_and_translate_windows.ps1 -Pdf "input\meu_arquivo.pdf" -RenderMode "pdf_overlay"
```

## “Permission denied” ao salvar `output\...pdf`

Se aparecer erro como `cannot remove file ... Permission denied`, normalmente o PDF de saída está aberto em outro programa.

1. Feche o arquivo em visualizadores (Adobe, Edge, navegador com preview, etc.).
2. Rode novamente o comando.
3. Se necessário, troque o nome de saída (`-Out`) para um novo arquivo.

> O script agora valida esse cenário antes de iniciar o pipeline e aborta com mensagem clara quando o arquivo estiver bloqueado.

## “tesseract.exe não encontrado”
- Verifique se existe:
  - `C:\Program Files\Tesseract-OCR\tesseract.exe`
- Se existir, o script normalmente detecta.
- Se não, rode novamente o setup.

## “LibreTranslate não respondeu”
Isso costuma acontecer por 3 motivos:

1) **Docker Desktop não está rodando** (ou travou)  
2) **Primeira inicialização** do LibreTranslate: ele baixa modelos (pode levar vários minutos)  
3) O teste de saúde não conseguiu acessar o endpoint (URL errada / firewall / etc.)

### Se estiver usando Docker
1. Abra o **Docker Desktop** e confirme que está **Running**.
2. No PowerShell, rode:

```powershell
docker ps
docker logs -f libretranslate
```

- Se os logs mostrarem download de modelos, **espere**.
- Se você quiser acelerar o 1º startup, use `-LibreTranslateLoadOnly "en,pt"` (carrega só inglês+português).

3. Teste a API no PowerShell:

```powershell
# deve retornar algo (status ok, ou lista de idiomas)
curl.exe http://127.0.0.1:5000/health
curl.exe http://127.0.0.1:5000/languages
```

### Se NÃO estiver usando Docker
- Rode em outro PowerShell (deixe aberto):

```powershell
scripts\start_libretranslate_python.ps1 -LoadOnly "en,pt"
```

### Dica extra (quando o container já existe)
Se você já criou o container antes **sem** `LT_LOAD_ONLY`, ele pode ter baixado muitos modelos.

Para recriar do zero (opcional):

```powershell
docker rm -f libretranslate
```

Depois rode novamente o `setup_and_translate_windows.ps1`.

---

# 🔒 Observações importantes

- Este projeto cria um PDF traduzido com **fundo rasterizado** (imagem).  
  Isso garante preservação de layout, inclusive para scans/híbridos, mas aumenta o tamanho do arquivo.
- O texto traduzido em `pdf_overlay` fica **selecionável**.
- LibreTranslate usa `target_lang=pt` (Português genérico). Para pt-BR “perfeito”, você pode:
  - usar glossário
  - ou trocar o provedor por um serviço que diferencie pt-BR/pt-PT (não incluso aqui)

---

## Licença
MIT (veja `LICENSE`)
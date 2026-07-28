# FullPrint — Spooler de etiquetas da Shopee Full

Automação de impressão das etiquetas de produto da **Shopee Full** em impressoras Zebra (ZD220) e afins. Agrupa por SKU, compõe no layout da sua bobina e manda o lote inteiro pra impressora — eliminando o fluxo manual (imprimir SKU por SKU no navegador + ZebraDesigner).

## Formas de entrada

O app aceita **dois tipos de arquivo**. A escolha muda diretamente a qualidade da impressão:

### 1. PDF da Shopee — **recomendado** ⭐

Lê a **camada textual** do PDF gerado pela Shopee (SKU, Seller SKU e descrição) e reimprime tudo como **texto ZPL nativo** (`^A0`), nítido, com o **QR regenerado a partir do próprio dado**. Sem OCR, sem IA e **sem rasterizar** — é o caminho de melhor qualidade e o fluxo principal do projeto. Extração determinista via PyMuPDF; detecta PDF sem camada textual.

### 2. TXT / ZPL da Shopee — opção alternativa

O arquivo `.txt`/`.zpl` exportado. Pode ser impresso de dois jeitos, conforme o **modelo de etiqueta**:

- **Fiel (10x15 Shopee)**: envia o arquivo **byte a byte** (pass-through) — sai idêntico ao original. Indicado para quem usa a bobina padrão 10x15 da Shopee.
- **Composto (bobina própria)**: recorta cada sticker do **bitmap** original (pixels do arquivo, **sem OCR**) e reposiciona na sua bobina (ex.: 50x25mm, 2 colunas). Como parte do conteúdo vem de imagem rasterizada e reescalada, pode sair **distorcido / menos legível** em bobinas menores.

> **Prefira sempre o PDF quando ele estiver disponível.** O TXT/ZPL composto é uma alternativa para os casos em que só há o arquivo `.txt` — e tende a ficar menos legível que o PDF nativo.

> **Status**: v0.5.x. Entrada por **PDF com texto nativo** é o fluxo principal. Modelos de etiqueta configuráveis; o preview mostra a etiqueta como ela vai sair.

## Funcionalidades

- Interface gráfica em **CustomTkinter** (tema dark).
- Seleção de impressora (lista as locais do Windows via `win32print`).
- **Anexar PDF (recomendado) ou TXT/ZPL** via diálogo — o parser é escolhido automaticamente pela extensão.
- **PDF → texto ZPL nativo**: SKU, Seller SKU e descrição saem como texto nítido (`^A0`), com word-wrap na descrição (nunca corta palavra) e QR regenerado do dado. Sem OCR nem rasterização.
- **Impressão pass-through** (TXT/ZPL, modelo fiel 10x15): os bytes originais vão direto para a impressora (RAW), sem decode/re-encode — fidelidade por construção.
- **Impressão composta** (TXT/ZPL ou PDF): re-monta as etiquetas no tamanho/layout da sua bobina, com **1 bloco de impressão por linha**.
- Preview por SKU ou individual, com **imagem real da etiqueta**, e quantidade por SKU (SKU lido do **QR code** de cada sticker via pyzbar no fluxo TXT).
- **Interpretação local de ZPL (substituto do Labelary)**: botão "Interpretar ZPL" renderiza o ZPL em imagem — texto, fontes, barcodes, QR, etc. — usando `zpl-renderer-js` (WASM) via Node, **sem Labelary online nem limite de caracteres**.
- **Seller SKU via catálogo manual**: duplo-clique numa linha cadastra o mapeamento SKU Shopee → Seller SKU (persistido em `data/sku_catalog.json`).
- **OCR opcional em lote** (só quando o Tesseract está instalado): pré-preenche o Seller SKU como **sugestão** num diálogo de confirmação — a impressão usa apenas o texto confirmado. Sem Tesseract, o recurso se desliga sozinho.
- Impressão em **thread separada** (worker daemon + fila).
- **Modo DEV** (Linux/macOS, ou win32print indisponível): grava o ZPL em `data/dev_output/` ao invés de mandar para impressora.
- Log de impressão em painel lateral e arquivo rotacionado (`logs/spooler.log`).

## Estrutura

```text
fullprint/
├── src/
│   ├── main.py
│   ├── config/{settings.py, config.yaml}
│   ├── core/{parser.py, pdf_reader.py, agrupador.py, grf_decoder.py,
│   │         sku_catalog.py, ocr_seller.py, label_models.py,
│   │         label_renderer.py, zpl_renderer.py}
│   ├── services/{printer.py, spooler_worker.py, updater.py}
│   ├── database/     # Fase 2
│   ├── ui/{app.py, views/{main_view.py, label_config.py, ocr_batch_dialog.py}}
│   └── utils/{logger.py, zpl_utils.py, runtime.py}
├── renderer/{package.json, render.mjs}   # motor Node (zpl-renderer-js) p/ interpretar ZPL
├── tests/{test_parser.py, smoke_pdf.py, smoke_pdf_render.py,
│          smoke_pdf_pipeline.py, smoke_pipeline.py, smoke_grf.py, render_grf.py}
├── logs/  data/
├── requirements.txt
└── README.md
```

## Instalação nas máquinas dos operadores (produção)

**Não use `git` nas máquinas de produção.** A distribuição é por instalador:

1. Baixe o `FullPrintSetup.exe` da página de **[Releases](https://github.com/LeandroBossiniSoleira/FullPrint/releases/latest)**.
2. Execute. Ele instala em `%LOCALAPPDATA%\FullPrint` (não pede admin) e cria atalho no Menu Iniciar e na Área de Trabalho — zero configuração manual.
3. Pronto. **A partir daí o app se atualiza sozinho**: ao abrir, verifica se há versão nova no GitHub, baixa em segundo plano e aplica a atualização (silenciosa) quando o app é fechado.

> A primeira instalação precisa ser feita pelo `FullPrintSetup.exe` (não por `git clone`), porque o auto-update reinstala por cima dele.

## Instalação para desenvolvimento

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

pip install -r requirements.txt

# Motor de interpretação de ZPL (Node.js v18+ deve estar instalado):
cd renderer && npm install && cd ..
```

`pywin32` instala apenas no Windows (marker em `requirements.txt`). Em outras plataformas, o serviço cai para **modo DEV** automaticamente. No Linux, o pyzbar precisa do `libzbar0` do sistema (`apt install libzbar0`). O OCR em lote é opcional e exige o Tesseract instalado (`apt install tesseract-ocr` / instalador no Windows). Rodando do código-fonte o auto-update fica desativado (só age no `.exe` instalado).

### Renderer de ZPL (Node.js)

A interpretação de ZPL → imagem ("Interpretar ZPL") usa o pacote `zpl-renderer-js` (WASM) chamado pelo Python via subprocess (`src/core/zpl_renderer.py` → `renderer/render.mjs`). **Não há servidor web nem API externa** — tudo roda localmente. Requisitos:

- **Node.js v18+** instalado e no `PATH` (ou aponte o binário via variável de ambiente `FULLPRINT_NODE`).
- `renderer/node_modules` instalado (`cd renderer && npm install`).

> O `render.mjs` carrega o `zpl-renderer-js` via `createRequire` (build CommonJS), o que mantém a interpretação de ZPL funcionando em qualquer versão do Node (v18–v22), sem depender de como cada versão resolve o pacote dual CJS/ESM.

Se o Node não estiver disponível, o app continua funcionando normalmente (impressão e preview); apenas o botão "Interpretar ZPL" fica desabilitado com uma mensagem explicando o motivo. O `node_modules/` é empacotado no build (CI roda `npm ci`).

**Nas máquinas dos operadores (instalador):** o `FullPrintSetup.exe` detecta se o Node já existe e, se não, **pergunta se deseja instalar** — com o aceite, baixa o Node.js portátil oficial (~30 MB) para `{app}\node` (sem admin, sem alterar o PATH). O app procura o Node nessa pasta automaticamente (`node_executable()`). Quem recusar pode usar o app sem a interpretação de ZPL. O `FULLPRINT_NODE` permite apontar um Node específico, se necessário.

## Execução

```bash
python -m src.main
# ou
python src/main.py
```

## Testes

```bash
python -m unittest discover tests
# ou, com pytest:
pytest -q

# Smoke com um PDF real da Shopee Full (fluxo recomendado):
python tests/smoke_pdf.py /caminho/etiquetas_shopee.pdf
python tests/smoke_pdf_render.py /caminho/etiquetas_shopee.pdf
# Smoke com um TXT real da Shopee Full (valida o pass-through byte a byte):
python tests/smoke_grf.py /caminho/arquivo_shopee.txt
# Calibração visual dos crops de sticker (gera PNGs em data/dev_output/):
python tests/render_grf.py /caminho/arquivo_shopee.txt
```

## Configuração

Edite `src/config/config.yaml`:

- `printer.default_name`: impressora padrão (deixe vazio para usar a do Windows).
- `printer.dev_mode: true`: força modo DEV mesmo no Windows (não imprime, grava arquivo).
- `paths.default_input_dir`: pasta inicial do diálogo de anexar arquivo.

Os **modelos de etiqueta** são gerenciados pela própria interface (botão "Configurar...") e salvos em `data/label_models.json` — dimensões da bobina, colunas, margens, zona segura da borda e tamanho do QR. Use "Imprimir teste" para calibrar o alinhamento na bobina real.

## Como lançar uma nova versão

O build do instalador é **automático** via GitHub Actions (`.github/workflows/release.yml`).

1. Ajuste `src/version.py` se quiser (o CI sobrescreve com a tag de qualquer forma).
2. Crie e publique a tag:
   ```bash
   git tag v1.2.0
   git push origin v1.2.0
   ```
3. O CI (runner Windows) roda os testes + PyInstaller + compila o Inno Setup e **publica o `FullPrintSetup.exe` no Release** correspondente.
4. As máquinas dos operadores detectam a versão nova no próximo start e se atualizam sozinhas.

> Use [versionamento semântico](https://semver.org/lang/pt-BR/) nas tags (`vMAJOR.MINOR.PATCH`). A tag **precisa** ser maior que a versão instalada para o auto-update disparar.

Peças do pipeline:

| Arquivo | Função |
|---|---|
| `src/version.py` | Fonte única da versão (injetada pela tag no build). |
| `src/services/updater.py` | Verifica/baixa/aplica updates via GitHub Releases. |
| `renderer/` | Motor Node (`zpl-renderer-js`) para interpretar ZPL; empacotado no build (CI roda `npm ci`). |
| `packaging/FullPrint.spec` | Empacotamento PyInstaller (gera o `.exe`; inclui a pasta `renderer/`). |
| `packaging/installer.iss` | Instalador Inno Setup (bundle + atalhos). |
| `.github/workflows/release.yml` | CI: testa, compila e publica a cada tag `v*`. |

## Próximos passos (Fase 2)

- Persistência em SQLite (lotes + reimpressão).
- Refatorar parser sob `MarketplaceParser` para suportar Mercado Livre Full.
- Barra de progresso real no worker.
- Suporte a PDF com layout variante (hoje o reader assume 1 etiqueta por página).

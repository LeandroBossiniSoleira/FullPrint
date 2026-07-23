/**
 * Motor de renderizacao ZPL -> PNG (local, sem Labelary/web).
 *
 * Chamado pelo Python (src/core/zpl_renderer.py) via subprocess:
 *   node render.mjs <dpmm> <widthMm> <heightMm>
 *
 * - Recebe o codigo ZPL completo (^XA...^XZ) via STDIN.
 * - Devolve os bytes do PNG via STDOUT (binario).
 * - Erros vao para STDERR prefixados com "RENDER_ERROR:" e exit code 1.
 *
 * Usa zpl-renderer-js (WASM/zebrash) — interpreta ZPL II de verdade (texto,
 * barcodes, QR, ^GFA, etc.), ao contrario da decodificacao GRF que so le o
 * bitmap ja embutido no arquivo da Shopee.
 *
 * IMPORTANTE: a lib trabalha em MILIMETROS e dpmm (dots/mm):
 *   dpmm 8 = 203 dpi, 12 = 300 dpi, 24 = 600 dpi.
 */
// zpl-renderer-js e' um pacote dual CJS+ESM. O Node resolve o formato de forma
// diferente entre versoes (ex.: v20.20 trata como ESM e o import nomeado
// funciona; v20.18 trata como CommonJS e `import { ready }` quebra com
// "does not provide an export named 'ready'"). Carregar via createRequire forca
// sempre o build CJS (`require`) e devolve module.exports com `ready` em
// qualquer versao do Node. Ver ClickUp 86ajk2mc2 (erro no PC do ARTHUR).
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { ready } = require("zpl-renderer-js");

const args = process.argv.slice(2);
const dpmm = parseInt(args[0], 10) || 8; // 8 = 203 dpi (impressora Zebra ZD220)
const widthMm = parseFloat(args[1]) || 100; // etiqueta Shopee 10x15 cm
const heightMm = parseFloat(args[2]) || 150;

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
      data += chunk;
    });
    process.stdin.on("end", () => resolve(data));
    process.stdin.on("error", reject);
  });
}

async function main() {
  const zpl = (await readStdin()).trim();
  if (!zpl) {
    process.stderr.write("RENDER_ERROR: ZPL vazio (nada recebido via stdin)\n");
    process.exit(1);
  }

  const { api } = await ready;
  const base64 = await api.zplToBase64Async(zpl, widthMm, heightMm, dpmm);
  if (!base64) {
    throw new Error("renderizador retornou imagem vazia");
  }
  process.stdout.write(Buffer.from(base64, "base64"));
}

main().catch((err) => {
  process.stderr.write("RENDER_ERROR: " + (err && err.message ? err.message : String(err)) + "\n");
  process.exit(1);
});

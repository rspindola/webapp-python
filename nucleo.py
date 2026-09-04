"""
Nucleo do sistema: leitura/OCR de PDFs grandes, busca por palavra-chave e
extracao de um intervalo de paginas para um novo PDF.

Este modulo nao depende do Flask - e usado tanto pelo app web (app.py)
quanto poderia ser usado por um script de linha de comando.
"""

import hashlib
import io
import json
import os
import re
import threading
import time
import unicodedata
from pathlib import Path

from pypdf import PdfReader, PdfWriter

try:
    import pytesseract
    from pdf2image import convert_from_path
    OCR_DISPONIVEL = True
except ImportError:
    OCR_DISPONIVEL = False


# --------------------------------------------------------------------------
# Traducao de caminhos Windows -> caminho dentro do container Docker
# --------------------------------------------------------------------------

def normalizar_caminho(caminho_str: str) -> str:
    """Converte caminhos Windows para caminhos acessiveis dentro do container,
    QUANDO estiver rodando dentro do container Linux (Docker).

    O Docker Desktop no Windows monta os drives assim:
        C:\\dados\\PDFs  ->  /mnt/c/dados/PDFs
        D:\\arq         ->  /mnt/d/arq

    Se o processo estiver rodando NATIVAMENTE no Windows (sem Docker, ex:
    `python app.py` direto pra um teste rapido numa unica maquina), o
    caminho C:\\dados\\PDFs ja funciona do jeito que esta - nao precisa (e
    nao deve) ser traduzido, senao o Windows nao acha a pasta /mnt/c/...
    """
    caminho_str = caminho_str.strip()
    if os.name == "nt":
        # rodando direto no Windows (fora do Docker) - nao traduz
        return caminho_str

    # rodando dentro do container Linux - traduz letra de drive -> /mnt/<letra>
    m = re.match(r"^([A-Za-z]):[/\\](.*)$", caminho_str)
    if m:
        drive = m.group(1).lower()
        resto = m.group(2).replace("\\", "/")
        return f"/mnt/{drive}/{resto}".rstrip("/")
    return caminho_str.replace("\\", "/")


class CaminhoNaoPermitido(Exception):
    pass


def caminhos_permitidos():
    """Le a lista de raizes permitidas da variavel de ambiente DATA_ROOTS
    (separadas por ';'). Se nao configurada, cai de volta para /mnt (todo
    disco montado) - funciona, mas e menos seguro. Configure DATA_ROOTS no
    .env para restringir o app a pastas especificas."""
    bruto = os.environ.get("DATA_ROOTS", "/mnt")
    return [normalizar_caminho(p) for p in bruto.split(";") if p.strip()]


def validar_caminho(caminho_str: str) -> Path:
    """Traduz o caminho Windows->container e garante que ele esta dentro de
    uma das raizes permitidas (DATA_ROOTS). Lanca CaminhoNaoPermitido caso
    contrario. Isso evita que um usuario do formulario aponte para pastas
    fora do que a empresa autorizou (ex: raiz do sistema de arquivos)."""
    alvo = Path(normalizar_caminho(caminho_str)).resolve()
    raizes = [Path(r).resolve() for r in caminhos_permitidos()]
    for raiz in raizes:
        try:
            alvo.relative_to(raiz)
            return alvo
        except ValueError:
            continue
    raise CaminhoNaoPermitido(
        f"Caminho fora das pastas permitidas pelo administrador: {caminho_str}"
    )


# --------------------------------------------------------------------------
# Lock por arquivo: evita que duas buscas simultaneas no MESMO pdf ainda-nao-
# cacheado disparem OCR em duplicidade (desperdicio de CPU + corrida de escrita
# no arquivo de cache). Funciona enquanto o app roda em um unico processo
# (gunicorn -w 1 --threads N, como configurado no Dockerfile).
# --------------------------------------------------------------------------

_LOCKS_LOCK = threading.Lock()
_LOCKS = {}


def lock_para(caminho: Path) -> threading.Lock:
    chave = str(caminho)
    with _LOCKS_LOCK:
        if chave not in _LOCKS:
            _LOCKS[chave] = threading.Lock()
        return _LOCKS[chave]


# --------------------------------------------------------------------------
# Normalizacao e busca tolerante a ruido de OCR
# --------------------------------------------------------------------------

def normalizar(texto: str) -> str:
    """minusculas, sem acento, espacos colapsados."""
    texto = texto or ""
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    texto = texto.lower()
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto


def padrao_busca(chave: str, folga: int = 20) -> re.Pattern:
    """Regex tolerante: as palavras da chave, na ordem, com folga para ruido de OCR entre elas."""
    palavras = normalizar(chave).split()
    partes = [re.escape(p) for p in palavras]
    return re.compile(("." + "{0,%d}?" % folga).join(partes))


def padrao_generico(chave: str, folga: int = 20):
    """Como padrao_busca, mas ignora numeros (serve pra achar a proxima capa do
    mesmo tipo de documento, com outro ano/numero)."""
    palavras = [p for p in normalizar(chave).split() if not p.isdigit()]
    if not palavras:
        return None
    partes = [re.escape(p) for p in palavras]
    return re.compile(("." + "{0,%d}?" % folga).join(partes))


# --------------------------------------------------------------------------
# Extracao de texto (com OCR) e cache incremental em disco
# --------------------------------------------------------------------------
#
# O cache guarda o texto de cada pagina em um JSON (lista, mesmo tamanho que
# o numero de paginas). Paginas ainda nao OCR-adas ficam como null. Isso
# permite PARAR de processar um arquivo assim que o documento procurado for
# encontrado, sem precisar OCR-ar o restante do PDF (que pode ter centenas
# de paginas depois do trecho que interessa) - e retomar de onde parou numa
# busca futura, sem refazer OCR das paginas ja lidas.

def _cache_path(pdf_path: Path, cache_dir: Path) -> Path:
    return cache_dir / (pdf_path.stem + ".json")


def _carregar_ou_iniciar_cache(pdf_path: Path, cache_dir: Path):
    """Retorna (textos, total_paginas). `textos` e uma lista com o texto de
    cada pagina, ou None nas posicoes ainda nao processadas (nem texto
    nativo nem OCR)."""
    cache_dir.mkdir(exist_ok=True, parents=True)
    cache_file = _cache_path(pdf_path, cache_dir)

    if cache_file.exists():
        textos = json.loads(cache_file.read_text(encoding="utf-8"))
        return textos, len(textos)

    reader = PdfReader(str(pdf_path))
    total = len(reader.pages)
    textos = []
    for page in reader.pages:
        # extrair texto nativo e' rapido (nao precisa de OCR/imagem), entao
        # fazemos isso pra todas as paginas de uma vez. So fica None quando
        # a pagina realmente precisa de OCR (PDF escaneado).
        t = page.extract_text() or ""
        textos.append(t if len(t.strip()) >= 15 else None)
    return textos, total


def _persistir_cache(pdf_path: Path, cache_dir: Path, textos):
    cache_file = _cache_path(pdf_path, cache_dir)
    tmp = cache_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(textos, ensure_ascii=False), encoding="utf-8")
    tmp.replace(cache_file)


def _garantir_pagina_ocr(pdf_path: Path, textos: list, indice: int,
                          dpi: int = 150, lang: str = "por"):
    """OCR-a a pagina `indice` se ainda nao tiver texto (in-place em `textos`)."""
    if textos[indice] is not None:
        return
    if not OCR_DISPONIVEL:
        raise RuntimeError(
            "Este PDF e escaneado (sem texto) mas pytesseract/pdf2image nao "
            "estao instalados. Veja o README para instalar o Tesseract."
        )
    imgs = convert_from_path(str(pdf_path), dpi=dpi, first_page=indice + 1, last_page=indice + 1)
    textos[indice] = pytesseract.image_to_string(imgs[0], lang=lang)


def extrair_texto_paginas(pdf_path: Path, cache_dir: Path, dpi: int = 150, lang: str = "por"):
    """Forca o OCR/leitura de TODAS as paginas e retorna a lista completa de
    textos. Mantido para compatibilidade/uso pontual - a busca normal usa
    `buscar_ocorrencias_no_pdf`, que e' mais rapida por poder parar mais cedo."""
    with lock_para(pdf_path):
        textos, total = _carregar_ou_iniciar_cache(pdf_path, cache_dir)
        mudou = False
        for i in range(total):
            if textos[i] is None:
                _garantir_pagina_ocr(pdf_path, textos, i, dpi=dpi, lang=lang)
                mudou = True
        if mudou:
            _persistir_cache(pdf_path, cache_dir, textos)
        return textos


def chave_cache_imagem(pdf_path: Path, pagina: int) -> str:
    """Nome de arquivo unico para o cache de miniaturas. Usa um hash do
    CAMINHO COMPLETO (nao so o nome do arquivo): PDFs com nomes numericos
    repetidos em pastas diferentes (ex: 00000520.pdf em duas pastas distintas)
    sao comuns nesse tipo de arquivo e, usando so o stem, a miniatura de um
    arquivo poderia vazar/aparecer para o outro por engano na tela de revisao."""
    h = hashlib.sha1(str(pdf_path).encode("utf-8")).hexdigest()[:16]
    return f"{h}_{pagina}.png"


# --------------------------------------------------------------------------
# Busca de todas as ocorrencias (inicio/fim de cada sub-documento) no PDF
# --------------------------------------------------------------------------

def buscar_ocorrencias_no_pdf(pdf_path: Path, cache_dir: Path, chave: str,
                               dpi: int = 150, lang: str = "por", limite_chars_capa: int = 320,
                               progresso_callback=None, max_ocorrencias=None):
    """Busca TODAS as ocorrencias da chave neste PDF (ex: "folha de pagamento"
    pode aparecer em varios meses/anos dentro do mesmo arquivo mesclado),
    OCR-ando pagina por pagina com cache incremental.

    Cada ocorrencia e' um "documento" que comeca numa pagina-capa (pouco
    texto, bate com a chave) e termina na proxima pagina-capa do mesmo tipo
    (ou no fim do arquivo). Continua procurando ate acabar as paginas ou
    atingir `max_ocorrencias` (protege contra escanear um arquivo enorme
    inteiro atras de uma chave generica demais).

    Retorna uma lista de dicts (pode ser vazia, se a chave nao aparecer
    neste arquivo). Se `progresso_callback` for informado, e' chamado apos
    cada pagina processada como progresso_callback(pagina_atual_1based, total_paginas)."""
    pad = padrao_busca(chave)
    pad_generico = padrao_generico(chave)

    with lock_para(pdf_path):
        textos, total = _carregar_ou_iniciar_cache(pdf_path, cache_dir)
        mudou = False
        ocorrencias = []
        inicio_atual = None

        i = 0
        while i < total:
            if textos[i] is None:
                _garantir_pagina_ocr(pdf_path, textos, i, dpi=dpi, lang=lang)
                mudou = True

            if progresso_callback:
                progresso_callback(i + 1, total)

            texto_norm = normalizar(textos[i])
            # so' considera "capa" paginas com pouco texto (titulo + nomes),
            # nao paginas de conteudo onde a mesma frase tambem aparece
            parece_capa = len(texto_norm) <= limite_chars_capa

            if inicio_atual is None:
                if parece_capa and pad.search(texto_norm):
                    inicio_atual = i
            else:
                if parece_capa and pad_generico and pad_generico.search(texto_norm):
                    # esta pagina fecha o documento atual (fim exclusivo)
                    ocorrencias.append({
                        "inicio": inicio_atual,
                        "fim_sugerido": i,
                        "total_paginas": total,
                        "trecho": (textos[inicio_atual] or "").strip().replace("\n", " ")[:180],
                    })
                    if max_ocorrencias and len(ocorrencias) >= max_ocorrencias:
                        inicio_atual = None
                        break
                    # a mesma pagina pode ja ser a capa do PROXIMO documento
                    # (ex: chave sem ano, tipo "folha de pagamento", casando
                    # com cada capa em sequencia)
                    inicio_atual = i if pad.search(texto_norm) else None

            i += 1

        # se chegou ao fim do arquivo com um documento ainda "aberto"
        if inicio_atual is not None:
            ocorrencias.append({
                "inicio": inicio_atual,
                "fim_sugerido": total,
                "total_paginas": total,
                "trecho": (textos[inicio_atual] or "").strip().replace("\n", " ")[:180],
            })

        if mudou:
            _persistir_cache(pdf_path, cache_dir, textos)

        return ocorrencias


# --------------------------------------------------------------------------
# Extracao do sub-PDF final
# --------------------------------------------------------------------------

def extrair_sub_pdf(pdf_path: Path, inicio: int, fim: int, saida_path: Path):
    """inicio/fim em 0-based, intervalo [inicio, fim)."""
    reader = PdfReader(str(pdf_path))
    writer = PdfWriter()
    for i in range(inicio, fim):
        writer.add_page(reader.pages[i])
    saida_path.parent.mkdir(parents=True, exist_ok=True)
    with open(saida_path, "wb") as f:
        writer.write(f)
    return saida_path


def gerar_previa_bytes(pdf_path: Path, inicio: int, fim: int) -> bytes:
    """Gera os bytes de um PDF so' com as paginas [inicio, fim) (0-based),
    SEM OCR (so reestrutura o PDF original - rapido mesmo em arquivos
    grandes). Usado pela previa ao vivo na tela de revisao, mostrada num
    <iframe> para o usuario rolar/dar zoom com o proprio leitor do
    navegador, em vez de miniaturas soltas."""
    reader = PdfReader(str(pdf_path))
    writer = PdfWriter()
    for i in range(inicio, fim):
        writer.add_page(reader.pages[i])
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def limpar_buscas_antigas(buscas: dict, ttl_segundos: int = 3600):
    """Remove buscas com mais de `ttl_segundos` do dicionario em memoria, para
    o processo nao crescer sem limite num servidor que fica no ar por semanas."""
    agora = time.time()
    expiradas = [k for k, v in buscas.items() if agora - v.get("criada_em", agora) > ttl_segundos]
    for k in expiradas:
        buscas.pop(k, None)


def eh_pdf(caminho: Path) -> bool:
    """Verifica se o arquivo E' um PDF pelo CONTEUDO (assinatura %PDF no
    inicio), nao pela extensao do nome. Necessario porque, na pratica, esses
    lotes numerados as vezes tem arquivos PDF de verdade sem a extensao
    .pdf no nome (ex: '00000520.001' em vez de '00000520.001.pdf')."""
    try:
        with open(caminho, "rb") as f:
            return f.read(5) == b"%PDF-"
    except OSError:
        return False


def listar_pdfs(pasta: Path):
    """Lista todos os PDFs de verdade dentro da pasta (por conteudo, nao so
    pela extensao .pdf), ignorando subpastas como .ocr_cache."""
    candidatos = sorted(
        p for p in pasta.iterdir()
        if p.is_file() and not p.name.startswith(".")
    )
    return [p for p in candidatos if eh_pdf(p)]


def nome_arquivo_saida(chave: str, inicio: int, fim: int) -> str:
    base = re.sub(r"[^a-zA-Z0-9_-]+", "_", chave.strip()).strip("_")
    return f"{base}_pag{inicio + 1}-{fim}.pdf"


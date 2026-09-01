"""
Nucleo do sistema: leitura/OCR de PDFs grandes, busca por palavra-chave e
extracao de um intervalo de paginas para um novo PDF.

Este modulo nao depende do Flask - e usado tanto pelo app web (app.py)
quanto poderia ser usado por um script de linha de comando.
"""

import hashlib
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
    `buscar_no_pdf`, que e' mais rapida por parar assim que acha o documento."""
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
# Busca de inicio / fim do sub-documento (com parada antecipada)
# --------------------------------------------------------------------------

def buscar_no_pdf(pdf_path: Path, cache_dir: Path, chave: str,
                   dpi: int = 150, lang: str = "por", limite_chars_capa: int = 320):
    """Busca a chave neste PDF, OCR-ando pagina por pagina (com cache
    incremental) e PARANDO assim que:
      1) achar a pagina inicial (capa que bate com a chave), e depois
      2) achar a proxima capa do mesmo tipo (fim do documento) ou chegar
         ao fim do arquivo.

    Isso evita OCR-ar o resto do PDF quando o documento procurado esta no
    comeco de um arquivo com centenas de paginas. Retorna um dict com o
    resultado, ou None se a chave nao foi encontrada neste arquivo (nesse
    caso, o arquivo inteiro tera sido OCR-ado e cacheado, entao a proxima
    busca nele - com essa ou outra chave - sera instantanea)."""
    pad = padrao_busca(chave)
    pad_generico = padrao_generico(chave)

    with lock_para(pdf_path):
        textos, total = _carregar_ou_iniciar_cache(pdf_path, cache_dir)
        mudou = False
        inicio = None
        fim = None

        for i in range(total):
            if textos[i] is None:
                _garantir_pagina_ocr(pdf_path, textos, i, dpi=dpi, lang=lang)
                mudou = True

            texto_norm = normalizar(textos[i])
            if inicio is None:
                if pad.search(texto_norm):
                    inicio = i
            elif pad_generico and len(texto_norm) <= limite_chars_capa and pad_generico.search(texto_norm):
                fim = i
                break  # achou a proxima capa - documento termina aqui

        if mudou:
            _persistir_cache(pdf_path, cache_dir, textos)

        if inicio is None:
            return None

        if fim is None:
            fim = total

        trecho = (textos[inicio] or "").strip().replace("\n", " ")[:180]
        return {
            "inicio": inicio,
            "fim_sugerido": fim,
            "total_paginas": total,
            "trecho": trecho,
        }


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


def limpar_buscas_antigas(buscas: dict, ttl_segundos: int = 3600):
    """Remove buscas com mais de `ttl_segundos` do dicionario em memoria, para
    o processo nao crescer sem limite num servidor que fica no ar por semanas."""
    agora = time.time()
    expiradas = [k for k, v in buscas.items() if agora - v.get("criada_em", agora) > ttl_segundos]
    for k in expiradas:
        buscas.pop(k, None)


def nome_arquivo_saida(chave: str, inicio: int, fim: int) -> str:
    base = re.sub(r"[^a-zA-Z0-9_-]+", "_", chave.strip()).strip("_")
    return f"{base}_pag{inicio + 1}-{fim}.pdf"


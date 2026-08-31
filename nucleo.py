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
    """Converte caminhos Windows para caminhos acessiveis dentro do container.

    O Docker Desktop no Windows monta os drives assim:
        C:\\dados\\PDFs  ->  /mnt/c/dados/PDFs
        D:\\arq         ->  /mnt/d/arq

    Se o caminho ja for Linux (comeca com /), e retornado sem alteracao.
    Caminhos UNC (\\\\servidor\\pasta) precisam ser mapeados como letra
    de drive no Windows antes de usar o sistema.
    """
    caminho_str = caminho_str.strip()
    # Caminho com letra de drive: C:\dados ou C:/dados
    m = re.match(r"^([A-Za-z]):[/\\](.*)$", caminho_str)
    if m:
        drive = m.group(1).lower()
        resto = m.group(2).replace("\\", "/")
        return f"/mnt/{drive}/{resto}".rstrip("/")
    # Ja e caminho Linux ou relativo: normaliza apenas separadores
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
# Extracao de texto (com OCR) e cache em disco
# --------------------------------------------------------------------------

def _cache_path(pdf_path: Path, cache_dir: Path) -> Path:
    return cache_dir / (pdf_path.stem + ".json")


def extrair_texto_paginas(pdf_path: Path, cache_dir: Path, dpi: int = 150,
                           lang: str = "por", progresso=None):
    """Retorna lista de strings (texto de cada pagina). Usa cache em disco:
    a segunda busca no mesmo arquivo e praticamente instantanea.

    Protegido por lock: se dois usuarios buscarem no mesmo PDF ainda-nao-
    cacheado ao mesmo tempo, o segundo espera o primeiro terminar o OCR em
    vez de refazer o trabalho (e corromper o arquivo de cache com escritas
    concorrentes)."""
    cache_dir.mkdir(exist_ok=True, parents=True)
    cache_file = _cache_path(pdf_path, cache_dir)

    with lock_para(pdf_path):
        if cache_file.exists():
            return json.loads(cache_file.read_text(encoding="utf-8"))

        reader = PdfReader(str(pdf_path))
        total = len(reader.pages)
        textos = []
        paginas_sem_texto = []

        for i, page in enumerate(reader.pages):
            t = page.extract_text() or ""
            textos.append(t)
            if len(t.strip()) < 15:
                paginas_sem_texto.append(i)

        if paginas_sem_texto:
            if not OCR_DISPONIVEL:
                raise RuntimeError(
                    "Este PDF e escaneado (sem texto) mas pytesseract/pdf2image nao "
                    "estao instalados. Veja o README para instalar o Tesseract."
                )
            for n, i in enumerate(paginas_sem_texto):
                if progresso:
                    progresso(n + 1, len(paginas_sem_texto), total)
                imgs = convert_from_path(str(pdf_path), dpi=dpi, first_page=i + 1, last_page=i + 1)
                textos[i] = pytesseract.image_to_string(imgs[0], lang=lang)

        # escreve em arquivo temporario + rename atomico (evita cache
        # corrompido se o processo for encerrado no meio da escrita)
        tmp = cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(textos, ensure_ascii=False), encoding="utf-8")
        tmp.replace(cache_file)
        return textos


def chave_cache_imagem(pdf_path: Path, pagina: int) -> str:
    """Nome de arquivo unico para o cache de miniaturas. Usa um hash do
    CAMINHO COMPLETO (nao so o nome do arquivo): PDFs com nomes numericos
    repetidos em pastas diferentes (ex: 00000520.pdf em duas pastas distintas)
    sao comuns nesse tipo de arquivo e, usando so o stem, a miniatura de um
    arquivo poderia vazar/aparecer para o outro por engano na tela de revisao."""
    h = hashlib.sha1(str(pdf_path).encode("utf-8")).hexdigest()[:16]
    return f"{h}_{pagina}.png"


def cache_pronto(pdf_path: Path, cache_dir: Path) -> bool:
    return _cache_path(pdf_path, cache_dir).exists()


# --------------------------------------------------------------------------
# Busca de inicio / fim do sub-documento
# --------------------------------------------------------------------------

def buscar_ocorrencias(textos, chave: str):
    """Retorna lista de indices (0-based) de paginas cujo texto contem a chave."""
    pad = padrao_busca(chave)
    return [i for i, t in enumerate(textos) if pad.search(normalizar(t))]


def sugerir_fim(textos, inicio: int, chave: str, max_paginas=None, limite_chars_capa: int = 320):
    """Sugere a pagina (exclusiva) onde o proximo documento comeca, procurando a
    proxima capa do mesmo tipo (mesmas palavras-chave, texto curto = pagina de
    capa, nao de conteudo)."""
    pad = padrao_generico(chave)
    limite = len(textos) if max_paginas is None else min(len(textos), inicio + max_paginas)
    if pad:
        for i in range(inicio + 1, limite):
            texto_norm = normalizar(textos[i])
            if len(texto_norm) <= limite_chars_capa and pad.search(texto_norm):
                return i
    return limite


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


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
import traceback
import unicodedata
from pathlib import Path

from logger import log

from pypdf import PdfReader, PdfWriter

try:
    import pytesseract
    from pdf2image import convert_from_path
    OCR_DISPONIVEL = True
except ImportError:
    OCR_DISPONIVEL = False


# Limite de quantas paginas podem ser OCR-adas AO MESMO TEMPO no servidor
# inteiro (buscas interativas + sincronizacao em segundo plano, tudo
# compartilha o mesmo limite). Cada OCR chama processos externos (Poppler +
# Tesseract) que consomem CPU/memoria; sem esse limite, varias buscas ou uma
# sincronizacao grande rodando junto podem sobrecarregar o servidor (ou o
# Docker Desktop/WSL2 no Windows) a ponto de travar a maquina inteira.
# Ajustavel via variavel de ambiente OCR_CONCORRENTE (padrao: 2).
_OCR_SEMAFORO = threading.Semaphore(int(os.environ.get("OCR_CONCORRENTE", "1")))


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
        prontas = sum(1 for t in textos if t is not None)
        pendentes = len(textos) - prontas
        log.debug("Cache existente para %s: %d paginas (%d prontas, %d pendentes)",
                  pdf_path.name, len(textos), prontas, pendentes)
        return textos, len(textos)

    log.info("Sem cache para %s — lendo PDF para iniciar cache", pdf_path.name)
    reader = PdfReader(str(pdf_path))
    total = len(reader.pages)
    textos = []
    nativas = 0
    for page in reader.pages:
        # extrair texto nativo e' rapido (nao precisa de OCR/imagem), entao
        # fazemos isso pra todas as paginas de uma vez. So fica None quando
        # a pagina realmente precisa de OCR (PDF escaneado).
        t = page.extract_text() or ""
        if len(t.strip()) >= 15:
            textos.append(t)
            nativas += 1
        else:
            textos.append(None)
    log.info("PDF %s: %d paginas total, %d com texto nativo, %d precisam OCR",
             pdf_path.name, total, nativas, total - nativas)
    return textos, total


def _persistir_cache(pdf_path: Path, cache_dir: Path, textos):
    """Salva o cache de forma atomica (escreve em .tmp e renomeia por cima).

    Em pastas de REDE (Windows/SMB), esse renomear as vezes falha com
    'Acesso negado' de forma passageira (ex: antivirus escaneando o arquivo
    por uma fracao de segundo, ou outro processo segurando o arquivo por um
    instante) - tenta de novo algumas vezes antes de desistir, em vez de
    derrubar a sincronizacao/busca inteira por causa de uma falha momentanea
    num unico arquivo."""
    cache_file = _cache_path(pdf_path, cache_dir)
    tmp = cache_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(textos, ensure_ascii=False), encoding="utf-8")

    tentativas = 5
    for i in range(tentativas):
        try:
            tmp.replace(cache_file)
            return
        except (PermissionError, OSError):
            if i == tentativas - 1:
                # esgotou as tentativas - remove o .tmp pra nao deixar lixo
                # acumulando, e desiste (levanta o erro pra quem chamou
                # decidir o que fazer, ex: pular este arquivo e seguir pros
                # outros, em vez de travar tudo)
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            time.sleep(0.5 * (i + 1))  # espera um pouco mais a cada tentativa


def _garantir_pagina_ocr(pdf_path: Path, textos: list, indice: int,
                          dpi: int = 150, lang: str = "por"):
    """OCR-a a pagina `indice` se ainda nao tiver texto (in-place em `textos`).

    Passa pelo _OCR_SEMAFORO: se ja tiver muitas paginas sendo OCR-adas ao
    mesmo tempo (por outras buscas ou pela sincronizacao), esta chamada
    espera a vez em vez de somar mais carga no servidor."""
    if textos[indice] is not None:
        return
    if not OCR_DISPONIVEL:
        raise RuntimeError(
            "Este PDF e escaneado (sem texto) mas pytesseract/pdf2image nao "
            "estao instalados. Veja o README para instalar o Tesseract."
        )
    with _OCR_SEMAFORO:
        imgs = convert_from_path(str(pdf_path), dpi=dpi, first_page=indice + 1, last_page=indice + 1)
        textos[indice] = pytesseract.image_to_string(imgs[0], lang=lang)


def extrair_texto_paginas(pdf_path: Path, cache_dir: Path, dpi: int = 150, lang: str = "por",
                           progresso_callback=None):
    """Forca o OCR/leitura de TODAS as paginas e retorna a lista completa de
    textos. Usado pela busca antiga (mantido por compatibilidade) e pela
    sincronizacao de cache em segundo plano (ver app.py)."""
    log.info("extrair_texto_paginas INICIO: %s", pdf_path.name)
    with lock_para(pdf_path):
        textos, total = _carregar_ou_iniciar_cache(pdf_path, cache_dir)
        mudou = False
        ocr_count = 0
        for i in range(total):
            if textos[i] is None:
                _garantir_pagina_ocr(pdf_path, textos, i, dpi=dpi, lang=lang)
                mudou = True
                ocr_count += 1
            if progresso_callback:
                progresso_callback(i + 1, total)
            if mudou and (i + 1) % 10 == 0:
                # salva a cada 10 paginas (nao so no final) - se o processo for
                # interrompido no meio de um arquivo grande, nao perde tudo
                _persistir_cache(pdf_path, cache_dir, textos)
        if mudou:
            _persistir_cache(pdf_path, cache_dir, textos)
        log.info("extrair_texto_paginas FIM: %s — %d paginas total, %d OCR realizados",
                 pdf_path.name, total, ocr_count)
        return textos


def cache_completo(pdf_path: Path, cache_dir: Path) -> bool:
    """Checa (rapido, so' le o JSON do cache) se este PDF ja esta 100%
    processado - usado pela sincronizacao pra pular arquivos que ja estao
    prontos, sem precisar reabrir o PDF."""
    cache_file = _cache_path(pdf_path, cache_dir)
    if not cache_file.exists():
        log.debug("cache_completo: %s — SEM cache (arquivo nao existe)", pdf_path.name)
        return False
    try:
        textos = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("cache_completo: %s — erro ao ler cache: %s", pdf_path.name, e)
        return False
    total = len(textos)
    prontas = sum(1 for t in textos if t is not None)
    completo = prontas == total
    if not completo:
        log.debug("cache_completo: %s — INCOMPLETO (%d/%d paginas prontas)",
                  pdf_path.name, prontas, total)
    else:
        log.debug("cache_completo: %s — OK (%d paginas)", pdf_path.name, total)
    return completo


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
            resultado = f.read(5) == b"%PDF-"
        if not resultado:
            log.debug("eh_pdf: %s — NAO e PDF (assinatura ausente)", caminho.name)
        return resultado
    except OSError as e:
        log.warning("eh_pdf: %s — ERRO ao ler arquivo (tratado como nao-PDF): %s",
                    caminho.name, e)
        return False


def _cache_lista_path(pasta: Path) -> Path:
    return pasta / ".ocr_cache" / "_lista_pdfs.json"


def listar_pdfs(pasta: Path):
    """Lista todos os PDFs de verdade dentro da pasta, ignorando subpastas
    como .ocr_cache.

    Duas otimizacoes importantes para pastas de REDE com muitos arquivos
    (onde abrir cada arquivo tem uma latencia real):

    1) Um arquivo que ja termina em .pdf e' aceito direto pelo nome, sem
       abrir (checar extensao nao faz I/O nenhum).
    2) Para os que NAO tem extensao .pdf (comum nesses lotes numerados, ex:
       '00000520.001'), o resultado da checagem de conteudo fica guardado
       num arquivo de cache (.ocr_cache/_lista_pdfs.json) - assim, cada
       arquivo so' precisa ser aberto e conferido UMA VEZ NA VIDA. Da segunda
       vez em diante (proxima busca, proxima sincronizacao), o resultado ja
       'e conhecido e nenhum arquivo novo precisa ser aberto pela rede."""
    log.info("listar_pdfs INICIO: %s", pasta)
    cache_lista = _cache_lista_path(pasta)
    try:
        conhecidos = json.loads(cache_lista.read_text(encoding="utf-8"))
        log.debug("listar_pdfs: cache de lista carregado com %d entradas", len(conhecidos))
    except (OSError, json.JSONDecodeError) as e:
        conhecidos = {}
        log.debug("listar_pdfs: sem cache de lista (ou erro: %s) — verificando todos", e)

    try:
        candidatos = sorted(
            p for p in pasta.iterdir()
            if p.is_file() and not p.name.startswith(".")
        )
    except OSError as e:
        log.error("listar_pdfs: ERRO ao listar pasta %s: %s", pasta, e)
        return []

    log.info("listar_pdfs: %d arquivos candidatos encontrados em %s", len(candidatos), pasta)

    pdfs = []
    ignorados_nao_pdf = 0
    ignorados_erro = 0
    aceitos_extensao = 0
    aceitos_conteudo = 0
    mudou = False
    for p in candidatos:
        if p.suffix.lower() == ".pdf":
            pdfs.append(p)
            aceitos_extensao += 1
            continue
        resultado = conhecidos.get(p.name)
        if resultado is None:
            resultado = eh_pdf(p)
            conhecidos[p.name] = resultado
            mudou = True
        if resultado:
            pdfs.append(p)
            aceitos_conteudo += 1
        else:
            ignorados_nao_pdf += 1

    if mudou:
        cache_lista.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_lista.with_suffix(".tmp")
        tmp.write_text(json.dumps(conhecidos, ensure_ascii=False), encoding="utf-8")
        tmp.replace(cache_lista)

    log.info("listar_pdfs FIM: %d PDFs encontrados (%d por extensao, %d por conteudo), "
             "%d ignorados (nao-PDF)",
             len(pdfs), aceitos_extensao, aceitos_conteudo, ignorados_nao_pdf)
    return pdfs


def nome_arquivo_saida(chave: str, inicio: int, fim: int) -> str:
    base = re.sub(r"[^a-zA-Z0-9_-]+", "_", chave.strip()).strip("_")
    return f"{base}_pag{inicio + 1}-{fim}.pdf"
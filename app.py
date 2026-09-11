import io
import os
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, request, render_template, redirect, url_for, send_file, flash, jsonify, session

import nucleo
from logger import log

app = Flask(__name__)
# Em producao, defina SECRET_KEY no .env (qualquer string aleatoria longa).
# Sem isso os flash messages (avisos) usam uma chave previsivel - baixo risco
# aqui pois nao guardamos dados sensiveis na sessao, mas e boa pratica.
app.secret_key = os.environ.get("SECRET_KEY", "troque-esta-chave-em-producao")

# Guarda o estado das buscas em memoria (app local; se preferir persistencia
# entre reinicializacoes, troque por JSON ou sqlite). Cada busca roda numa
# THREAD em segundo plano (ver executar_busca), entao a requisicao HTTP do
# POST /buscar volta quase instantaneamente - o processamento pesado (OCR)
# NUNCA fica preso ao tempo-limite do servidor web. Isso e' importante com
# arquivos grandes: antes, uma busca demorada podia estourar o timeout do
# gunicorn e derrubar o worker inteiro, tirando TODOS os usuarios do ar, nao
# so quem fez aquela busca.
BUSCAS = {}
BUSCAS_LOCK = threading.Lock()

def carregar_env():
    """Carrega variaveis de ambiente do arquivo .env se existir (no host ou container)."""
    candidatos = [
        Path(__file__).parent / ".env",
        Path.cwd() / ".env",
    ]
    for env_path in candidatos:
        if env_path.is_file():
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip("'\"")
                            if k:
                                os.environ[k] = v
            except Exception:
                pass
            break

carregar_env()


def obter_pasta_padrao_entrada():
    """Resolve a pasta de entrada padrao a partir do .env, ambiente ou fallbacks."""
    for c in ["PASTA_ENTRADA", "ENTRADA_PDF", "ENTRADA_HOST"]:
        val = os.environ.get(c, "").strip()
        if val:
            return val

    for candidato in ["/dados/entrada", "./dados/entrada"]:
        if Path(candidato).exists():
            return candidato

    data_roots = [r.strip() for r in os.environ.get("DATA_ROOTS", "").split(";") if r.strip()]
    if data_roots:
        return data_roots[0]

    return "./dados/entrada"


def obter_pasta_padrao_saida():
    """Resolve a pasta de saida padrao a partir do .env, ambiente ou fallbacks."""
    for c in ["PASTA_SAIDA", "SAIDA_PDF", "SAIDA_HOST"]:
        val = os.environ.get(c, "").strip()
        if val:
            return val

    for candidato in ["/dados/saida", "./dados/saida"]:
        if Path(candidato).exists():
            return candidato

    data_roots = [r.strip() for r in os.environ.get("DATA_ROOTS", "").split(";") if r.strip()]
    if len(data_roots) > 1:
        return data_roots[1]
    elif data_roots:
        return data_roots[0]

    return "./dados/saida"

# Pasta temporaria onde renderizamos as miniaturas das paginas para previa
CACHE_IMG = Path(__file__).parent / "static" / "paginas"
CACHE_IMG.mkdir(parents=True, exist_ok=True)

# Estado da sincronizacao de cache em segundo plano (ver executar_sincronizacao).
# So' uma sincronizacao roda por vez (protegido por SINCRONIZACAO_LOCK) - clicar
# de novo enquanto ja esta rodando so' mostra o progresso atual, nao inicia outra.
SINCRONIZACAO = {"rodando": False, "erros": []}
SINCRONIZACAO_LOCK = threading.Lock()


# Limite de seguranca: quantas ocorrencias no maximo uma busca traz. Sem
# isso, uma chave muito generica (ex: "recibo") numa pasta de 80GB faria o
# sistema escanear TUDO atras de mais ocorrencias. Com o limite, a busca
# para de procurar mais assim que atinge esse numero (e avisa na tela).
MAX_OCORRENCIAS = 25


def _atualizar_busca(busca_id, **campos):
    with BUSCAS_LOCK:
        if busca_id in BUSCAS:
            BUSCAS[busca_id].update(campos)


def executar_busca(busca_id, pasta_entrada, cache_dir, chave):
    """Roda em uma thread separada - NAO tem acesso a` sessao/request do
    Flask (por isso nao pode usar flash() aqui; erros vao direto no dict
    BUSCAS, e a tela de espera os exibe).

    A listagem dos PDFs (nucleo.listar_pdfs) tambem roda AQUI DENTRO, nao no
    request principal: em pastas de rede com muitos arquivos, so' listar
    (confirmando que cada arquivo e' PDF de verdade pelo conteudo) pode levar
    minutos - fazer isso fora da thread prenderia o POST /buscar por esse
    tempo todo, reabrindo risco de estourar o timeout do servidor.

    Procura em TODOS os PDFs da pasta (nao para no primeiro arquivo com
    ocorrencia), ate encontrar MAX_OCORRENCIAS no total ou acabar os
    arquivos - assim uma chave como "folha de pagamento", que existe em
    varios meses/anos espalhados em arquivos diferentes, traz todas elas."""
    def progresso_callback(pdf_nome):
        def cb(pagina_atual, total_paginas):
            _atualizar_busca(busca_id, progresso={
                "arquivo": pdf_nome, "pagina": pagina_atual, "total": total_paginas,
            })
        return cb

    pdfs = nucleo.listar_pdfs(pasta_entrada)
    if not pdfs:
        _atualizar_busca(busca_id, status="vazio",
                          mensagem=f"Nenhum PDF encontrado em {pasta_entrada}")
        return

    resultados = []
    erros = []
    limite_atingido = False
    for pdf_path in pdfs:
        restante = MAX_OCORRENCIAS - len(resultados)
        if restante <= 0:
            limite_atingido = True
            break
        try:
            ocorrencias = nucleo.buscar_ocorrencias_no_pdf(
                pdf_path, cache_dir, chave,
                progresso_callback=progresso_callback(pdf_path.name),
                max_ocorrencias=restante,
            )
        except Exception as e:
            # mesma logica da sincronizacao: um arquivo com problema (rede,
            # permissao, corrompido) nao pode travar a busca nos demais.
            erros.append(f"{pdf_path.name}: {e}")
            continue
        for r in ocorrencias:
            resultados.append({
                "pdf": str(pdf_path),
                "pdf_nome": pdf_path.name,
                "inicio": r["inicio"],
                "fim_sugerido": r["fim_sugerido"],
                "total_paginas": r["total_paginas"],
                "trecho": r["trecho"],
            })
        if len(resultados) >= MAX_OCORRENCIAS:
            limite_atingido = True
            break

    if resultados:
        _atualizar_busca(busca_id, status="pronto", resultados=resultados,
                          limite_atingido=limite_atingido)
    elif erros:
        _atualizar_busca(busca_id, status="erro", mensagem="; ".join(erros))
    else:
        _atualizar_busca(busca_id, status="vazio",
                          mensagem=f"Nenhuma ocorrencia de \"{chave}\" encontrada.")


def executar_sincronizacao(pasta_entrada, cache_dir):
    """Roda em segundo plano: passa por TODOS os PDFs da pasta e faz o OCR
    completo de cada um (pulando os que ja estao 100% em cache), preenchendo
    o cache com calma ANTES de alguem precisar buscar. Como usa o mesmo
    _OCR_SEMAFORO das buscas normais (ver nucleo.py), nunca soma carga demais
    no servidor - se OCR_CONCORRENTE=2, no maximo 2 paginas sao processadas
    ao mesmo tempo, seja essa sincronizacao ou uma busca de usuario rodando
    junto."""
    try:
        log.info("="*60)
        log.info("SINCRONIZACAO INICIADA: %s", pasta_entrada)
        log.info("="*60)
        # rodando=True e' setado JA' AQUI, antes de listar os arquivos - em
        # pastas de rede com muitos itens, so' a listagem (abrindo cada
        # arquivo pra confirmar que e' PDF de verdade) pode levar minutos.
        # Sem isso, a tela ficava "parada" sem mostrar nada durante esse
        # tempo todo, porque o estado ainda dizia rodando=False.
        with SINCRONIZACAO_LOCK:
            SINCRONIZACAO.update({
                "rodando": True, "pasta": str(pasta_entrada), "preparando": True,
                "total_arquivos": 0, "ja_prontos": 0, "processados": 0,
                "arquivo_atual": None, "pagina_atual": 0, "pagina_total": 0,
                "erro": None, "erros": [],
            })

        log.info("Listando PDFs em %s...", pasta_entrada)
        pdfs = nucleo.listar_pdfs(pasta_entrada)
        log.info("Total de PDFs encontrados: %d", len(pdfs))

        log.info("Verificando cache de cada PDF...")
        pendentes = []
        ja_prontos_nomes = []
        for p in pdfs:
            if nucleo.cache_completo(p, cache_dir):
                ja_prontos_nomes.append(p.name)
            else:
                pendentes.append(p)
        log.info("Resultado: %d ja prontos, %d pendentes de processamento",
                 len(ja_prontos_nomes), len(pendentes))

        with SINCRONIZACAO_LOCK:
            if not SINCRONIZACAO["rodando"]:
                log.warning("Sincronizacao CANCELADA durante listagem")
                return  # cancelado enquanto ainda estava listando os arquivos
            SINCRONIZACAO.update({
                "preparando": False,
                "total_arquivos": len(pdfs), "ja_prontos": len(pdfs) - len(pendentes),
            })

        def progresso(pagina_atual, pagina_total):
            with SINCRONIZACAO_LOCK:
                SINCRONIZACAO["pagina_atual"] = pagina_atual
                SINCRONIZACAO["pagina_total"] = pagina_total

        erros_acumulados = []
        for idx, pdf_path in enumerate(pendentes):
            with SINCRONIZACAO_LOCK:
                if not SINCRONIZACAO["rodando"]:
                    log.warning("Sincronizacao CANCELADA pelo usuario (apos %d/%d pendentes)",
                                idx, len(pendentes))
                    break  # cancelado (ver /sincronizar/cancelar)
                SINCRONIZACAO["arquivo_atual"] = pdf_path.name
            try:
                log.info("Processando [%d/%d pendentes]: %s",
                         idx + 1, len(pendentes), pdf_path.name)
                nucleo.extrair_texto_paginas(pdf_path, cache_dir, progresso_callback=progresso)
            except Exception as e:
                # qualquer falha num arquivo especifico (rede instavel,
                # arquivo corrompido, permissao, etc) NAO pode derrubar a
                # sincronizacao inteira - registra o erro e segue pros
                # proximos milhares de arquivos.
                msg_erro = f"{pdf_path.name}: {e}"
                log.error("ERRO ao processar %s: %s", pdf_path.name, e, exc_info=True)
                erros_acumulados.append(msg_erro)
                with SINCRONIZACAO_LOCK:
                    SINCRONIZACAO["erro"] = msg_erro  # ultimo erro (compatibilidade UI)
                    SINCRONIZACAO["erros"] = list(erros_acumulados)  # todos os erros
                continue
            with SINCRONIZACAO_LOCK:
                SINCRONIZACAO["processados"] += 1

        # Resumo final
        with SINCRONIZACAO_LOCK:
            processados_final = SINCRONIZACAO["processados"]
        log.info("="*60)
        log.info("SINCRONIZACAO FINALIZADA")
        log.info("  Pasta: %s", pasta_entrada)
        log.info("  Total PDFs encontrados: %d", len(pdfs))
        log.info("  Ja estavam prontos: %d", len(ja_prontos_nomes))
        log.info("  Pendentes processados: %d", len(pendentes))
        log.info("  Processados com sucesso: %d", processados_final)
        log.info("  Erros: %d", len(erros_acumulados))
        if erros_acumulados:
            log.info("  Lista de erros:")
            for err in erros_acumulados:
                log.info("    - %s", err)
        log.info("="*60)
    finally:
        with SINCRONIZACAO_LOCK:
            SINCRONIZACAO["rodando"] = False
            SINCRONIZACAO["arquivo_atual"] = None


@app.route("/", methods=["GET"])
def index():
    entrada_padrao = obter_pasta_padrao_entrada()
    saida_padrao = obter_pasta_padrao_saida()
    return render_template("index.html", entrada_padrao=entrada_padrao, saida_padrao=saida_padrao)


@app.route("/sincronizar", methods=["POST"])
def sincronizar():
    entrada_bruta = request.form["pasta_entrada"].strip()
    try:
        pasta_entrada = nucleo.validar_caminho(entrada_bruta)
    except nucleo.CaminhoNaoPermitido as e:
        flash(str(e))
        return redirect(url_for("index"))
    if not pasta_entrada.is_dir():
        flash(f"Pasta nao encontrada: {pasta_entrada}")
        return redirect(url_for("index"))

    with SINCRONIZACAO_LOCK:
        ja_rodando = SINCRONIZACAO.get("rodando")
    if not ja_rodando:
        cache_dir = pasta_entrada / ".ocr_cache"
        thread = threading.Thread(
            target=executar_sincronizacao, args=(pasta_entrada, cache_dir), daemon=True
        )
        thread.start()

    flash("Sincronizacao iniciada/em andamento - acompanhe o progresso na tela inicial.")
    return redirect(url_for("index"))


@app.route("/sincronizar/cancelar", methods=["POST"])
def sincronizar_cancelar():
    with SINCRONIZACAO_LOCK:
        SINCRONIZACAO["rodando"] = False
    return redirect(url_for("index"))


@app.route("/sincronizar/estado")
def sincronizar_estado():
    with SINCRONIZACAO_LOCK:
        estado = dict(SINCRONIZACAO)
        estado["total_erros"] = len(estado.get("erros", []))
        return jsonify(estado)


@app.route("/logs")
def ver_logs():
    """Exibe as ultimas linhas do log de sincronizacao no browser,
    para diagnostico sem precisar acessar o servidor por SSH."""
    from logger import _resolver_pasta_logs
    arquivo_log = _resolver_pasta_logs() / "sincronizacao.log"
    n_linhas = int(request.args.get("n", 200))

    if not arquivo_log.exists():
        return "<pre>Nenhum log encontrado ainda.</pre>", 200

    try:
        conteudo = arquivo_log.read_text(encoding="utf-8")
        linhas = conteudo.splitlines()
        ultimas = linhas[-n_linhas:] if len(linhas) > n_linhas else linhas
        texto = "\n".join(ultimas)
    except OSError as e:
        texto = f"Erro ao ler log: {e}"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Logs de Sincronização</title>
<style>
body {{ background: #1a1a2e; color: #e0e0e0; font-family: 'Courier New', monospace; font-size: 12.5px; padding: 20px; margin: 0; }}
pre {{ white-space: pre-wrap; word-wrap: break-word; line-height: 1.6; }}
h1 {{ color: #64ffda; font-size: 18px; margin-bottom: 8px; }}
.info {{ color: #888; font-size: 12px; margin-bottom: 16px; }}
a {{ color: #64ffda; }}
.WARNING {{ color: #ffd93d; }}
.ERROR {{ color: #ff6b6b; font-weight: bold; }}
.INFO {{ color: #a8dadc; }}
</style>
</head><body>
<h1>📋 Logs de Sincronização</h1>
<p class="info">Arquivo: {arquivo_log} — mostrando últimas {len(ultimas)} de {len(linhas)} linhas
 · <a href="/logs?n={len(linhas)}">ver tudo</a> · <a href="/">← voltar</a></p>
<pre>{texto}</pre>
<script>window.scrollTo(0, document.body.scrollHeight);</script>
</body></html>""", 200


@app.route("/buscar", methods=["POST"])
def buscar():
    nucleo.limpar_buscas_antigas(BUSCAS)
    session.pop("_flashes", None)

    chave = request.form["chave"].strip()
    entrada_bruta = request.form["pasta_entrada"].strip()
    saida_bruta = request.form["pasta_saida"].strip()

    # Estas validacoes ficam SINCRONAS de proposito (sao rapidas: checar
    # caminho, pasta existir, chave vazia) - dao feedback imediato de erro
    # de digitacao, sem precisar passar pela tela de espera.
    try:
        pasta_entrada = nucleo.validar_caminho(entrada_bruta)
        pasta_saida = nucleo.validar_caminho(saida_bruta)
    except nucleo.CaminhoNaoPermitido as e:
        flash(str(e))
        return redirect(url_for("index"))

    if not pasta_entrada.is_dir():
        flash(f"Pasta de entrada nao encontrada: {pasta_entrada}")
        return redirect(url_for("index"))
    if not chave:
        flash("Digite uma palavra-chave para buscar.")
        return redirect(url_for("index"))

    # NAO chamamos nucleo.listar_pdfs() aqui: em pastas de rede com muitos
    # arquivos, so' listar (abrindo cada arquivo pra confirmar que e' PDF de
    # verdade) pode levar minutos - se isso rodasse aqui, o POST inteiro
    # ficaria preso de novo, reabrindo o risco de estourar o timeout do
    # servidor. Essa listagem vai para dentro da thread em segundo plano
    # (ver executar_busca).
    cache_dir = pasta_entrada / ".ocr_cache"
    busca_id = uuid.uuid4().hex[:10]
    with BUSCAS_LOCK:
        BUSCAS[busca_id] = {
            "status": "processando",
            "chave": chave,
            "pasta_saida": str(pasta_saida),
            "progresso": {"arquivo": None, "pagina": 0, "total": 0},
            "criada_em": time.time(),
        }

    thread = threading.Thread(
        target=executar_busca, args=(busca_id, pasta_entrada, cache_dir, chave), daemon=True
    )
    thread.start()

    return redirect(url_for("aguardando", busca_id=busca_id))


@app.route("/aguardando/<busca_id>")
def aguardando(busca_id):
    busca = BUSCAS.get(busca_id)
    if not busca:
        flash("Busca expirada, faca a busca novamente.")
        return redirect(url_for("index"))
    if busca["status"] == "pronto":
        return redirect(url_for("resultados", busca_id=busca_id))
    return render_template("aguardando.html", busca_id=busca_id, chave=busca["chave"])


@app.route("/estado/<busca_id>")
def estado(busca_id):
    """Endpoint consultado (polling) pela tela de espera para saber quando a
    busca em segundo plano terminou, e mostrar o progresso enquanto isso."""
    busca = BUSCAS.get(busca_id)
    if not busca:
        return jsonify({"status": "expirada"})
    return jsonify({
        "status": busca["status"],
        "progresso": busca.get("progresso"),
        "mensagem": busca.get("mensagem"),
    })


@app.route("/resultados/<busca_id>")
def resultados(busca_id):
    busca = BUSCAS.get(busca_id)
    if not busca:
        flash("Busca expirada, faca a busca novamente.")
        return redirect(url_for("index"))
    if busca["status"] != "pronto":
        return redirect(url_for("aguardando", busca_id=busca_id))
    return render_template("resultados.html", busca_id=busca_id, chave=busca["chave"],
                            resultados=busca["resultados"],
                            limite_atingido=busca.get("limite_atingido", False))


@app.route("/revisar/<busca_id>/<int:indice>")
def revisar(busca_id, indice):
    busca = BUSCAS.get(busca_id)
    if not busca:
        flash("Busca expirada, faca a busca novamente.")
        return redirect(url_for("index"))

    r = busca["resultados"][indice]
    # inicio/fim ajustaveis via querystring (1-based na interface)
    inicio = int(request.args.get("inicio", r["inicio"] + 1))
    fim = int(request.args.get("fim", r["fim_sugerido"]))
    inicio = max(1, min(inicio, r["total_paginas"]))
    fim = max(inicio, min(fim, r["total_paginas"]))

    return render_template(
        "revisar.html",
        busca_id=busca_id, indice=indice, chave=busca["chave"],
        pdf_nome=r["pdf_nome"], total_paginas=r["total_paginas"],
        inicio=inicio, fim=fim,
        n_paginas=fim - inicio + 1,
    )


@app.route("/previa/<busca_id>/<int:indice>.pdf")
def previa_pdf(busca_id, indice):
    """PDF de previa (sem OCR, so' reestrutura paginas - rapido) com o
    intervalo [inicio, fim] atual, pra mostrar num <iframe> com o leitor
    nativo do navegador (rolagem e zoom de verdade), em vez de miniaturas."""
    busca = BUSCAS.get(busca_id)
    if not busca or busca.get("status") != "pronto":
        return "busca expirada", 404

    r = busca["resultados"][indice]
    pdf_path = Path(nucleo.normalizar_caminho(r["pdf"]))

    inicio = int(request.args.get("inicio", r["inicio"] + 1))
    fim = int(request.args.get("fim", r["fim_sugerido"]))
    inicio = max(1, min(inicio, r["total_paginas"]))
    fim = max(inicio, min(fim, r["total_paginas"]))

    pdf_bytes = nucleo.gerar_previa_bytes(pdf_path, inicio - 1, fim)
    resp = send_file(io.BytesIO(pdf_bytes), mimetype="application/pdf",
                      as_attachment=False, download_name="previa.pdf")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/pagina/<busca_id>/<int:indice>/<int:pagina>.png")
def pagina_png(busca_id, indice, pagina):
    """Renderiza (com cache) a pagina `pagina` (1-based) do PDF do resultado como PNG,
    para exibir como previa na tela de revisao."""
    busca = BUSCAS.get(busca_id)
    if not busca:
        return "busca expirada", 404
    r = busca["resultados"][indice]
    # normalizar_caminho garante que o caminho funciona dentro do container
    pdf_path = Path(nucleo.normalizar_caminho(r["pdf"]))

    # chave de cache pelo caminho completo (nao so o nome do arquivo) - evita
    # mostrar a miniatura de um PDF errado quando dois arquivos em pastas
    # diferentes tem o mesmo nome (comum nesses lotes numerados)
    cache_file = CACHE_IMG / nucleo.chave_cache_imagem(pdf_path, pagina)
    if not cache_file.exists():
        from pdf2image import convert_from_path
        imgs = convert_from_path(str(pdf_path), dpi=110, first_page=pagina, last_page=pagina)
        imgs[0].save(cache_file, "PNG")

    return send_file(cache_file, mimetype="image/png")


@app.route("/confirmar/<busca_id>/<int:indice>", methods=["POST"])
def confirmar(busca_id, indice):
    busca = BUSCAS.get(busca_id)
    if not busca:
        flash("Busca expirada, faca a busca novamente.")
        return redirect(url_for("index"))

    r = busca["resultados"][indice]
    inicio_1based = int(request.form["inicio"])
    fim_1based = int(request.form["fim"])  # ultima pagina incluida, 1-based

    pdf_path = Path(nucleo.normalizar_caminho(r["pdf"]))
    pasta_saida = Path(nucleo.normalizar_caminho(busca["pasta_saida"]))
    nome = nucleo.nome_arquivo_saida(busca["chave"], inicio_1based - 1, fim_1based)
    saida_path = pasta_saida / nome

    nucleo.extrair_sub_pdf(pdf_path, inicio_1based - 1, fim_1based, saida_path)

    return render_template("sucesso.html", caminho=str(saida_path.resolve()),
                            n_paginas=fim_1based - inicio_1based + 1)


if __name__ == "__main__":
    # Uso apenas para desenvolvimento local (fora do Docker). Em producao o
    # Dockerfile chama o gunicorn diretamente (veja CMD no Dockerfile) -
    # o servidor embutido do Flask nao foi feito pra atender varios usuarios
    # ao mesmo tempo.
    app.run(host="0.0.0.0", port=5000, threaded=True)
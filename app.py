import io
import os
import time
import uuid
from pathlib import Path

from flask import Flask, request, render_template, redirect, url_for, send_file, flash

import nucleo

app = Flask(__name__)
# Em producao, defina SECRET_KEY no .env (qualquer string aleatoria longa).
# Sem isso os flash messages (avisos) usam uma chave previsivel - baixo risco
# aqui pois nao guardamos dados sensiveis na sessao, mas e boa pratica.
app.secret_key = os.environ.get("SECRET_KEY", "troque-esta-chave-em-producao")

# Guarda o estado das buscas em memoria (app local, single-user; se
# preferir persistencia entre reinicializacoes, troque por JSON ou sqlite).
BUSCAS = {}

# Pasta temporaria onde renderizamos as miniaturas das paginas para previa
CACHE_IMG = Path(__file__).parent / "static" / "paginas"
CACHE_IMG.mkdir(parents=True, exist_ok=True)


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/buscar", methods=["POST"])
def buscar():
    nucleo.limpar_buscas_antigas(BUSCAS)

    chave = request.form["chave"].strip()
    entrada_bruta = request.form["pasta_entrada"].strip()
    saida_bruta = request.form["pasta_saida"].strip()

    # valida que os caminhos digitados estao dentro do que o administrador
    # autorizou (DATA_ROOTS no .env) - impede acessar pastas fora do escopo
    # combinado com a empresa, mesmo que o container tenha o disco inteiro montado.
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

    cache_dir = pasta_entrada / ".ocr_cache"
    pdfs = sorted(pasta_entrada.glob("*.pdf"))
    if not pdfs:
        flash(f"Nenhum PDF encontrado em {pasta_entrada}")
        return redirect(url_for("index"))

    resultados = []
    for pdf_path in pdfs:
        try:
            r = nucleo.buscar_no_pdf(pdf_path, cache_dir, chave)
        except RuntimeError as e:
            flash(f"{pdf_path.name}: {e}")
            continue
        if r is None:
            continue  # chave nao encontrada neste arquivo, tenta o proximo
        resultados.append({
            "pdf": str(pdf_path),
            "pdf_nome": pdf_path.name,
            "inicio": r["inicio"],
            "fim_sugerido": r["fim_sugerido"],
            "total_paginas": r["total_paginas"],
            "trecho": r["trecho"],
        })
        # para de varrer a pasta assim que achar o documento em algum arquivo -
        # evita rodar OCR nos demais PDFs (podem ser centenas) sem necessidade.
        # Se a mesma chave puder aparecer em mais de um arquivo na sua pasta,
        # remova este 'break' para listar todas as ocorrencias (mais lento).
        break

    if not resultados:
        flash(f"Nenhuma ocorrencia de \"{chave}\" encontrada nos PDFs de {pasta_entrada}.")
        return redirect(url_for("index"))

    busca_id = uuid.uuid4().hex[:10]
    BUSCAS[busca_id] = {
        "chave": chave,
        "pasta_saida": str(pasta_saida),
        "resultados": resultados,
        "criada_em": time.time(),
    }
    return redirect(url_for("resultados", busca_id=busca_id))


@app.route("/resultados/<busca_id>")
def resultados(busca_id):
    busca = BUSCAS.get(busca_id)
    if not busca:
        flash("Busca expirada, faca a busca novamente.")
        return redirect(url_for("index"))
    return render_template("resultados.html", busca_id=busca_id, chave=busca["chave"],
                            resultados=busca["resultados"])


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


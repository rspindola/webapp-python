# Imagem base
FROM python:3.11-slim

# Dependencias de sistema (Tesseract + Poppler)
# Instaladas automaticamente dentro da imagem. Nenhum usuario precisa instalar nada.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-por \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Dependencias Python
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Codigo da aplicacao
COPY . .

# Porta exposta
EXPOSE 5000

# Comando de inicializacao — gunicorn (servidor de producao), NAO o servidor
# de desenvolvimento do Flask. 1 worker + varias threads: como o estado das
# buscas fica em memoria (dict BUSCAS em app.py), precisa ser 1 processo so
# para todos os usuarios compartilharem o mesmo estado. As threads dao conta
# de atender ~10 usuarios simultaneos; o OCR (subprocess do Tesseract) libera
# o GIL do Python enquanto roda, entao nao trava as outras threads.
CMD ["gunicorn", "-w", "1", "--threads", "8", "--timeout", "600", "-b", "0.0.0.0:5000", "app:app"]

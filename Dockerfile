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

# Comando de inicializacao
CMD ["python", "app.py"]

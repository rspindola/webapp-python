# Extrator de sub-PDFs por palavra-chave

Sistema web que roda num **servidor Windows central** e e acessado por qualquer
usuario da rede pelo navegador — sem instalar absolutamente nada nos computadores
dos usuarios.

## Como funciona

1. Os PDFs ficam nas pastas normais do servidor Windows (`C:\dados\PDFs`, etc.).
2. O Docker roda o app nesse mesmo servidor.
3. Os usuarios abrem o navegador e acessam `http://ip-do-servidor` — pronto.
4. O usuario digita o caminho Windows normalmente (`C:\dados\PDFs`) — o sistema
   converte automaticamente para funcionar dentro do container.

---

## Instalacao no servidor (feita UMA unica vez pela TI)

### 1. Instalar o Docker Desktop

Baixe e instale o Docker Desktop para Windows:
https://www.docker.com/products/docker-desktop/

Durante a instalacao, mantenha a opcao **"Use WSL 2 instead of Hyper-V"** marcada.

Apos instalar, abra o Docker Desktop e aguarde o status ficar **"Running"**.

> **Importante:** Configure o Docker Desktop para iniciar automaticamente com o Windows:
> Abra o Docker Desktop > Settings > General > marque **"Start Docker Desktop when you log in"**.

### 2. Copiar o projeto para o servidor

Copie a pasta do projeto para o servidor (ex: `C:\extrator-pdf\`).

### 3. Configurar os discos no docker-compose.yml

O arquivo `docker-compose.yml` define quais discos e pastas do servidor ficam
acessiveis dentro do container. Abra o arquivo e edite a secao `volumes`.

Por padrao, os discos C: e D: ja estao configurados:

```yaml
volumes:
  - "C:/:/mnt/c"
  - "D:/:/mnt/d"
```

Se houver outros discos ou letras de drive mapeadas, adicione-os:

```yaml
  - "E:/:/mnt/e"
  - "Z:/:/mnt/z"
```

### 4. Pastas de rede (\\servidor\pasta)

Se os PDFs estiverem em uma pasta de rede (caminho UNC como `\\servidor\Cgeral`),
e necessario mapea-la como letra de drive no servidor Windows **antes** de subir
o Docker. Isso e feito uma unica vez:

**Abra o Prompt de Comando como Administrador e rode:**

```cmd
net use Z: \\servidor\Cgeral /persistent:yes
```

Substitua `Z:` pela letra desejada e `\\servidor\Cgeral` pelo caminho real da pasta.

Em seguida, adicione a nova letra no `docker-compose.yml`:

```yaml
volumes:
  - "C:/:/mnt/c"
  - "D:/:/mnt/d"
  - "Z:/:/mnt/z"    # <- adicionar esta linha
```

A partir dai, os usuarios digitam `Z:\PDFs` normalmente no formulario e o sistema
encontra os arquivos sem nenhuma configuracao adicional.

> **Dica:** Para verificar se o mapeamento funcionou, abra o Explorador de Arquivos
> e confirme que a letra Z: aparece em "Este Computador" com os arquivos corretos.

### 5. Subir o sistema

Abra o PowerShell ou Prompt de Comando na pasta do projeto e rode:

```
docker compose up -d --build
```

Isso vai:
- Baixar a imagem base Python (~200 MB, so na primeira vez)
- Instalar o Tesseract OCR e o Poppler **automaticamente** dentro do container
- Instalar as dependencias Python
- Iniciar o servidor

**Na primeira execucao leva alguns minutos. Nas proximas e instantaneo.**

### 6. Pronto

Abra o navegador em qualquer computador da rede e acesse:

```
http://IP_DO_SERVIDOR
```

Para descobrir o IP do servidor: abra o Prompt de Comando e rode `ipconfig`.
Procure pelo endereco **IPv4** da placa de rede local (ex: `192.168.1.10`).

---

## Uso diario

Na tela inicial, preencha:

- **Pasta de entrada**: caminho Windows onde estao os PDFs
  Exemplos: `C:\dados\PDFs 2024` ou `Z:\Cgeral\Documentos`
- **Pasta de saida**: onde salvar os PDFs extraidos
  Exemplos: `C:\dados\Extraidos` ou `Z:\Cgeral\Saida`
- **Palavra-chave**: ex. `fluxo de trabalho 2024`

O sistema busca nos PDFs, mostra previa das paginas para revisar e, ao confirmar,
salva o PDF na pasta de saida.

---

## Manutencao

### Parar o sistema
```
docker compose down
```

### Reiniciar
```
docker compose up -d
```

### Atualizar para nova versao
```
docker compose up -d --build
```

### Ver logs (para diagnosticar problemas)
```
docker compose logs -f
```

---

## Observacoes

- **Primeira busca em cada PDF e lenta** (OCR de todas as paginas). As seguintes
  sao quase instantaneas — o texto fica em cache em `<pasta_entrada>\.ocr_cache\`.
- A sugestao automatica de inicio/fim nao e 100% garantida — sempre revise
  visualmente na tela de previa antes de confirmar.
- O sistema e otimizado para documentos em **Portugues (PT-BR)**.
- O Docker Desktop precisa estar **aberto e rodando** no servidor. Configure-o
  para iniciar automaticamente com o Windows conforme indicado no passo 1.


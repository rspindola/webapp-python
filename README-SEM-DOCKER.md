# Rodar sem Docker — direto no Windows (para teste em 1 máquina)

Use este guia se o Docker Desktop não estiver funcionando na máquina (ex: erro
de "virtualização não detectada") e você quiser testar o sistema mesmo assim,
rodando o Python diretamente no Windows, sem container.

> Isso é só para teste/uso numa única máquina. Para várias pessoas acessarem
> ao mesmo tempo pela rede, o ideal continua sendo resolver o Docker depois
> (veja a nota no final).

---

## Passo 1 — Instalar o Python

1. Baixe em: https://www.python.org/downloads/windows/
2. Execute o instalador.
3. **Importante:** marque a caixa **"Add python.exe to PATH"** na primeira tela do instalador, antes de clicar em "Install Now".
4. Para confirmar que funcionou, abra o **PowerShell** (Menu Iniciar → digite `PowerShell`) e rode:
   ```
   python --version
   ```
   Deve mostrar algo como `Python 3.12.x`.

## Passo 2 — Instalar o Tesseract OCR (com pacote de português)

1. Baixe o instalador em: https://github.com/UB-Mannheim/tesseract/wiki
2. Execute o instalador.
3. Na tela de **seleção de componentes**, expanda "Language data" (ou similar) e marque **Portuguese**.
4. Conclua a instalação. Por padrão instala em:
   ```
   C:\Program Files\Tesseract-OCR\tesseract.exe
   ```
   Anote esse caminho — vamos precisar dele no Passo 5 se o app não achar o Tesseract sozinho.

## Passo 3 — Instalar o Poppler

1. Baixe o `.zip` em: https://github.com/oschwartz10612/poppler-windows/releases (pegue o release mais recente, o arquivo termina em `.zip`)
2. Extraia o conteúdo em uma pasta fixa, por exemplo: `C:\poppler`
3. Dentro dela deve existir uma subpasta parecida com `C:\poppler\Library\bin` — é esse caminho que importa.
4. Adicione esse caminho ao PATH do Windows:
   - Menu Iniciar → digite `variáveis de ambiente` → abra **"Editar as variáveis de ambiente do sistema"**
   - Clique em **Variáveis de Ambiente...**
   - Em "Variáveis do sistema", selecione **Path** → **Editar** → **Novo**
   - Cole: `C:\poppler\Library\bin` (ajuste se você extraiu em outro lugar)
   - OK em tudo, feche e reabra qualquer PowerShell que já estava aberto.

## Passo 4 — Copiar o projeto e instalar as dependências Python

1. Extraia o `.zip` do projeto (o que te enviei) em uma pasta, por exemplo: `C:\apps\extrator-pdf`
2. Abra o PowerShell **dentro dessa pasta**: navegue até ela no Explorador de Arquivos, segure **Shift** e clique com o botão direito em um espaço vazio → **"Abrir janela do PowerShell aqui"** (ou "Abrir no Terminal").
3. Rode:
   ```
   pip install flask pypdf pdf2image pytesseract
   ```
   (Não instale o `gunicorn` — ele não funciona no Windows nativo, só dentro do container Linux/Docker.)

## Passo 5 — Se o Tesseract não for encontrado automaticamente

Abra o arquivo `app.py` (pasta do projeto) em um editor de texto (Bloco de Notas serve) e adicione estas duas linhas logo após os `import`s do topo:

```python
import pytesseract
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
```

Ajuste o caminho se você instalou o Tesseract em outro lugar. Salve o arquivo.

*(Só faça isso se o Passo 7 der erro mencionando "tesseract is not installed". Se funcionar sem isso, pode pular.)*

## Passo 6 — Configurar as variáveis de ambiente da sessão

No mesmo PowerShell aberto na pasta do projeto, rode (ajustando o caminho para uma pasta real que você quer testar):

```powershell
$env:SECRET_KEY="teste123"
$env:DATA_ROOTS="C:\dados"
```

`DATA_ROOTS` é a(s) pasta(s) que o sistema vai aceitar como entrada/saída. Se quiser permitir mais de uma, separe por `;`, por exemplo:
```powershell
$env:DATA_ROOTS="C:\dados\PDFs;C:\dados\Extraidos"
```

> Atenção: essas variáveis valem só para essa janela do PowerShell. Se você fechar e abrir de novo, precisa rodar essas duas linhas outra vez antes do Passo 7.

## Passo 7 — Rodar o sistema

Ainda no mesmo PowerShell:

```
python app.py
```

Deve aparecer algo como:
```
Running on http://0.0.0.0:5000
```

Deixe essa janela do PowerShell aberta — é o "servidor" rodando. Fechar essa janela desliga o sistema.

## Passo 8 — Acessar pelo navegador

Abra o navegador (Chrome, Edge, etc.) nessa mesma máquina e acesse:

```
http://localhost:5000
```

Preencha o formulário:
- **Pasta de entrada**: uma pasta real dentro do que você colocou em `DATA_ROOTS` (ex: `C:\dados\PDFs`)
- **Pasta de saída**: idem (ex: `C:\dados\Extraidos`)
- **Palavra-chave**: o texto que você quer buscar

---

## Erros comuns

| Erro | Causa provável | Solução |
|---|---|---|
| `'python' não é reconhecido...` | Python não está no PATH | Reinstale marcando "Add to PATH", ou reabra o PowerShell |
| `Caminho fora das pastas permitidas` | A pasta digitada não está dentro do `DATA_ROOTS` | Ajuste o `$env:DATA_ROOTS` (Passo 6) ou digite um caminho dentro dele |
| `tesseract is not installed or it's not in your PATH` | O Tesseract não foi encontrado | Faça o Passo 5 |
| Erro relacionado a `poppler` / `Unable to get page count` | Poppler não está no PATH | Revise o Passo 3, reabra o PowerShell depois de mexer no PATH |
| Página não abre no navegador | O `python app.py` não está rodando, ou deu erro no terminal | Olhe o texto no PowerShell — o erro real aparece ali |

---

## Depois de resolver o Docker

Quando o Docker Desktop voltar a funcionar no servidor (veja a questão da
virtualização/BIOS), o mesmo projeto roda em container sem precisar mudar
nada de código — o `nucleo.py` já detecta sozinho se está rodando dentro do
Docker ou direto no Windows. Nesse caso, use o `docker-compose.yml` normal
(`docker compose up -d --build`), que já vem configurado com o servidor de
produção (gunicorn) certo para atender várias pessoas ao mesmo tempo pela
rede — o que este guia (`python app.py`) não faz, sendo indicado só pra teste
numa máquina só.

"""
Servico de log centralizado para o sistema de sincronizacao/cache de PDFs.

Registra em arquivo rotacionado tudo que acontece durante a sincronizacao:
quais arquivos foram processados, pulados, ignorados, ou deram erro - com
timestamps e detalhes suficientes para diagnosticar por que nem todos os
arquivos sao cacheados.

Uso:
    from logger import log
    log.info("mensagem")
    log.warning("algo suspeito: %s", detalhe)
    log.error("falha em %s", arquivo, exc_info=True)   # com traceback
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _resolver_pasta_logs() -> Path:
    """Resolve a pasta de logs, priorizando variavel de ambiente."""
    pasta = os.environ.get("LOG_DIR", "").strip()
    if pasta:
        return Path(pasta)
    return Path(__file__).parent / "dados" / "logs"


def configurar_logger() -> logging.Logger:
    """Cria e retorna o logger principal da aplicacao.

    - Arquivo rotacionado: max 10 MB, 5 backups (total ~60 MB no pior caso)
    - Formato: timestamp | nivel | mensagem
    - Tambem imprime no console (util em docker logs / terminal)
    """
    pasta_logs = _resolver_pasta_logs()
    pasta_logs.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("sincronizacao")
    # Evita duplicar handlers se configurar_logger() for chamado mais de uma vez
    # (ex: reload do Flask em modo debug)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # --- Handler de arquivo (rotacionado) ---
    arquivo = pasta_logs / "sincronizacao.log"
    fh = RotatingFileHandler(
        str(arquivo),
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fmt_arquivo = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh.setFormatter(fmt_arquivo)
    logger.addHandler(fh)

    # --- Handler de console ---
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    fmt_console = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )
    ch.setFormatter(fmt_console)
    logger.addHandler(ch)

    return logger


# Logger pronto para uso: `from logger import log`
log = configurar_logger()

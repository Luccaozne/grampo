"""Atualização automática: ao abrir, o Grampo olha a última versão publicada no GitHub, baixa em segundo plano e
instala quando a pessoa fecha o programa.

Só o que está nas Releases do repositório vale: o instalador tem de se chamar Grampo-<versão>.exe e bater com o
SHA-256 que o próprio GitHub informa para o arquivo. Nenhum arquivo do usuário sai do computador; o único acesso à
internet é este.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

from . import VERSAO

REPOSITORIO = "Luccaozne/grampo"
ENDERECO = f"https://api.github.com/repos/{REPOSITORIO}/releases/latest"
NOME_DO_INSTALADOR = re.compile(r"^Grampo-(\d+)\.(\d+)\.(\d+)\.exe$")
PASTA = Path(tempfile.gettempdir()) / "Grampo-atualizacao"
PRAZO = 20  # segundos para conversar com o GitHub

_trava = threading.Lock()
_estado = {"versao": None, "arquivo": None}  # versao: a nova que está sendo baixada ou já baixada


def versao_em_numeros(texto):
    achou = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", (texto or "").strip())
    return tuple(int(n) for n in achou.groups()) if achou else None


def escolher_instalador(release):
    """Da resposta do GitHub, o instalador novo: (versão, endereço, tamanho, sha256) ou None se não há nada a fazer."""
    versao = versao_em_numeros(release.get("tag_name"))
    if not versao or versao <= versao_em_numeros(VERSAO) or release.get("draft") or release.get("prerelease"):
        return None
    for ativo in release.get("assets", []):
        nome = NOME_DO_INSTALADOR.match(ativo.get("name", ""))
        sha = (ativo.get("digest") or "").removeprefix("sha256:").lower()
        endereco = ativo.get("browser_download_url", "")
        if (nome and tuple(int(n) for n in nome.groups()) == versao and re.fullmatch(r"[0-9a-f]{64}", sha)
                and endereco.startswith(f"https://github.com/{REPOSITORIO}/releases/download/")):
            return versao, endereco, int(ativo.get("size", 0)), sha
    return None  # sem SHA-256 publicado não instalamos: não dá para provar que o arquivo é o certo


def _pedir(endereco, cabecalhos=None):
    pedido = urllib.request.Request(endereco, headers={"User-Agent": f"Grampo/{VERSAO}", **(cabecalhos or {})})
    return urllib.request.urlopen(pedido, timeout=PRAZO)


def buscar_release():
    with _pedir(ENDERECO, {"Accept": "application/vnd.github+json"}) as resposta:
        return json.load(resposta)


def baixar(versao, endereco, tamanho, sha):
    """Baixa para a pasta temporária e confere o SHA-256. Devolve o caminho, ou None se o arquivo não confere."""
    PASTA.mkdir(parents=True, exist_ok=True)
    for velho in PASTA.glob("Grampo-*.exe*"):
        velho.unlink(missing_ok=True)
    destino = PASTA / f"Grampo-{'.'.join(map(str, versao))}.exe"
    parcial = destino.with_name(destino.name + ".parte")
    resumo = hashlib.sha256()
    with _pedir(endereco) as resposta, open(parcial, "wb") as saida:
        while bloco := resposta.read(1 << 20):
            resumo.update(bloco)
            saida.write(bloco)
    if resumo.hexdigest() != sha or (tamanho and parcial.stat().st_size != tamanho):
        parcial.unlink(missing_ok=True)
        return None
    parcial.replace(destino)
    return destino


def verificar():
    """Faz tudo: olha, baixa e confere. Qualquer falha (sem internet, GitHub fora) só deixa para a próxima vez."""
    try:
        achado = escolher_instalador(buscar_release())
        if not achado:
            return
        versao = ".".join(map(str, achado[0]))
        with _trava:
            _estado.update(versao=versao, arquivo=None)
        arquivo = baixar(*achado)
        if arquivo:
            with _trava:
                _estado["arquivo"] = arquivo
    except Exception:  # noqa: BLE001 - atualizar nunca pode atrapalhar quem está usando o programa
        pass


def comecar():
    """Chamar ao abrir o programa instalado. Não faz nada quando rodando do código-fonte."""
    if getattr(sys, "frozen", False):
        threading.Thread(target=verificar, daemon=True).start()


def situacao():
    with _trava:
        return {"versao": _estado["versao"], "pronta": bool(_estado["arquivo"])}


def instalar_ao_fechar():
    """Chamar quando o programa está fechando: se há versão baixada e conferida, roda o instalador sem janelas."""
    with _trava:
        arquivo = _estado["arquivo"]
    if not arquivo or not Path(arquivo).is_file():
        return False
    subprocess.Popen([str(arquivo), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
                     close_fds=True, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) |
                     getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    return True

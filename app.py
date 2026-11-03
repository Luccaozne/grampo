"""Grampo: abre a janela do programa (ou o navegador, com --navegador).

Por dentro é um site que só existe neste computador (127.0.0.1): os arquivos vão da tela para uma pasta temporária
local, são processados aqui mesmo e o resultado é gravado na Downloads. Nada sai do computador.

  .venv\\Scripts\\pythonw.exe app.py              janela própria
  .venv\\Scripts\\python.exe app.py --navegador   no navegador padrão
  .venv\\Scripts\\python.exe app.py --porta 5100 --sem-janela   só o servidor (para a prévia e para testes)
"""

import argparse
import atexit
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file

from grampo import NOME, VERSAO, conversao, ferramentas, operacoes, saida
from grampo.erros import ErroAmigavel

# No programa montado (PyInstaller) as telas ficam dentro do pacote
PASTA_PROGRAMA = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
PREFIXO_TEMPORARIO = "Grampo-envios-"


class Sessao:
    """Os arquivos que a tela mandou e as tarefas em andamento, enquanto o programa está aberto."""

    def __init__(self):
        self.pasta = Path(tempfile.mkdtemp(prefix=PREFIXO_TEMPORARIO))
        self.arquivos = {}  # id -> {"caminho", "nome"}
        self.tarefas = {}  # id -> {"progresso", "texto", "estado", "resultado", "erro"}
        self.gerados = set()  # o que o Grampo gravou: só isso pode ser aberto pela tela
        self.trava = threading.Lock()

    def limpar(self):
        shutil.rmtree(self.pasta, ignore_errors=True)


LIMITE_DE_ARQUIVOS = 500


def ordem_natural(nome):
    """"nota 2" antes de "nota 10", sem diferença de maiúsculas."""
    return [(0, int(p), "") if p.isdigit() else (1, 0, p.lower()) for p in re.split(r"(\d+)", nome) if p]


def expandir_caminhos(caminhos, aceita):
    """Troca cada pasta pelos arquivos aceitos dentro dela (com subpastas). Devolve (arquivos, quantos de outro
    tipo ficaram de fora, problemas para a tela)."""
    arquivos, ignorados, problemas = [], 0, []
    for bruto in caminhos:
        caminho = Path(str(bruto))
        if caminho.is_dir():
            dentro = sorted((p for p in caminho.rglob("*") if p.is_file()),
                            key=lambda p: [ordem_natural(parte) for parte in p.relative_to(caminho).parts])
            servem = [p for p in dentro if not aceita or p.suffix.lower() in aceita]
            ignorados += len(dentro) - len(servem)
            if not servem:
                problemas.append(f"A pasta “{caminho.name}” não tem nenhum arquivo que sirva nesta ferramenta.")
            arquivos += servem
        elif caminho.is_file():
            if aceita and caminho.suffix.lower() not in aceita:
                ignorados += 1
            else:
                arquivos.append(caminho)
        else:
            problemas.append(f"Não achei “{caminho.name}”. Ele pode ter sido movido ou apagado.")
    return arquivos, ignorados, problemas


def limpar_sobras():
    """Pastas temporárias de vezes anteriores que não foram apagadas (programa fechado à força)."""
    for pasta in Path(tempfile.gettempdir()).glob(PREFIXO_TEMPORARIO + "*"):
        try:
            if time.time() - pasta.stat().st_mtime > 24 * 3600:
                shutil.rmtree(pasta, ignore_errors=True)
        except OSError:
            pass


def criar_app(sessao=None):
    app = Flask(__name__, template_folder=str(PASTA_PROGRAMA / "templates"),
                static_folder=str(PASTA_PROGRAMA / "static"))
    app.config["MAX_CONTENT_LENGTH"] = None
    sessao = sessao or Sessao()
    app.sessao = sessao
    chave = secrets.token_urlsafe(24)

    @app.before_request
    def so_do_proprio_programa():
        # Outro site aberto no navegador não consegue mandar este cabeçalho: só a tela do Grampo usa a API
        if request.method == "POST" and request.headers.get("X-Grampo") != chave:
            abort(403)

    def erro(mensagem, codigo=400):
        return jsonify({"erro": mensagem}), codigo

    @app.get("/")
    def inicio():
        return render_template("index.html", nome=NOME, versao=VERSAO, chave=chave, grupos=ferramentas.GRUPOS,
                               ferramentas=ferramentas.FERRAMENTAS)

    @app.get("/api/ambiente")
    def ambiente():
        return jsonify({"pasta": str(saida.pasta_de_saida()), "padrao": str(saida.pasta_downloads()),
                        "programas": conversao.programas_disponiveis()})

    @app.post("/api/pasta")
    def trocar_pasta():
        caminho = (request.get_json(silent=True) or {}).get("pasta")
        if caminho and not Path(caminho).is_dir():
            return erro("Essa pasta não existe.")
        saida.escolher_pasta_de_saida(caminho)
        return jsonify({"pasta": str(saida.pasta_de_saida())})

    def registrar(nome, gravar):
        """Guarda um arquivo na pasta temporária (`gravar(destino)` faz a cópia) e devolve o que a tela mostra."""
        identificador = uuid.uuid4().hex
        nome = saida.limpar_nome(Path(nome or "arquivo").name)
        caminho = sessao.pasta / identificador / nome  # o temporário mantém o nome (o Office põe no PDF)
        caminho.parent.mkdir()
        gravar(caminho)
        info = {"id": identificador, "nome": nome, "tamanho": caminho.stat().st_size}
        if caminho.suffix.lower() == ".pdf":
            if operacoes.tem_senha(caminho):
                info["senha"] = True
            else:
                try:
                    info["paginas"] = operacoes.contar_paginas(caminho)
                except ErroAmigavel as e:
                    info["problema"] = str(e)
        with sessao.trava:
            sessao.arquivos[identificador] = {"caminho": caminho, "nome": nome}
        return info

    @app.post("/api/enviar")
    def enviar():
        """A tela manda o conteúdo dos arquivos (no navegador; na janela própria usa /api/adicionar)."""
        return jsonify([registrar(a.filename, a.save) for a in request.files.getlist("arquivos")])

    @app.post("/api/adicionar")
    def adicionar():
        """Arquivos e pastas pelo caminho no disco (diálogo do Windows ou arrastados do Explorer, na janela própria).
        Pasta vale por todos os arquivos aceitos dentro dela, em ordem de nome."""
        pedido = request.get_json(silent=True) or {}
        aceita = {e.lower() for e in pedido.get("aceita") or []}
        arquivos, ignorados, problemas = expandir_caminhos(pedido.get("caminhos") or [], aceita)
        recebidos = []
        for caminho in arquivos[:LIMITE_DE_ARQUIVOS]:
            try:
                recebidos.append(registrar(caminho.name, lambda destino, origem=caminho: shutil.copyfile(origem, destino)))
            except OSError:
                problemas.append(f"Não consegui ler “{caminho.name}”. Se ele está no OneDrive, abra a pasta dele, "
                                 "espere terminar de baixar e tente de novo.")
        if len(arquivos) > LIMITE_DE_ARQUIVOS:
            problemas.append(f"Peguei só os {LIMITE_DE_ARQUIVOS} primeiros arquivos.")
        return jsonify({"arquivos": recebidos, "ignorados": ignorados, "problemas": problemas})

    @app.get("/api/miniatura/<identificador>/<int:pagina>")
    def miniatura(identificador, pagina):
        arquivo = sessao.arquivos.get(identificador)
        if not arquivo:
            abort(404)
        try:
            png = operacoes.miniatura(arquivo["caminho"], pagina, int(request.args.get("largura", 180)))
        except ErroAmigavel:
            abort(404)
        resposta = app.response_class(png, mimetype="image/png")
        resposta.headers["Cache-Control"] = "max-age=3600"
        return resposta

    @app.post("/api/executar")
    def executar():
        pedido = request.get_json(silent=True) or {}
        try:
            arquivos = [(sessao.arquivos[i]["caminho"], sessao.arquivos[i]["nome"]) for i in pedido.get("arquivos", [])]
        except KeyError:
            return erro("Um dos arquivos não está mais disponível. Escolha de novo.")
        identificador = uuid.uuid4().hex
        tarefa = {"progresso": 0, "texto": "Começando", "estado": "rodando"}
        sessao.tarefas[identificador] = tarefa

        def avisar(fracao, texto=""):
            tarefa["progresso"] = max(0, min(100, round(fracao * 100)))
            if texto:
                tarefa["texto"] = texto

        def trabalhar():
            try:
                resultado = ferramentas.executar(pedido.get("ferramenta"), arquivos, pedido.get("opcoes"),
                                                 saida.pasta_de_saida(), avisar)
                with sessao.trava:
                    sessao.gerados.update(resultado["arquivos"])
                    sessao.gerados.update(str(Path(a).parent) for a in resultado["arquivos"])
                tarefa.update(estado="pronto", progresso=100, resultado=resultado)
            except ErroAmigavel as e:
                tarefa.update(estado="erro", erro=str(e))
            except PermissionError:
                tarefa.update(estado="erro", erro="O Windows não deixou gravar o resultado. Feche o arquivo se ele "
                                                  "estiver aberto em outro programa, ou escolha outra pasta.")
            except Exception as e:  # o inesperado: mensagem simples na tela, detalhe para a TI no console
                print(f"[Grampo] erro inesperado em {pedido.get('ferramenta')}: {e!r}", file=sys.stderr)
                tarefa.update(estado="erro", erro="Algo deu errado e não consegui terminar. Tente de novo; se "
                                                  "continuar, chame a TI e conte o que estava fazendo.")

        threading.Thread(target=trabalhar, daemon=True).start()
        return jsonify({"tarefa": identificador})

    @app.get("/api/tarefa/<identificador>")
    def tarefa(identificador):
        if identificador not in sessao.tarefas:
            abort(404)
        return jsonify(sessao.tarefas[identificador])

    @app.post("/api/abrir")
    def abrir():
        pedido = request.get_json(silent=True) or {}
        caminho = pedido.get("caminho", "")
        if caminho not in sessao.gerados and caminho != str(saida.pasta_de_saida()):
            return erro("Só dá para abrir o que o Grampo gravou.", 403)
        if not Path(caminho).exists():
            return erro("O arquivo não está mais lá. Ele pode ter sido movido ou apagado.", 404)
        if pedido.get("mostrar") and Path(caminho).is_file():
            subprocess.Popen(["explorer", "/select,", caminho])
        else:
            os.startfile(caminho)
        return jsonify({"ok": True})

    @app.get("/api/baixar")
    def baixar():
        """Só para a prévia no navegador: o resultado já está gravado na pasta, isto entrega uma cópia."""
        caminho = request.args.get("caminho", "")
        if caminho not in sessao.gerados or not Path(caminho).is_file():
            abort(404)
        return send_file(caminho, as_attachment=True)

    return app


class ApiDaJanela:
    """O que a tela chama direto do Python quando está na janela própria (pywebview)."""

    def __init__(self):
        self._janela = None
        self._ultima_pasta = ""  # o diálogo abre de novo onde a pessoa estava

    def escolher_pasta(self):
        """A pasta onde o resultado é gravado."""
        import webview

        escolha = self._janela.create_file_dialog(webview.FileDialog.FOLDER, directory=str(saida.pasta_de_saida()))
        return escolha[0] if escolha else None

    def escolher_arquivos(self, aceita, varios):
        """Diálogo do Windows para escolher um ou vários arquivos (Ctrl ou Shift, ou Ctrl+A para todos)."""
        import webview

        tipos = ";".join("*" + e for e in aceita)
        escolha = self._janela.create_file_dialog(
            webview.FileDialog.OPEN, directory=self._ultima_pasta, allow_multiple=bool(varios),
            file_types=(f"Arquivos que servem aqui ({tipos})", "Todos os arquivos (*.*)"))
        if not escolha:
            return []
        self._ultima_pasta = str(Path(escolha[0]).parent)
        return list(escolha)

    def escolher_pasta_de_arquivos(self):
        """Uma pasta inteira: a tela pega todos os arquivos que servem dentro dela."""
        import webview

        escolha = self._janela.create_file_dialog(webview.FileDialog.FOLDER, directory=self._ultima_pasta)
        if not escolha:
            return []
        self._ultima_pasta = str(Path(escolha[0]))
        return [escolha[0]]

    def _ao_soltar(self, evento):
        """Arquivos ou pastas soltos na janela: o pywebview dá o caminho completo de cada um, e a tela os adiciona."""
        caminhos = [f.get("pywebviewFullPath") for f in (evento.get("dataTransfer") or {}).get("files", [])]
        caminhos = [c for c in caminhos if c]
        if caminhos:
            self._janela.evaluate_js(f"window.receberCaminhos && window.receberCaminhos({json.dumps(caminhos)})")

    def _ligar_arrastar(self):
        from webview.dom import DOMEventHandler

        self._janela.dom.document.events.drop += DOMEventHandler(self._ao_soltar, prevent_default=True)


def porta_livre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    parametros = argparse.ArgumentParser(description="Grampo: ferramentas de PDF sem internet")
    parametros.add_argument("--navegador", action="store_true", help="abrir no navegador em vez da janela")
    parametros.add_argument("--sem-janela", action="store_true", help="só o servidor, sem abrir nada")
    parametros.add_argument("--porta", type=int, default=0)
    args = parametros.parse_args()

    limpar_sobras()
    conversao.devolver_impressora()
    sessao = Sessao()
    atexit.register(sessao.limpar)
    app = criar_app(sessao)
    porta = args.porta or porta_livre()
    endereco = f"http://127.0.0.1:{porta}/"

    if args.sem_janela or args.navegador:
        if args.navegador:
            threading.Timer(1.0, lambda: __import__("webbrowser").open(endereco)).start()
        app.run(host="127.0.0.1", port=porta, threaded=True)
        return

    import webview

    threading.Thread(target=lambda: app.run(host="127.0.0.1", port=porta, threaded=True), daemon=True).start()
    api = ApiDaJanela()
    api._janela = webview.create_window(NOME, endereco, js_api=api, width=1180, height=820, min_size=(760, 560),
                                       background_color="#F3F6FA")
    api._janela.events.loaded += api._ligar_arrastar
    webview.start()


if __name__ == "__main__":
    main()

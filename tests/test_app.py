"""O servidor local que a tela usa: envio, execução com andamento, segurança da API."""

import re
import time

import pytest

import app as programa
from grampo import saida as modulo_saida


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    monkeypatch.setattr(modulo_saida, "ARQUIVO_CONFIGURACAO", tmp_path / "config.json")
    monkeypatch.setattr(modulo_saida, "pasta_downloads", lambda: tmp_path / "Downloads")
    (tmp_path / "Downloads").mkdir()
    sessao = programa.Sessao()
    aplicacao = programa.criar_app(sessao)
    with aplicacao.test_client() as c:
        pagina = c.get("/").get_data(as_text=True)
        c.chave = re.search(r'chave: "([^"]+)"', pagina).group(1)
        c.pasta = tmp_path / "Downloads"
        yield c
    sessao.limpar()


def enviar(cliente, *caminhos):
    arquivos = [(open(c, "rb"), c.name) for c in caminhos]
    try:
        resposta = cliente.post("/api/enviar", data={"arquivos": arquivos}, headers={"X-Grampo": cliente.chave},
                                content_type="multipart/form-data")
    finally:
        for f, _ in arquivos:
            f.close()
    assert resposta.status_code == 200
    return resposta.get_json()


def esperar(cliente, tarefa):
    for _ in range(200):
        situacao = cliente.get(f"/api/tarefa/{tarefa}").get_json()
        if situacao["estado"] != "rodando":
            return situacao
        time.sleep(0.05)
    raise AssertionError("a tarefa não terminou")


def test_tela_inicial_tem_todas_as_ferramentas(cliente):
    pagina = cliente.get("/").get_data(as_text=True)
    for f in programa.ferramentas.FERRAMENTAS:
        assert f'"id": "{f["id"]}"' in pagina


def test_sem_a_chave_nao_aceita(cliente, exemplos):
    resposta = cliente.post("/api/executar", json={"ferramenta": "juntar"})
    assert resposta.status_code == 403


def test_fluxo_completo(cliente, exemplos):
    recebidos = enviar(cliente, exemplos / "anexo.pdf", exemplos / "relatorio.pdf", exemplos / "com-senha.pdf")
    assert recebidos[0]["paginas"] == 2 and recebidos[1]["paginas"] == 6
    assert recebidos[2]["senha"] is True
    resposta = cliente.post("/api/executar", headers={"X-Grampo": cliente.chave},
                            json={"ferramenta": "juntar", "arquivos": [recebidos[1]["id"], recebidos[0]["id"]]})
    situacao = esperar(cliente, resposta.get_json()["tarefa"])
    assert situacao["estado"] == "pronto", situacao
    assert (cliente.pasta / "relatorio (juntado).pdf").is_file()
    miniatura = cliente.get(f"/api/miniatura/{recebidos[0]['id']}/1")
    assert miniatura.status_code == 200 and miniatura.mimetype == "image/png"


def test_erro_vira_mensagem_simples(cliente, exemplos):
    recebidos = enviar(cliente, exemplos / "anexo.pdf")
    resposta = cliente.post("/api/executar", headers={"X-Grampo": cliente.chave},
                            json={"ferramenta": "excluir", "arquivos": [recebidos[0]["id"]],
                                  "opcoes": {"paginas": "1-2"}})
    situacao = esperar(cliente, resposta.get_json()["tarefa"])
    assert situacao["estado"] == "erro"
    assert "nenhuma página" in situacao["erro"]


def test_adicionar_pasta_pega_so_os_que_servem_em_ordem_de_nome(cliente, exemplos, tmp_path):
    pasta = tmp_path / "Notas de março"
    (pasta / "sub").mkdir(parents=True)
    for nome in ("nota 10.pdf", "nota 2.pdf", "Nota 1.pdf"):
        (pasta / nome).write_bytes((exemplos / "anexo.pdf").read_bytes())
    (pasta / "sub" / "nota 3.pdf").write_bytes((exemplos / "anexo.pdf").read_bytes())
    (pasta / "leia-me.txt").write_text("x")
    resposta = cliente.post("/api/adicionar", headers={"X-Grampo": cliente.chave},
                            json={"caminhos": [str(pasta)], "aceita": [".pdf"]}).get_json()
    assert [a["nome"] for a in resposta["arquivos"]] == ["Nota 1.pdf", "nota 2.pdf", "nota 10.pdf", "nota 3.pdf"]
    assert all(a["paginas"] == 2 for a in resposta["arquivos"])
    assert resposta["ignorados"] == 1 and resposta["problemas"] == []


def test_adicionar_arquivos_e_o_que_nao_existe(cliente, exemplos, tmp_path):
    vazia = tmp_path / "vazia"
    vazia.mkdir()
    resposta = cliente.post("/api/adicionar", headers={"X-Grampo": cliente.chave}, json={
        "caminhos": [str(exemplos / "relatorio.pdf"), str(exemplos / "foto1.jpg"), str(tmp_path / "sumiu.pdf"),
                     str(vazia)], "aceita": [".pdf"]}).get_json()
    assert [a["nome"] for a in resposta["arquivos"]] == ["relatorio.pdf"]
    assert resposta["ignorados"] == 1
    assert any("sumiu.pdf" in p for p in resposta["problemas"])
    assert any("não tem nenhum arquivo" in p for p in resposta["problemas"])
    # a cópia fica na pasta temporária: o original não é usado nem mexido
    identificador = resposta["arquivos"][0]["id"]
    assert cliente.application.sessao.arquivos[identificador]["caminho"] != exemplos / "relatorio.pdf"


def test_adicionar_exige_a_chave(cliente, exemplos):
    assert cliente.post("/api/adicionar", json={"caminhos": [str(exemplos)]}).status_code == 403


def test_so_abre_o_que_o_grampo_gravou(cliente):
    resposta = cliente.post("/api/abrir", headers={"X-Grampo": cliente.chave},
                            json={"caminho": r"C:\Windows\System32\calc.exe"})
    assert resposta.status_code == 403


def test_trocar_pasta(cliente, tmp_path):
    outra = tmp_path / "Outra"
    outra.mkdir()
    resposta = cliente.post("/api/pasta", headers={"X-Grampo": cliente.chave}, json={"pasta": str(outra)})
    assert resposta.get_json()["pasta"] == str(outra)
    assert cliente.get("/api/ambiente").get_json()["pasta"] == str(outra)
    cliente.post("/api/pasta", headers={"X-Grampo": cliente.chave}, json={"pasta": None})
    assert cliente.get("/api/ambiente").get_json()["pasta"] == str(cliente.pasta)

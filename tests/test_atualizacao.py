"""Atualização automática: só instala o que o GitHub publicou e o SHA-256 confirma."""

import hashlib
import io

import pytest

from grampo import atualizacao

BASE = f"https://github.com/{atualizacao.REPOSITORIO}/releases/download"


def release(tag="v9.0.0", sha=None, nome=None, **extra):
    nome = nome or f"Grampo-{tag.lstrip('v')}.exe"
    ativo = {"name": nome, "size": 3, "browser_download_url": f"{BASE}/{tag}/{nome}"}
    if sha:
        ativo["digest"] = f"sha256:{sha}"
    return {"tag_name": tag, "assets": [ativo], **extra}


SHA = hashlib.sha256(b"abc").hexdigest()


def test_versao_em_numeros():
    assert atualizacao.versao_em_numeros("v1.10.0") > atualizacao.versao_em_numeros("1.9.0")  # número, não texto
    assert atualizacao.versao_em_numeros("lixo") is None


def test_escolhe_o_instalador_da_versao_nova():
    assert atualizacao.escolher_instalador(release(sha=SHA))[0] == (9, 0, 0)


@pytest.mark.parametrize("r", [
    release(tag="v0.0.1", sha=SHA, nome="Grampo-0.0.1.exe"),   # mais velha
    release(sha=None),                                          # sem SHA-256 publicado
    release(sha=SHA, nome="Outro-9.0.0.exe"),                   # nome fora do padrão
    release(sha=SHA, prerelease=True),
    release(sha=SHA, draft=True),
])
def test_nao_instala(r):
    assert atualizacao.escolher_instalador(r) is None


def test_recusa_endereco_de_fora():
    r = release(sha=SHA)
    r["assets"][0]["browser_download_url"] = "https://exemplo.com/Grampo-9.0.0.exe"
    assert atualizacao.escolher_instalador(r) is None


def preparar(monkeypatch, tmp_path, conteudo):
    monkeypatch.setattr(atualizacao, "PASTA", tmp_path / "baixados")
    monkeypatch.setattr(atualizacao, "_pedir", lambda *a, **k: io.BytesIO(conteudo))
    monkeypatch.setitem(atualizacao._estado, "arquivo", None)


def test_baixa_e_confere(monkeypatch, tmp_path):
    preparar(monkeypatch, tmp_path, b"abc")
    arquivo = atualizacao.baixar((9, 0, 0), "https://x", 3, SHA)
    assert arquivo.read_bytes() == b"abc"


def test_arquivo_adulterado_e_descartado(monkeypatch, tmp_path):
    preparar(monkeypatch, tmp_path, b"xyz")
    assert atualizacao.baixar((9, 0, 0), "https://x", 3, SHA) is None
    assert not list((tmp_path / "baixados").glob("Grampo-*"))


def test_verificar_sem_internet_nao_levanta(monkeypatch):
    def cai(*a, **k):
        raise OSError("sem rede")
    monkeypatch.setattr(atualizacao, "_pedir", cai)
    atualizacao.verificar()


def test_so_instala_se_baixou(monkeypatch):
    monkeypatch.setitem(atualizacao._estado, "arquivo", None)
    assert atualizacao.instalar_ao_fechar() is False


def test_instala_em_silencio(monkeypatch, tmp_path):
    exe = tmp_path / "Grampo-9.0.0.exe"
    exe.write_bytes(b"abc")
    chamadas = []
    monkeypatch.setitem(atualizacao._estado, "arquivo", exe)
    monkeypatch.setattr(atualizacao.subprocess, "Popen", lambda *a, **k: chamadas.append(a[0]))
    assert atualizacao.instalar_ao_fechar() is True
    assert chamadas[0][0] == str(exe) and "/VERYSILENT" in chamadas[0]


def test_endpoint_de_situacao(monkeypatch):
    import app as programa
    monkeypatch.setitem(atualizacao._estado, "versao", None)
    monkeypatch.setitem(atualizacao._estado, "arquivo", None)
    with programa.criar_app(programa.Sessao()).test_client() as c:
        assert c.get("/api/atualizacao").get_json() == {"versao": None, "pronta": False}

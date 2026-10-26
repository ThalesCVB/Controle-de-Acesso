"""Tratamento de nomes: padronização e busca tolerante a erro de digitação (português do Brasil)."""
import re
import unicodedata
from difflib import SequenceMatcher

PARTICULAS = {"da", "de", "do", "das", "dos", "e", "di", "du", "van", "von"}
LIMIAR_TOKEN = 0.84      # similaridade mínima entre duas palavras (chave fonética)
LIMIAR_DUPLICADO = 0.86  # média mínima pra considerar "parecido" no cadastro


def sem_acento(s):
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn")


def padronizar(nome):
    """'  maria  DA silva ' -> 'Maria da Silva'. Mantém partículas em minúscula."""
    palavras = re.sub(r"\s+", " ", (nome or "").strip()).split(" ")
    saida = []
    for i, p in enumerate(palavras):
        if not p:
            continue
        baixa = p.lower()
        saida.append(baixa if (i > 0 and baixa in PARTICULAS) else "-".join(x.capitalize() for x in baixa.split("-")))
    return " ".join(saida)


def normalizar(s):
    """Minúsculas, sem acento, só letras/números/espaço."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", sem_acento(s).lower())).strip()


def fonetica(palavra):
    """Chave aproximada pelo som: Luiz~Luis, Jhonatan~Jonatan, Wilson~Vilson, Cássia~Cassia~Kássia."""
    p = (palavra or "").lower().replace("ç", "ss")
    p = re.sub(r"[^a-z]", "", sem_acento(p))
    for a, b in (("ph", "f"), ("th", "t"), ("ch", "x"), ("sh", "x"), ("lh", "li"), ("nh", "ni"),
                 ("qu", "k"), ("ck", "k"), ("gu", "g"), ("sc", "s"), ("xc", "s")):
        p = p.replace(a, b)
    p = re.sub(r"c(?=[ei])", "s", p)
    p = re.sub(r"g(?=[ei])", "j", p)
    p = p.replace("c", "k").replace("z", "s").replace("w", "v").replace("y", "i").replace("h", "")
    p = p.replace("m", "n")
    p = re.sub(r"(.)\1+", r"\1", p)
    return re.sub(r"o$", "u", re.sub(r"e$", "i", p))


def _tokens(s):
    return [t for t in normalizar(s).split(" ") if t]


def _sim(tq, tn, prefixo):
    """Similaridade 0..1 entre duas palavras já normalizadas."""
    if tq == tn:
        return 1.0
    if prefixo and tn.startswith(tq):
        return 0.95
    fq, fn = fonetica(tq), fonetica(tn)
    if not fq or not fn:
        return 0.0
    if fq == fn:
        return 0.93
    if prefixo and fn.startswith(fq) and len(fq) >= 2:
        return 0.9
    r = SequenceMatcher(None, fq, fn).ratio()
    return r if r >= LIMIAR_TOKEN and min(len(fq), len(fn)) >= 4 else 0.0


def pontuar(consulta, nome, prefixo=True):
    """0 = não combina. Todas as palavras da consulta precisam achar par no nome."""
    tq, tn = _tokens(consulta), _tokens(nome)
    if not tq or not tn:
        return 0.0
    total = 0.0
    for q in tq:
        melhor = max((_sim(q, n, prefixo) for n in tn), default=0.0)
        if melhor == 0.0:
            return 0.0
        total += melhor
    return total / len(tq)


def buscar(consulta, itens, chave=lambda x: x, limite=None):
    """Itens que combinam com a consulta, melhores primeiro (empate: ordem original)."""
    pontos = [(pontuar(consulta, chave(i)), idx, i) for idx, i in enumerate(itens)]
    achados = sorted((t for t in pontos if t[0] > 0), key=lambda t: (-t[0], t[1]))
    res = [i for _, _, i in achados]
    return res[:limite] if limite else res


def parecidos(nome, itens, chave=lambda x: x, limite=5):
    """Nomes já cadastrados que podem ser a mesma pessoa (cadastro completo, não prefixo)."""
    if len(_tokens(nome)) < 2:
        return []
    achados = []
    for i in itens:
        p = max(pontuar(nome, chave(i), prefixo=False), pontuar(chave(i), nome, prefixo=False))
        if p >= LIMIAR_DUPLICADO:
            achados.append((p, i))
    return [i for _, i in sorted(achados, key=lambda t: -t[0])][:limite]

import os
import io
import re
import unicodedata
import pandas as pd
import requests
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import StreamingResponse, JSONResponse

app = FastAPI()
SERPAPI_KEY = os.getenv("SERPAPI_KEY")

# ---------------------------------------------------------------------------
# Utilitários de texto
# ---------------------------------------------------------------------------

def limpar_texto(texto):
    if not texto:
        return ""
    texto = unicodedata.normalize("NFKD", str(texto))
    texto = texto.encode("ascii", "ignore").decode("utf-8")
    return texto.strip()


def normalizar_coluna(nome):
    nome = unicodedata.normalize("NFKD", str(nome))
    nome = "".join(c for c in nome if not unicodedata.combining(c))
    return nome.lower().strip().replace(" ", "_")


def mapear_colunas(df):
    MAPA_EMPRESA = {"organizacao", "razao_social", "razao", "empresa", "nome_empresa", "nome"}
    MAPA_CNPJ    = {"cnpj", "documento", "doc", "nr_cnpj", "num_cnpj"}
    df.columns   = [normalizar_coluna(c) for c in df.columns]
    col_empresa  = next((c for c in df.columns if c in MAPA_EMPRESA), None)
    col_cnpj     = next((c for c in df.columns if c in MAPA_CNPJ), None)
    if not col_empresa or not col_cnpj:
        return None, "CSV precisa conter colunas compativeis com empresa e cnpj"
    return df.rename(columns={col_empresa: "empresa", col_cnpj: "cnpj"}), None


def nome_simples(nome_empresa):
    if not nome_empresa:
        return ""
    STOPWORDS = {
        "sa", "s/a", "ltda", "ltda.", "s.a", "s.a.", "me", "eireli", "epp",
        "do", "de", "da", "dos", "das", "e", "em",
        "brasil", "brasileira", "grupo", "cia", "companhia",
        "industria", "industrias", "instituto", "hospital", "clinica",
        "centro", "fundacao", "associacao", "cooperativa",
        "servicos", "comercio", "solucoes", "tecnologia",
        "nacional", "internacional", "produtos", "sistemas",
        "eletronicos", "seguranca", "industriais", "quimicos",
    }
    partes = limpar_texto(nome_empresa).lower().split()
    tokens = [p for p in partes if p not in STOPWORDS and len(p) > 3]
    return " ".join(tokens) if tokens else " ".join(partes)


def _tokens_empresa(nome):
    return [t for t in nome_simples(nome).lower().split() if len(t) >= 4]


def nome_para_busca(nome_fantasia, empresa):
    if nome_fantasia and len(nome_fantasia.strip()) > 3:
        return nome_fantasia.strip()
    return nome_simples(empresa) or empresa


def _extrair_dominio_url(url):
    m = re.search(r"https?://(?:www\.)?([^/?#]+)", url or "")
    return m.group(1).lower() if m else ""


# ---------------------------------------------------------------------------
# Blacklists
# ---------------------------------------------------------------------------

DOMINIOS_BLOQUEADOS = {
    "youtube.com", "youtu.be", "facebook.com", "fb.com",
    "instagram.com", "twitter.com", "x.com", "tiktok.com",
    "linkedin.com", "whatsapp.com", "telegram.org",
    "scribd.com", "pt.scribd.com", "slideshare.net", "issuu.com",
    "google.com", "google.com.br", "bing.com", "yahoo.com",
    "uol.com.br", "globo.com", "r7.com",
    "wikipedia.org", "wikimedia.org",
    "brasilapi.com.br", "receitaws.com.br",
    "receita.fazenda.gov.br", "cnpj.biz", "cnpj.services",
    "cnpja.com", "cnpjdados.com.br", "cnpjaberto.com.br",
    "cnpjinfo.com.br", "cnpjagora.com.br", "cnpjbrasil.com.br",
    "consultasocio.com.br", "situacaocadastral.com.br",
    "sintegra.gov.br", "transparencia.cc",
    "informecadastral.com.br", "cadastroempresa.com.br",
    "diariocidade.com.br", "onlineempresas.com.br",
    "numerodozap.com.br", "casadosdados.com.br",
    "jusbrasil.com.br", "escavador.com", "reclameaqui.com.br",
    "estadao.com.br", "folha.uol.com.br", "exame.com",
    "valor.com.br", "infomoney.com.br",
    "rocketreach.co", "contactout.com", "finalscout.com",
    "zoominfo.com", "apollo.io", "lusha.com", "snov.io",
    "leadiq.com", "hunter.io", "econodata.com.br",
    "glassdoor.com", "indeed.com", "catho.com.br",
    "infojobs.com.br", "vagas.com",
    "obahortifruti.com.br", "kaeferbrasil.com.br",
    "cnpj.linkana.com", "linkana.com", "psvar.com.br",
}

DOMINIOS_GENERICOS = {
    "gmail.com", "hotmail.com", "yahoo.com", "yahoo.com.br",
    "outlook.com", "live.com", "uol.com.br", "bol.com.br",
    "terra.com.br", "ig.com.br", "globo.com",
}


def dominio_bloqueado(dominio):
    if not dominio:
        return True
    d = dominio.lower().lstrip("www.")
    return d in DOMINIOS_BLOQUEADOS or any(d.endswith("." + b) for b in DOMINIOS_BLOQUEADOS)


def dominio_generico(dominio):
    if not dominio:
        return True
    return dominio.lower().lstrip("www.") in DOMINIOS_GENERICOS


# ---------------------------------------------------------------------------
# SerpAPI
# ---------------------------------------------------------------------------

def serpapi_search(query, num=5):
    if not SERPAPI_KEY:
        return []
    try:
        r = requests.get("https://serpapi.com/search", params={
            "engine":  "google",
            "q":       query,
            "api_key": SERPAPI_KEY,
            "num":     num,
            "hl":      "pt",
            "gl":      "br",
        }, timeout=15)
        return r.json().get("organic_results", [])
    except Exception:
        return []


# ---------------------------------------------------------------------------
# BrasilAPI
# ---------------------------------------------------------------------------

PRIORIDADE_QSA = {
    "presidente":          0,
    "diretor presidente":  1,
    "diretor":             2,
    "administrador":       3,
    "socio-administrador": 4,
    "socio administrador": 4,
    "gerente":             5,
    "socio":               6,
}


def _prioridade_qualificacao(qual):
    q = limpar_texto(qual).lower()
    for chave, prio in PRIORIDADE_QSA.items():
        if chave in q:
            return prio
    return 99


def extrair_pessoas_qsa(qsa_list):
    TERMOS_PJ = {
        "ltda", "s/a", "s.a", "eireli", "holding", "participacoes",
        "investimentos", "empreendimentos", "international", "engenharia",
        "tecnologia", "construtora", "servicos", "comercio", "industria",
        "group", "grupo", "gmbh", "corp", "inc",
    }
    pessoas = []
    for item in (qsa_list or []):
        nome = limpar_texto(item.get("nome_socio") or "").strip()
        qual = limpar_texto(item.get("qualificacao_socio") or "").strip()
        doc  = re.sub(r"\D", "", item.get("cnpj_cpf_do_socio") or "")
        if len(doc) == 14:
            continue
        if any(t in nome.lower() for t in TERMOS_PJ):
            continue
        if not nome or len(nome.split()) < 2:
            continue
        pessoas.append({
            "nome":        nome.title(),
            "qualificacao": qual,
            "prioridade":  _prioridade_qualificacao(qual),
        })
    pessoas.sort(key=lambda x: x["prioridade"])
    return pessoas


def buscar_dados_cnpj(cnpj):
    cnpj_limpo = re.sub(r"\D", "", str(cnpj)).zfill(14)
    try:
        r = requests.get(
            f"https://brasilapi.com.br/api/cnpj/v1/{cnpj_limpo}",
            timeout=10
        )
        if r.status_code != 200:
            return "", "", "", "", []
        d = r.json()
        t = re.sub(r"\D", "", d.get("ddd_telefone_1", "") or "")
        telefone = f"({t[:2]}) {t[2:]}" if len(t) >= 10 else t
        nome_fantasia = limpar_texto(d.get("nome_fantasia") or "")
        email_empresa = limpar_texto(d.get("email") or "").lower()
        pessoas_qsa   = extrair_pessoas_qsa(d.get("qsa", []))
        return telefone.strip(), nome_fantasia, email_empresa, d.get("uf", ""), pessoas_qsa
    except Exception:
        return "", "", "", "", []


# ---------------------------------------------------------------------------
# Site da empresa
# ---------------------------------------------------------------------------

def resolver_dominio(nome_busca, cnpj, email_empresa):
    cnpj_limpo = re.sub(r"\D", "", str(cnpj)).zfill(14)
    tokens = _tokens_empresa(nome_busca)

    # 1. Email BrasilAPI
    if email_empresa and "@" in email_empresa:
        d = email_empresa.split("@")[-1].strip()
        if not dominio_generico(d) and not dominio_bloqueado(d):
            return d

    # 2. SerpAPI por nome
    for query in [
        f'"{nome_busca}" site oficial',
        f'"{nome_busca}" contato',
        f'{nome_busca} site oficial',
    ]:
        for r in serpapi_search(query, num=5):
            link = r.get("link", "")
            d = _extrair_dominio_url(link)
            if not d or dominio_bloqueado(d):
                continue
            if not tokens or any(t in d for t in tokens):
                return d

    # 3. SerpAPI por CNPJ
    for r in serpapi_search(f"{cnpj_limpo} site oficial", num=5):
        link = r.get("link", "")
        d = _extrair_dominio_url(link)
        if not d or dominio_bloqueado(d):
            continue
        if not tokens or any(t in d for t in tokens):
            return d

    return ""


# ---------------------------------------------------------------------------
# LinkedIn empresa
# ---------------------------------------------------------------------------

def buscar_linkedin_empresa(nome_busca, cnpj):
    cnpj_limpo = re.sub(r"\D", "", str(cnpj)).zfill(14)
    tokens = _tokens_empresa(nome_busca)

    for query in [
        f'"{nome_busca}" site:linkedin.com/company',
        f'{nome_busca} linkedin empresa',
    ]:
        for r in serpapi_search(query, num=5):
            link = r.get("link", "")
            if "linkedin.com/company" not in link:
                continue
            if tokens:
                if any(t in link.lower() for t in tokens):
                    return link
            else:
                # Siglas (GPS, etc): valida por contexto
                contexto = (r.get("title", "") + " " + r.get("snippet", "")).lower()
                palavras = [w for w in limpar_texto(nome_busca).lower().split() if len(w) > 2]
                if any(p in contexto for p in palavras):
                    return link

    # Fallback CNPJ
    for r in serpapi_search(f"{cnpj_limpo} site:linkedin.com/company", num=3):
        link = r.get("link", "")
        if "linkedin.com/company" in link:
            return link

    return ""


# ---------------------------------------------------------------------------
# LinkedIn de pessoas (QSA)
# ---------------------------------------------------------------------------

def buscar_linkedin_pessoa(nome_completo, nome_busca, dominio_oficial=""):
    partes     = limpar_texto(nome_completo).lower().split()
    primeiro   = partes[0] if partes else ""
    ultimo     = partes[-1] if len(partes) > 1 else ""
    nome_curto = f"{partes[0].title()} {partes[-1].title()}" if len(partes) > 1 else nome_completo
    nome_busca_simples = nome_simples(nome_busca)

    # Hint de domínio para subsidiárias (diretoria.rip@kaefer.com → "kaefer")
    hint_dominio = ""
    if dominio_oficial:
        raiz = dominio_oficial.lower().replace("www.", "").split(".")[0]
        if len(raiz) > 3 and raiz not in nome_busca_simples.lower():
            hint_dominio = raiz

    queries = [
        f'"{nome_completo}" "{nome_busca}" site:linkedin.com/in',
        f'"{nome_curto}" "{nome_busca}" site:linkedin.com/in',
        f'"{nome_curto}" "{nome_busca_simples}" linkedin',
    ]
    if hint_dominio:
        queries.append(f'"{nome_curto}" "{hint_dominio}" site:linkedin.com/in')

    for query in queries:
        for r in serpapi_search(query, num=5):
            link = r.get("link", "")
            if "linkedin.com/in" not in link:
                continue
            contexto = (r.get("title", "") + " " + r.get("snippet", "")).lower()
            if primeiro in contexto or ultimo in contexto:
                return link

    return ""


# ---------------------------------------------------------------------------
# Endpoint principal
# ---------------------------------------------------------------------------

@app.post("/processar")
async def processar_csv(file: UploadFile = File(...)):
    try:
        df_input = pd.read_csv(file.file, sep=None, engine="python")
    except Exception as e:
        return JSONResponse(status_code=400, content={"erro": f"Erro ao ler CSV: {str(e)}"})

    df_input, erro = mapear_colunas(df_input)
    if erro:
        return JSONResponse(status_code=400, content={"erro": erro})

    resultados = []

    for _, row in df_input.iterrows():
        empresa = str(row.get("empresa", "")).strip()
        cnpj    = str(row.get("cnpj", "")).strip()

        # CAMADA 0: BrasilAPI
        telefone, nome_fantasia, email_empresa, uf, pessoas_qsa = buscar_dados_cnpj(cnpj)
        nome_busca = nome_para_busca(nome_fantasia, empresa)

        # CAMADA 1: Site
        site_empresa = resolver_dominio(nome_busca, cnpj, email_empresa)

        # CAMADA 2: LinkedIn empresa
        linkedin_empresa = buscar_linkedin_empresa(nome_busca, cnpj)

        # CAMADA 3: LinkedIn de até 3 pessoas do QSA
        pessoas_resultado = []
        for pessoa in pessoas_qsa[:3]:
            linkedin_p = buscar_linkedin_pessoa(
                pessoa["nome"], nome_busca, site_empresa
            )
            pessoas_resultado.append({
                "nome":     pessoa["nome"],
                "cargo":    pessoa["qualificacao"],
                "linkedin": linkedin_p,
            })

        # Garante sempre 3 slots
        while len(pessoas_resultado) < 3:
            pessoas_resultado.append({"nome": "", "cargo": "", "linkedin": ""})

        resultado = {
            "empresa":          empresa,
            "nome_fantasia":    nome_fantasia,
            "cnpj":             cnpj,
            "uf":               uf,
            "telefone":         telefone,
            "site_empresa":     site_empresa,
            "linkedin_empresa": linkedin_empresa,
        }
        for i, p in enumerate(pessoas_resultado[:3], start=1):
            resultado[f"pessoa{i}_nome"]     = p["nome"]
            resultado[f"pessoa{i}_cargo"]    = p["cargo"]
            resultado[f"pessoa{i}_linkedin"] = p["linkedin"]

        resultados.append(resultado)

    df_final = pd.DataFrame(resultados)
    output   = io.StringIO()
    df_final.to_csv(output, index=False)
    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=resultado_pipeline.csv"}
    )

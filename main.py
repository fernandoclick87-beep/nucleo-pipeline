import os
import io
import re
import unicodedata
import pandas as pd
import requests
import dns.resolver
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import StreamingResponse, JSONResponse

app = FastAPI()
SERPAPI_KEY    = os.getenv("SERPAPI_KEY")
ZEROBOUNCE_KEY = os.getenv("ZEROBOUNCE_KEY")
HUNTER_KEY     = os.getenv("HUNTER_KEY")


# ---------------------------------------------------------------------------
# Utilitarios
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


def serpapi_search(query):
    if not SERPAPI_KEY:
        return []
    try:
        r = requests.get("https://serpapi.com/search", params={
            "engine": "google",
            "q": query,
            "api_key": SERPAPI_KEY,
            "num": 5
        }, timeout=15)
        return r.json().get("organic_results", [])
    except Exception:
        return []


def validar_mx(dominio):
    if not dominio:
        return False
    try:
        dns.resolver.resolve(dominio, "MX")
        return True
    except Exception:
        return False


def validar_http(dominio):
    for scheme in ("https", "http"):
        try:
            r = requests.get(
                f"{scheme}://{dominio}",
                timeout=5,
                allow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0"}
            )
            if r.status_code < 400:
                return True
        except Exception:
            pass
    return False


def eh_nome_pessoa(nome):
    if not nome:
        return False
    nao_pessoa = [
        "grupo", "assessoria", "consultoria", "agencia", "gestao",
        "solucoes", "servicos", "comercial", "marketing", "comunicacao",
        "holding", "organicos", "associacao", "cooperativa", "industria",
        "empresa", "companhia", "ltda", "s/a", "manteiga", "laticinios",
        "aviacao", "quimica", "pier", "sistemas", "tecnologia"
    ]
    if any(p in nome.lower() for p in nao_pessoa):
        return False
    partes = nome.split()
    if len(partes) < 2:
        return False
    if len(partes[0]) < 3 or len(partes[-1]) < 3:
        return False
    return True


# ---------------------------------------------------------------------------
# CNPJ e nome fantasia
# ---------------------------------------------------------------------------

DOMINIOS_CONFIAVEIS_CNPJ = [
    "casadosdados.com.br",
    "econodata.com.br",
    "cnpj.biz",
    "consultascnpj.com",
    "situacaocadastral.info",
    "numerodozap.com",
]


def extrair_nome_fantasia_do_cnpj(cnpj, razao_social):
    cnpj_limpo  = re.sub(r"\D", "", str(cnpj)).zfill(14)
    resultados  = serpapi_search(cnpj_limpo)
    razao_lower = limpar_texto(razao_social).lower()

    for r in resultados:
        link    = r.get("link", "")
        snippet = r.get("snippet", "")

        if not any(d in link.lower() for d in DOMINIOS_CONFIAVEIS_CNPJ):
            continue

        match = re.search(r"nome fantasia[:\s]+([A-Z][A-Z\s]{3,40})", snippet, re.IGNORECASE)
        if match:
            candidato = match.group(1).strip().title()
            if limpar_texto(candidato).lower() not in razao_lower:
                return candidato

        titulo = r.get("title", "")
        partes = re.split(r" - | em ", titulo)
        if partes:
            candidato       = partes[0].strip()
            candidato_lower = limpar_texto(candidato).lower()
            if (candidato_lower not in razao_lower
                    and razao_lower[:10] not in candidato_lower
                    and len(candidato) > 4
                    and not any(x in candidato_lower for x in [
                        "cnpj", "consulta", "empresa", "dados", "serasa",
                        "receita", "situacao", "cadastral", "informacoes"
                    ])):
                return candidato.title()

    return ""


def buscar_dados_cnpj(cnpj, razao_social=""):
    cnpj_limpo = re.sub(r"\D", "", str(cnpj)).zfill(14)
    try:
        r = requests.get(
            f"https://brasilapi.com.br/api/cnpj/v1/{cnpj_limpo}",
            timeout=10
        )
        if r.status_code == 200:
            d         = r.json()
            telefone  = d.get("ddd_telefone_1", "")
            if telefone:
                t        = re.sub(r"\D", "", telefone)
                telefone = f"({t[:2]}) {t[2:]}" if len(t) >= 10 else t
            endereco = (
                f"{d.get('logradouro','')}, {d.get('numero','')} - "
                f"{d.get('bairro','')}, {d.get('municipio','')}/{d.get('uf','')} - "
                f"CEP {d.get('cep','')}"
            )
            nome_fantasia = (d.get("nome_fantasia", "") or "").strip()
            if not nome_fantasia and razao_social:
                nome_fantasia = extrair_nome_fantasia_do_cnpj(cnpj_limpo, razao_social)
            return telefone.strip(), endereco.strip(), d.get("uf", ""), nome_fantasia
    except Exception:
        pass
    return "", "", "", ""


# ---------------------------------------------------------------------------
# Dominio oficial
# FIX RAIZ: constroi candidatos a partir do nome e valida via HTTP/MX.
# Nao extrai dominio de resultados Google (muito ruidoso).
# ---------------------------------------------------------------------------

SUFIXOS_JURIDICOS = {
    "sa", "s/a", "ltda", "ltda.", "s.a", "s.a.", "do", "de", "da", "dos", "das",
    "brasil", "brasileira", "grupo", "cia", "companhia", "industria", "industrial",
    "instituto", "hospital", "clinica", "centro", "fundacao", "associacao",
    "cooperativa", "servicos", "comercio", "solucoes", "tecnologia",
    "nacional", "internacional", "produtos", "quimicos", "sistemas",
    "eletronicos", "seguranca", "industriais", "servico", "service", "ltda"
}


def construir_candidatos_dominio(nome):
    base   = limpar_texto(nome).lower()
    partes = [p for p in base.split() if p not in SUFIXOS_JURIDICOS and len(p) > 1]
    if not partes:
        return []

    slugs = []
    slugs.append("".join(partes))
    slugs.append("-".join(partes))
    if len(partes) >= 2:
        slugs.append("".join(partes[:2]))
    slugs.append(partes[0])

    candidatos = []
    vistos     = set()
    for slug in slugs:
        for tld in [".com.br", ".com", ".org.br", ".coop.br", ".net.br", ".rio"]:
            dominio = slug + tld
            if dominio not in vistos:
                vistos.add(dominio)
                candidatos.append(dominio)

    return candidatos


def buscar_dominio_oficial(nome_fantasia, razao_social):
    """Tenta construir e validar o dominio sem depender de busca Google."""
    fontes = []
    if nome_fantasia and len(nome_fantasia) > 3:
        fontes.append(nome_fantasia)
    fontes.append(razao_social)

    for fonte in fontes:
        for dominio in construir_candidatos_dominio(fonte):
            if validar_mx(dominio) or validar_http(dominio):
                return dominio

    return None


# ---------------------------------------------------------------------------
# LinkedIn da empresa
# ---------------------------------------------------------------------------

def buscar_linkedin_empresa(nome_busca):
    for r in serpapi_search(f"{nome_busca} linkedin company"):
        if "linkedin.com/company" in r.get("link", ""):
            return r["link"]
    for r in serpapi_search(f"{nome_busca} site:linkedin.com/company"):
        if "linkedin.com/company" in r.get("link", ""):
            return r["link"]
    return None


# ---------------------------------------------------------------------------
# Decisor
# FIX RAIZ: usa nome_busca completo nas queries (nao apenas 1 palavra).
# FIX RAIZ: NAO revalida por nome de empresa no snippet - confia no Google.
# ---------------------------------------------------------------------------

CARGOS_PRIORITARIOS = [
    "esg", "sustentabilidade", "sustainability", "relacoes institucionais",
    "responsabilidade social", "investimento social", "impacto social",
    "comunicacao", "marketing", "patrocinio"
]

CARGOS_EXECUTIVOS = [
    "ceo", "coo", "cfo", "presidente", "vice-presidente", "vice presidente",
    "diretor", "diretora", "director", "socio", "gerente", "head", "manager"
]


def parse_linkedin_result(r):
    link   = r.get("link", "")
    titulo = r.get("title", "")
    if "linkedin.com/in" not in link:
        return None, None, None
    partes = titulo.split(" - ")
    nome   = partes[0].strip()
    cargo  = partes[1].strip() if len(partes) >= 2 else ""
    cargo  = re.sub(r"\s*\|\s*LinkedIn.*$", "", cargo, flags=re.IGNORECASE).strip()
    if not eh_nome_pessoa(nome):
        return None, None, None
    return nome, cargo, link


def buscar_decisor_hunter(dominio):
    if not HUNTER_KEY or not dominio:
        return None, None, None, None
    try:
        r = requests.get("https://api.hunter.io/v2/domain-search", params={
            "domain": dominio,
            "api_key": HUNTER_KEY,
            "limit": 10,
            "seniority": "executive,director,manager"
        }, timeout=15)
        if r.status_code != 200:
            return None, None, None, None
        emails = r.json().get("data", {}).get("emails", [])
        if not emails:
            return None, None, None, None

        for e in emails:
            if any(c in (e.get("position") or "").lower() for c in CARGOS_PRIORITARIOS):
                return (
                    f"{e.get('first_name','')} {e.get('last_name','')}".strip(),
                    e.get("position", ""), e.get("linkedin", ""), e.get("value", "")
                )
        for e in emails:
            if any(c in (e.get("position") or "").lower() for c in CARGOS_EXECUTIVOS):
                return (
                    f"{e.get('first_name','')} {e.get('last_name','')}".strip(),
                    e.get("position", ""), e.get("linkedin", ""), e.get("value", "")
                )
        for e in emails:
            if e.get("first_name") and e.get("last_name"):
                return (
                    f"{e.get('first_name','')} {e.get('last_name','')}".strip(),
                    e.get("position", ""), e.get("linkedin", ""), e.get("value", "")
                )
    except Exception:
        pass
    return None, None, None, None


def buscar_decisor_serpapi(nome_busca):
    """
    Busca decisor no LinkedIn usando nome_busca completo.
    Confia no Google para filtrar relevancia - sem revalidacao por empresa.
    """
    queries = [
        f"{nome_busca} linkedin esg OR sustentabilidade OR \"relacoes institucionais\" OR \"responsabilidade social\"",
        f"{nome_busca} linkedin diretor OR diretora OR CEO OR presidente OR gerente OR socio",
        f"{nome_busca} linkedin",
    ]

    for query in queries:
        for r in serpapi_search(query):
            nome, cargo, link = parse_linkedin_result(r)
            if nome:
                return nome, cargo, link

    return None, None, None


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def verificar_email(email):
    if not ZEROBOUNCE_KEY or not email:
        return "nao verificado"
    try:
        r = requests.get(
            "https://api.zerobounce.net/v2/validate",
            params={"api_key": ZEROBOUNCE_KEY, "email": email, "ip_address": ""},
            timeout=10
        )
        if r.status_code == 200:
            return r.json().get("status", "unknown")
    except Exception:
        pass
    return "unknown"


def gerar_email(nome_decisor, dominio):
    if not nome_decisor or not dominio:
        return ""
    partes = limpar_texto(nome_decisor).lower().split()
    if len(partes) < 2 or len(partes[0]) < 3 or len(partes[-1]) < 3:
        return ""
    return f"{partes[0]}.{partes[-1]}@{dominio}"


# ---------------------------------------------------------------------------
# Endpoint
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

        # 1. Dados CNPJ + nome fantasia
        telefone, endereco, uf, nome_fantasia = buscar_dados_cnpj(cnpj, empresa)

        # 2. Nome para buscas
        nome_busca = nome_fantasia if nome_fantasia and len(nome_fantasia) > 4 else empresa

        # 3. Dominio: construcao + validacao (sem Google)
        dominio_oficial = buscar_dominio_oficial(nome_fantasia, empresa)
        dominio_tem_mx  = validar_mx(dominio_oficial) if dominio_oficial else False

        # 4. LinkedIn empresa
        linkedin_empresa = buscar_linkedin_empresa(nome_busca)

        # 5. Decisor: Hunter primeiro, SerpAPI como fallback
        nome_decisor, cargo_decisor, linkedin_decisor, email_hunter = buscar_decisor_hunter(dominio_oficial)
        fonte_decisor = "hunter" if nome_decisor else ""

        if not nome_decisor:
            nome_decisor, cargo_decisor, linkedin_decisor = buscar_decisor_serpapi(nome_busca)
            fonte_decisor = "serpapi" if nome_decisor else ""

        # 6. Email
        if email_hunter:
            email_previsto = email_hunter
            email_status   = "hunter_verified"
        elif nome_decisor and dominio_oficial:
            email_previsto = gerar_email(nome_decisor, dominio_oficial)
            email_status   = verificar_email(email_previsto) if email_previsto else "sem email"
        else:
            email_previsto = ""
            email_status   = "sem email"

        resultados.append({
            "empresa":          empresa,
            "nome_fantasia":    nome_fantasia,
            "cnpj":             cnpj,
            "uf":               uf,
            "telefone_sede":    telefone,
            "endereco_sede":    endereco,
            "dominio_oficial":  dominio_oficial  or "",
            "dominio_tem_mx":   dominio_tem_mx,
            "linkedin_empresa": linkedin_empresa or "",
            "decisor_nome":     nome_decisor     or "",
            "decisor_cargo":    cargo_decisor    or "",
            "decisor_linkedin": linkedin_decisor or "",
            "email_previsto":   email_previsto   or "",
            "email_status":     email_status,
            "fonte_decisor":    fonte_decisor,
        })

    df_final = pd.DataFrame(resultados)
    output   = io.StringIO()
    df_final.to_csv(output, index=False)
    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=resultado_pipeline.csv"}
    )

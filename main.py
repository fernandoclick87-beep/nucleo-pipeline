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

SERPAPI_KEY = os.getenv("SERPAPI_KEY")
ZEROBOUNCE_KEY = os.getenv("ZEROBOUNCE_KEY")

# ==============================
# UTILIDADES
# ==============================

def limpar_texto(texto):
    if not texto:
        return ""
    texto = unicodedata.normalize("NFKD", str(texto))
    texto = texto.encode("ascii", "ignore").decode("utf-8")
    return texto.strip()

def normalizar_coluna(nome: str) -> str:
    nome = unicodedata.normalize("NFKD", str(nome))
    nome = "".join(c for c in nome if not unicodedata.combining(c))
    nome = nome.lower().strip()
    nome = re.sub(r"\s+", "_", nome)
    return nome

def mapear_colunas(df):
    # Aceita variações comuns
    MAPA_EMPRESA = {"organizacao", "organização", "razao_social", "razao", "razão", "empresa", "nome_empresa", "nome", "razao social"}
    MAPA_CNPJ = {"cnpj", "documento", "doc", "nr_cnpj", "num_cnpj", "cnpj_"}
    # UF é opcional (você falou que prefere não ter)
    MAPA_UF = {"uf", "estado"}

    df.columns = [normalizar_coluna(c) for c in df.columns]

    col_empresa = next((c for c in df.columns if c in {normalizar_coluna(x) for x in MAPA_EMPRESA}), None)
    col_cnpj = next((c for c in df.columns if c in {normalizar_coluna(x) for x in MAPA_CNPJ}), None)
    col_uf = next((c for c in df.columns if c in {normalizar_coluna(x) for x in MAPA_UF}), None)

    if not col_empresa or not col_cnpj:
        return None, "CSV precisa conter colunas compatíveis com EMPRESA/ORGANIZAÇÃO e CNPJ."

    ren = {col_empresa: "empresa", col_cnpj: "cnpj"}
    if col_uf:
        ren[col_uf] = "uf"

    df = df.rename(columns=ren)

    if "uf" not in df.columns:
        df["uf"] = ""

    return df, None

def cnpj_limpo_14(cnpj: str) -> str:
    c = re.sub(r"\D", "", str(cnpj))
    return c.zfill(14)

def validar_mx(dominio):
    if not dominio:
        return False
    try:
        dns.resolver.resolve(dominio, "MX")
        return True
    except:
        return False

def extrair_dominio(url: str):
    if not url:
        return None
    m = re.search(r"https?://(?:www\.)?([^/]+)", url.strip())
    return m.group(1).lower() if m else None

def limpar_url(url: str) -> str:
    if not url:
        return ""
    return url.split("?")[0].strip()

# ==============================
# CNPJ / BRASILAPI
# ==============================

def buscar_dados_cnpj(cnpj: str):
    cnpj14 = cnpj_limpo_14(cnpj)
    try:
        r = requests.get(f"https://brasilapi.com.br/api/cnpj/v1/{cnpj14}", timeout=15)
        if r.status_code != 200:
            return "", "", "", ""
        d = r.json()

        # telefone (quando disponível)
        telefone = d.get("ddd_telefone_1", "") or ""
        telefone = re.sub(r"\D", "", str(telefone))
        if len(telefone) >= 10:
            telefone = f"({telefone[:2]}) {telefone[2:]}"
        telefone = telefone.strip()

        # endereço
        logradouro = d.get("logradouro", "") or ""
        numero = d.get("numero", "") or ""
        bairro = d.get("bairro", "") or ""
        municipio = d.get("municipio", "") or ""
        uf = (d.get("uf", "") or "").strip()
        cep = d.get("cep", "") or ""

        endereco = f"{logradouro}, {numero} - {bairro}, {municipio}/{uf} - CEP {cep}".strip()
        endereco = re.sub(r"\s+", " ", endereco)

        nome_fantasia = (d.get("nome_fantasia", "") or "").strip()

        return telefone, endereco, uf, nome_fantasia
    except:
        return "", "", "", ""

# ==============================
# ZEROBOUNCE
# ==============================

def verificar_email(email: str) -> str:
    if not ZEROBOUNCE_KEY or not email:
        return "nao_verificado"
    try:
        r = requests.get(
            "https://api.zerobounce.net/v2/validate",
            params={"api_key": ZEROBOUNCE_KEY, "email": email, "ip_address": ""},
            timeout=15
        )
        if r.status_code == 200:
            return r.json().get("status", "unknown")
    except:
        pass
    return "unknown"

# ==============================
# SERPAPI
# ==============================

def serpapi_search(query: str, num: int = 10):
    if not SERPAPI_KEY:
        return []
    try:
        r = requests.get(
            "https://serpapi.com/search",
            params={
                "engine": "google",
                "q": query,
                "api_key": SERPAPI_KEY,
                "num": num,
                "hl": "pt",
                "gl": "br",
            },
            timeout=25
        )
        j = r.json()
        return j.get("organic_results", []) or []
    except:
        return []

def nome_para_busca(nome_fantasia: str, empresa: str) -> str:
    # Se nome fantasia for útil, usa. Caso contrário, usa razão social.
    nf = (nome_fantasia or "").strip()
    if nf and len(nf.split()) >= 2:
        return nf
    return (empresa or "").strip()

# ==============================
# LINKEDIN EMPRESA + DOMÍNIO
# ==============================

def buscar_linkedin_e_dominio(nome_busca: str):
    linkedin_empresa = ""
    dominio_oficial = ""

    ignorar_dominio = [
        "linkedin.", "facebook.", "instagram.", "x.com", "twitter.",
        "cnpj.", "cnpj.info", "empresas.", "econodata", "jusbrasil",
        "wikipedia", "google.", "brasilapi", "glassdoor", "indeed", "catho", "infojobs",
        "leadiq", "apollo", "hunter", "zoominfo", "reclameaqui",
        "valor.com", "exame.com", "infomoney.com", "serasa", "serasaexperian"
    ]

    # 1) tenta capturar LinkedIn company
    # 2) tenta capturar algum site oficial (domínio que não seja agregador)
    query = f'{nome_busca} (site oficial OR "website") LinkedIn company'
    results = serpapi_search(query, num=10)

    for r in results:
        link = limpar_url(r.get("link", "") or "")
        if not link:
            continue

        low = link.lower()

        if (not linkedin_empresa) and ("linkedin.com/company" in low):
            linkedin_empresa = link

        if not dominio_oficial:
            if not any(bad in low for bad in ignorar_dominio):
                dom = extrair_dominio(link)
                if dom and "." in dom and len(dom) >= 6:
                    dominio_oficial = dom

        if linkedin_empresa and dominio_oficial:
            break

    # fallback: se não achou domínio, tenta query só de site oficial
    if not dominio_oficial:
        results2 = serpapi_search(f'{nome_busca} "site oficial"', num=10)
        for r in results2:
            link = limpar_url(r.get("link", "") or "")
            if not link:
                continue
            low = link.lower()
            if any(bad in low for bad in ignorar_dominio):
                continue
            dom = extrair_dominio(link)
            if dom and "." in dom and len(dom) >= 6:
                dominio_oficial = dom
                break

    return linkedin_empresa, dominio_oficial

# ==============================
# DECISOR (camadas: forte -> ampla)
# ==============================

def _decisor_parse_result(r):
    link = limpar_url(r.get("link", "") or "")
    title = (r.get("title", "") or "").strip()
    snippet = (r.get("snippet", "") or "").strip()
    if "linkedin.com/in" not in (link.lower()):
        return None

    # nome provável
    nome = title.split(" - ")[0].strip() if " - " in title else title.strip()
    if len(nome.split()) < 2:
        return None

    cargo = title  # deixa o title inteiro como "cargo" aproximado (melhor que vazio)
    return nome, cargo, link, snippet[:180]

def buscar_decisor(nome_busca: str):
    """
    Estratégia:
    1) Busca forte com temas (ESG, Sustentabilidade, Marketing, Financeiro, Pessoas/RH)
    2) Se não achar, busca ampla só por empresa + linkedin.com/in
    Retorna: (nome, cargo_texto, link, snippet)
    """
    # termos que capturam padrões reais (inclui Diretoria de Pessoas / Gente e Gestão)
    blocos = [
        '"ESG" OR Sustentabilidade OR "Investimento Social" OR "Responsabilidade Social"',
        'Marketing OR Comunicacao OR Comunicação OR Marca OR "Assuntos Corporativos"',
        'Financeiro OR Financas OR Finanças OR "Relações com Investidores" OR Investimentos',
        '"Gente e Gestao" OR "Gente & Gestao" OR Pessoas OR RH OR "Recursos Humanos" OR "People" OR "People & Culture"'
    ]
    # tentativa forte (prioriza achar alguém com algum desses temas)
    query_strong = f'"{nome_busca}" ({ " OR ".join(blocos) }) site:linkedin.com/in'
    for r in serpapi_search(query_strong, num=10):
        parsed = _decisor_parse_result(r)
        if parsed:
            nome, cargo, link, snippet = parsed
            return nome, cargo, link, snippet

    # fallback amplo
    query_soft = f'"{nome_busca}" site:linkedin.com/in'
    for r in serpapi_search(query_soft, num=10):
        parsed = _decisor_parse_result(r)
        if parsed:
            nome, cargo, link, snippet = parsed
            return nome, cargo, link, snippet

    return "", "", "", ""

def gerar_email_provavel(nome_decisor: str, dominio: str):
    if not nome_decisor or not dominio:
        return ""
    partes = limpar_texto(nome_decisor).lower().split()
    if len(partes) < 2:
        return ""
    primeiro = partes[0]
    ultimo = partes[-1]
    if len(primeiro) <= 2 or len(ultimo) <= 2:
        return ""
    return f"{primeiro}.{ultimo}@{dominio}"

# ==============================
# ENDPOINT
# ==============================

@app.post("/processar")
async def processar_csv(file: UploadFile = File(...)):
    # lê bytes para permitir tentar encodings diferentes
    try:
        raw = await file.read()
        try:
            df_input = pd.read_csv(io.BytesIO(raw), sep=None, engine="python", encoding="utf-8-sig")
        except:
            df_input = pd.read_csv(io.BytesIO(raw), sep=None, engine="python", encoding="latin1")
    except Exception as e:
        return JSONResponse(status_code=400, content={"erro": f"Assinatura/arquivo inválido: {str(e)}"})

    df_input, erro = mapear_colunas(df_input)
    if erro:
        return JSONResponse(status_code=400, content={"erro": erro})

    resultados = []

    for _, row in df_input.iterrows():
        empresa = str(row.get("empresa", "")).strip()
        cnpj = str(row.get("cnpj", "")).strip()

        telefone, endereco, uf_api, nome_fantasia = buscar_dados_cnpj(cnpj)

        # UF: se o CSV tiver UF, mantém; se não tiver, usa a da BrasilAPI
        uf_csv = str(row.get("uf", "")).strip()
        uf_final = uf_csv if uf_csv else (uf_api or "")

        nome_busca = nome_para_busca(nome_fantasia, empresa)

        linkedin_empresa, dominio_oficial = buscar_linkedin_e_dominio(nome_busca)
        dominio_tem_mx = validar_mx(dominio_oficial)

        decisor_nome, decisor_cargo, decisor_linkedin, decisor_snippet = buscar_decisor(nome_busca)

        email_previsto = gerar_email_provavel(decisor_nome, dominio_oficial) if dominio_tem_mx else ""
        email_status = verificar_email(email_previsto) if email_previsto else "sem_email"

        resultados.append({
            "empresa": empresa,
            "nome_fantasia": nome_fantasia,
            "cnpj": cnpj_limpo_14(cnpj),
            "uf": uf_final,
            "telefone_sede": telefone,
            "endereco_sede": endereco,
            "dominio_oficial": dominio_oficial,
            "dominio_tem_mx": dominio_tem_mx,
            "linkedin_empresa": linkedin_empresa,
            "decisor_nome": decisor_nome,
            "decisor_cargo": decisor_cargo,
            "decisor_linkedin": decisor_linkedin,
            "decisor_contexto": decisor_snippet,
            "email_previsto": email_previsto,
            "email_status": email_status,
        })

    df_final = pd.DataFrame(resultados)

    out = io.StringIO()
    df_final.to_csv(out, index=False)
    out.seek(0)

    return StreamingResponse(
        iter([out.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=resultado_pipeline.csv"}
    )

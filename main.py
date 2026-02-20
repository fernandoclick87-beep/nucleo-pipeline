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
APOLLO_KEY     = os.getenv("APOLLO_KEY")

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
    return nome.lower().strip().replace(" ", "_")

def mapear_colunas(df):
    MAPA_EMPRESA = {"organizacao", "razao_social", "razao", "empresa", "nome_empresa", "nome"}
    MAPA_CNPJ    = {"cnpj", "documento", "doc", "nr_cnpj", "num_cnpj"}
    df.columns   = [normalizar_coluna(c) for c in df.columns]
    col_empresa  = next((c for c in df.columns if c in MAPA_EMPRESA), None)
    col_cnpj     = next((c for c in df.columns if c in MAPA_CNPJ), None)
    if not col_empresa or not col_cnpj:
        return None, "CSV precisa conter colunas compatíveis com empresa e cnpj"
    return df.rename(columns={col_empresa: "empresa", col_cnpj: "cnpj"}), None

def validar_mx(dominio):
    if not dominio:
        return False
    try:
        dns.resolver.resolve(dominio, "MX")
        return True
    except:
        return False

def nome_simples(nome_empresa: str) -> str:
    ignorar = {
        "sa", "s/a", "ltda", "ltda.", "s.a", "s.a.", "do", "de", "da", "dos", "das",
        "brasil", "brasileira", "grupo", "cia", "companhia", "industria",
        "instituto", "hospital", "clinica", "centro", "fundacao", "associacao",
        "cooperativa", "servicos", "comercio", "solucoes", "tecnologia",
        "patologia", "diagnostica", "laboratorio", "nacional", "internacional"
    }
    partes = limpar_texto(nome_empresa).lower().split()
    principais = [p for p in partes if p not in ignorar and len(p) > 3]
    return principais[0] if principais else partes[0] if partes else ""

# ==============================
# BRASILAPI — GRÁTIS
# ==============================

def buscar_dados_cnpj(cnpj: str):
    cnpj_limpo = re.sub(r"\D", "", str(cnpj)).zfill(14)
    try:
        r = requests.get(
            f"https://brasilapi.com.br/api/cnpj/v1/{cnpj_limpo}",
            timeout=10
        )
        if r.status_code == 200:
            d = r.json()
            telefone = d.get("ddd_telefone_1", "")
            if telefone:
                t = re.sub(r"\D", "", telefone)
                telefone = f"({t[:2]}) {t[2:]}" if len(t) >= 10 else t
            endereco = (f"{d.get('logradouro','')}, {d.get('numero','')} - "
                        f"{d.get('bairro','')}, {d.get('municipio','')}/{d.get('uf','')} - "
                        f"CEP {d.get('cep','')}")
            nome_fantasia = limpar_texto(d.get("nome_fantasia", "") or "")
            return telefone.strip(), endereco.strip(), d.get("uf", ""), nome_fantasia
    except:
        pass
    return "", "", "", ""

# ==============================
# APOLLO — DECISOR + EMAIL
# ==============================

def buscar_decisor_apollo(nome_empresa, dominio):
    """
    Busca decisor na base Apollo por nome da empresa e domínio.
    Prioriza cargos de ESG, Sustentabilidade, Marketing, Financeiro, Diretoria.
    Retorna nome, cargo, linkedin, email.
    """
    if not APOLLO_KEY:
        return None, None, None, None

    cargos_alvo = [
        "ESG", "Sustentabilidade", "Sustainability",
        "Marketing", "Financeiro", "Finance",
        "Diretor", "Director", "VP", "Gerente"
    ]

    try:
        payload = {
            "api_key": APOLLO_KEY,
            "q_organization_name": nome_empresa,
            "organization_domains": [dominio] if dominio else [],
            "person_titles": cargos_alvo,
            "per_page": 5
        }
        r = requests.post(
            "https://api.apollo.io/v1/mixed_people/search",
            json=payload,
            timeout=15
        )
        if r.status_code == 200:
            pessoas = r.json().get("people", [])
            for p in pessoas:
                nome  = f"{p.get('first_name','')} {p.get('last_name','')}".strip()
                cargo = p.get("title", "")
                email = p.get("email", "") or ""
                linkedin = p.get("linkedin_url", "") or ""
                if nome:
                    return nome, cargo, linkedin, email
    except:
        pass
    return None, None, None, None

# ==============================
# ZEROBOUNCE — só usa se Apollo não trouxer email
# ==============================

def verificar_email(email: str) -> str:
    if not ZEROBOUNCE_KEY or not email:
        return "não verificado"
    try:
        r = requests.get(
            "https://api.zerobounce.net/v2/validate",
            params={"api_key": ZEROBOUNCE_KEY, "email": email, "ip_address": ""},
            timeout=10
        )
        if r.status_code == 200:
            return r.json().get("status", "unknown")
    except:
        pass
    return "unknown"

# ==============================
# SERPAPI — só LinkedIn + domínio
# ==============================

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
    except:
        return []

def buscar_linkedin_e_dominio(nome_busca):
    chave = nome_simples(nome_busca)
    linkedin_empresa = None
    dominio_oficial  = None

    ignorar_dominio = [
        "linkedin", "facebook", "instagram", "receitafederal",
        "cnpj.info", "empresas.net", "econodata", "jusbrasil",
        "tabelasalarios", "wikipedia", "google", "brasilapi",
        "glassdoor", "indeed", "catho", "infojobs",
        "leadiq", "apollo", "hunter", "zoominfo", "reclameaqui",
        "valor.com", "exame.com", "infomoney", "serasaexperian"
    ]

    palavras_nome = [p for p in nome_busca.lower().split() if len(p) > 4
                     and p not in {"ltda", "brasil", "grupo", "instituto",
                                   "hospital", "clinica", "servicos"}]

    for r in serpapi_search(f"{nome_busca} LinkedIn empresa site oficial"):
        link     = r.get("link", "")
        contexto = (r.get("title", "") + " " + r.get("snippet", "")).lower()

        if not linkedin_empresa and "linkedin.com/company" in link:
            if any(p in contexto for p in palavras_nome) or chave in link.lower():
                linkedin_empresa = link

        if not dominio_oficial and link:
            if not any(i in link.lower() for i in ignorar_dominio):
                match = re.search(r"https?://(?:www\.)?([^/]+)", link)
                if match:
                    dominio_oficial = match.group(1)

        if linkedin_empresa and dominio_oficial:
            break

    return linkedin_empresa, dominio_oficial

def gerar_email_provavel(nome_decisor, dominio):
    if not nome_decisor or not dominio:
        return None
    partes = limpar_texto(nome_decisor).lower().split()
    if len(partes) < 2 or any(len(p) <= 2 for p in [partes[0], partes[-1]]):
        return None
    return f"{partes[0]}.{partes[-1]}@{dominio}"

# ==============================
# ENDPOINT PRINCIPAL
# ==============================

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
        empresa = limpar_texto(row.get("empresa", ""))
        cnpj    = str(row.get("cnpj", "")).strip()

        # GRÁTIS
        telefone, endereco, uf, nome_fantasia = buscar_dados_cnpj(cnpj)
        nome_busca = nome_fantasia if nome_fantasia else empresa

        # 1 crédito SerpAPI — LinkedIn + domínio
        linkedin_empresa, dominio_oficial = buscar_linkedin_e_dominio(nome_busca)
        dominio_tem_mx = validar_mx(dominio_oficial)

        # Apollo — decisor + email verificado (consome crédito Apollo)
        nome_decisor, cargo_decisor, linkedin_decisor, email_apollo = buscar_decisor_apollo(
            nome_busca, dominio_oficial
        )

        # Se Apollo trouxe email, usa direto; senão gera e valida com ZeroBounce
        if email_apollo:
            email_previsto = email_apollo
            email_status   = "apollo-verified"
        else:
            email_previsto = gerar_email_provavel(nome_decisor, dominio_oficial) if dominio_tem_mx else None
            email_status   = verificar_email(email_previsto) if email_previsto else "sem email"

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

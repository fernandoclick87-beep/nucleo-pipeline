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

    df.columns = [normalizar_coluna(c) for c in df.columns]
    col_empresa = next((c for c in df.columns if c in MAPA_EMPRESA), None)
    col_cnpj    = next((c for c in df.columns if c in MAPA_CNPJ), None)

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

def serpapi_search(query):
    if not SERPAPI_KEY:
        return []
    try:
        r = requests.get("https://serpapi.com/search", params={
            "engine": "google",
            "q": query,
            "api_key": SERPAPI_KEY,
            "num": 5
        }, timeout=10)
        return r.json().get("organic_results", [])
    except:
        return []

def buscar_linkedin_empresa(nome_empresa):
    for r in serpapi_search(f"{nome_empresa} site:linkedin.com/company"):
        link = r.get("link", "")
        if "linkedin.com/company" in link:
            return link
    return None

def buscar_dominio_real(nome_empresa, cnpj):
    """Busca o site oficial da empresa, não o slug do LinkedIn."""
    resultados = serpapi_search(f"{nome_empresa} CNPJ {cnpj} site oficial")
    for r in resultados:
        link = r.get("link", "")
        if not link:
            continue
        # Ignora redes sociais, portais genéricos e agregadores
        ignorar = ["linkedin", "facebook", "instagram", "receitafederal",
                   "cnpj.info", "empresas.net", "econodata", "jusbrasil",
                   "tabelasalarios", "wikipedia", "google"]
        if any(i in link.lower() for i in ignorar):
            continue
        match = re.search(r"https?://(?:www\.)?([^/]+)", link)
        if match:
            return match.group(1)
    return None

def buscar_decisor(nome_empresa):
    """Busca decisor de ESG/Sustentabilidade/Marketing/Financeiro no LinkedIn."""
    cargos = ["ESG", "Sustentabilidade", "Marketing", "Financeiro", "Diretoria"]
    for cargo in cargos:
        query = f"{nome_empresa} {cargo} site:linkedin.com/in"
        for r in serpapi_search(query):
            link = r.get("link", "")
            titulo = r.get("title", "")
            snippet = r.get("snippet", "")
            if "linkedin.com/in" in link:
                nome_decisor = titulo.split(" - ")[0].strip() if " - " in titulo else titulo.strip()
                cargo_decisor = snippet[:120] if snippet else cargo
                return nome_decisor, cargo_decisor, link
    return None, None, None

def gerar_email_provavel(nome_decisor, dominio):
    """Gera padrões comuns de email corporativo."""
    if not nome_decisor or not dominio:
        return None
    partes = limpar_texto(nome_decisor).lower().split()
    if len(partes) < 2:
        return None
    primeiro = partes[0]
    ultimo   = partes[-1]
    # Padrão mais comum no Brasil: primeiro.ultimo@dominio
    return f"{primeiro}.{ultimo}@{dominio}"

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
        uf      = str(row.get("uf", "")).strip()

        # 1. LinkedIn da empresa
        linkedin_empresa = buscar_linkedin_empresa(empresa)

        # 2. Domínio real (não o slug do LinkedIn)
        dominio_oficial = buscar_dominio_real(empresa, cnpj)

        # 3. Validação MX no domínio real
        dominio_tem_mx = validar_mx(dominio_oficial)

        # 4. Decisor
        nome_decisor, cargo_decisor, linkedin_decisor = buscar_decisor(empresa)

        # 5. Email provável (só gera se domínio tem MX ativo)
        email_previsto = None
        if dominio_tem_mx:
            email_previsto = gerar_email_provavel(nome_decisor, dominio_oficial)

        resultados.append({
            "empresa":           empresa,
            "cnpj":              cnpj,
            "uf":                uf,
            "dominio_oficial":   dominio_oficial,
            "dominio_tem_mx":    dominio_tem_mx,
            "linkedin_empresa":  linkedin_empresa,
            "decisor_nome":      nome_decisor or "",
            "decisor_cargo":     cargo_decisor or "",
            "decisor_linkedin":  linkedin_decisor or "",
            "email_previsto":    email_previsto or "",
        })

    df_final = pd.DataFrame(resultados)
    output = io.StringIO()
    df_final.to_csv(output, index=False)
    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=resultado_pipeline.csv"}
    )

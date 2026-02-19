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

# ==============================
# BRASILAPI — GRÁTIS
# ==============================

def buscar_dados_cnpj(cnpj: str):
    """Busca telefone, endereço e UF direto na Receita Federal via BrasilAPI."""
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
                telefone = re.sub(r"\D", "", telefone)
                if len(telefone) >= 10:
                    telefone = f"({telefone[:2]}) {telefone[2:]}"
            logradouro  = d.get("logradouro", "")
            numero      = d.get("numero", "")
            bairro      = d.get("bairro", "")
            municipio   = d.get("municipio", "")
            uf          = d.get("uf", "")
            cep         = d.get("cep", "")
            endereco = f"{logradouro}, {numero} - {bairro}, {municipio}/{uf} - CEP {cep}"
            return telefone.strip(), endereco.strip(), uf.strip()
    except:
        pass
    return "", "", ""

# ==============================
# SERPAPI — 1 CRÉDITO POR BUSCA
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

def buscar_linkedin_empresa(nome_empresa):
    for r in serpapi_search(f"{nome_empresa} site:linkedin.com/company"):
        link = r.get("link", "")
        if "linkedin.com/company" in link:
            return link
    return None

def buscar_dominio_real(nome_empresa, cnpj):
    ignorar = ["linkedin", "facebook", "instagram", "receitafederal",
               "cnpj.info", "empresas.net", "econodata", "jusbrasil",
               "tabelasalarios", "wikipedia", "google", "brasilapi"]
    for r in serpapi_search(f"{nome_empresa} CNPJ {cnpj} site oficial"):
        link = r.get("link", "")
        if not link or any(i in link.lower() for i in ignorar):
            continue
        match = re.search(r"https?://(?:www\.)?([^/]+)", link)
        if match:
            return match.group(1)
    return None

def buscar_decisor(nome_empresa):
    cargos = ["ESG", "Sustentabilidade", "Marketing", "Financeiro", "Diretoria"]
    for cargo in cargos:
        for r in serpapi_search(f"{nome_empresa} {cargo} site:linkedin.com/in"):
            link    = r.get("link", "")
            titulo  = r.get("title", "")
            snippet = r.get("snippet", "")
            if "linkedin.com/in" in link:
                nome_decisor  = titulo.split(" - ")[0].strip() if " - " in titulo else titulo.strip()
                cargo_decisor = snippet[:120] if snippet else cargo
                return nome_decisor, cargo_decisor, link
    return None, None, None

def gerar_email_provavel(nome_decisor, dominio):
    if not nome_decisor or not dominio:
        return None
    partes = limpar_texto(nome_decisor).lower().split()
    if len(partes) < 2:
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

        # GRÁTIS — BrasilAPI
        telefone, endereco, uf = buscar_dados_cnpj(cnpj)

        # 1 crédito — LinkedIn empresa
        linkedin_empresa = buscar_linkedin_empresa(empresa)

        # 1 crédito — Domínio real
        dominio_oficial = buscar_dominio_real(empresa, cnpj)
        dominio_tem_mx  = validar_mx(dominio_oficial)

        # 1 crédito — Decisor
        nome_decisor, cargo_decisor, linkedin_decisor = buscar_decisor(empresa)

        # Grátis — Email gerado
        email_previsto = None
        if dominio_tem_mx:
            email_previsto = gerar_email_provavel(nome_decisor, dominio_oficial)

        resultados.append({
            "empresa":          empresa,
            "cnpj":             cnpj,
            "uf":               uf,
            "telefone_sede":    telefone,
            "endereco_sede":    endereco,
            "dominio_oficial":  dominio_oficial,
            "dominio_tem_mx":   dominio_tem_mx,
            "linkedin_empresa": linkedin_empresa,
            "decisor_nome":     nome_decisor     or "",
            "decisor_cargo":    cargo_decisor    or "",
            "decisor_linkedin": linkedin_decisor or "",
            "email_previsto":   email_previsto   or "",
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

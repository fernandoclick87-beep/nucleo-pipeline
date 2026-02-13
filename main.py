import os
import pandas as pd
import requests
import re
import unicodedata
import dns.resolver

from fastapi import FastAPI, UploadFile, File
from fastapi.responses import JSONResponse

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


def validar_mx(dominio):
    try:
        registros = dns.resolver.resolve(dominio, 'MX')
        return True
    except:
        return False


def buscar_linkedin_empresa(nome_empresa):
    if not SERPAPI_KEY:
        return None

    query = f"{nome_empresa} LinkedIn"
    url = "https://serpapi.com/search"

    params = {
        "engine": "google",
        "q": query,
        "api_key": SERPAPI_KEY
    }

    try:
        response = requests.get(url, params=params)
        data = response.json()

        if "organic_results" in data:
            for resultado in data["organic_results"]:
                link = resultado.get("link", "")
                if "linkedin.com/company" in link:
                    return link

        return None

    except:
        return None


# ==============================
# ENDPOINT PRINCIPAL
# ==============================

@app.post("/processar")
async def processar_csv(file: UploadFile = File(...)):

    try:
        df_input = pd.read_csv(file.file, sep=None, engine="python")
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"erro": f"Erro ao ler CSV: {str(e)}"}
        )

    resultados = []

    for _, row in df_input.iterrows():

        empresa = limpar_texto(row.get("empresa", ""))
        cnpj = str(row.get("cnpj", ""))
        uf = row.get("uf", "")

        linkedin = buscar_linkedin_empresa(empresa)

        dominio_oficial = None
        dominio_tem_mx = False

        if linkedin:
            dominio_oficial = linkedin.split("/")[-1]
            dominio_tem_mx = validar_mx(dominio_oficial)

        resultados.append({
            "empresa": empresa,
            "cnpj": cnpj,
            "uf": uf,
            "linkedin_empresa": linkedin,
            "dominio_oficial": dominio_oficial,
            "dominio_tem_mx": dominio_tem_mx
        })

    return {
        "total_processado": len(resultados),
        "dados": resultados
    }

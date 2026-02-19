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


def extrair_slug_linkedin(link):
    if not link:
        return None
    partes = link.split("/")
    if "company" in partes:
        idx = partes.index("company")
        if idx + 1 < len(partes):
            slug = partes[idx + 1]
            slug = slug.replace("?trk=public_post_main-feed-card-text", "")
            slug = slug.replace("jobs", "")
            return slug
    return None


def buscar_linkedin_empresa(nome_empresa):
    if not SERPAPI_KEY:
        return None

    query = f"{nome_empresa} site:linkedin.com/company"
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


def buscar_decisores(nome_empresa):
    if not SERPAPI_KEY:
        return []

    query = f'{nome_empresa} ("ESG" OR "Sustentabilidade" OR "Marketing" OR "Financeiro") site:linkedin.com/in'
    url = "https://serpapi.com/search"

    params = {
        "engine": "google",
        "q": query,
        "api_key": SERPAPI_KEY,
        "num": 5
    }

    try:
        response = requests.get(url, params=params)
        data = response.json()

        decisores = []

        if "organic_results" in data:
            for resultado in data["organic_results"]:
                titulo = resultado.get("title", "")
                link = resultado.get("link", "")

                if "linkedin.com/in" in link:
                    decisores.append({
                        "nome_cargo": titulo,
                        "linkedin": link
                    })

        return decisores

    except:
        return []


def gerar_email_provavel(nome_decisor, dominio):
    if not dominio:
        return None

    nome_limpo = limpar_texto(nome_decisor)
    partes = nome_limpo.split(" ")

    if len(partes) >= 2:
        primeiro = partes[0].lower()
        ultimo = partes[-1].lower()
        return f"{primeiro}.{ultimo}@{dominio}"

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

        linkedin_empresa = buscar_linkedin_empresa(empresa)
        slug = extrair_slug_linkedin(linkedin_empresa)

        dominio_oficial = slug
        dominio_tem_mx = validar_mx(dominio_oficial) if dominio_oficial else False

        decisores = buscar_decisores(empresa)

        if not decisores:
            resultados.append({
                "empresa": empresa,
                "cnpj": cnpj,
                "linkedin_empresa": linkedin_empresa,
                "dominio_oficial": dominio_oficial,
                "dominio_tem_mx": dominio_tem_mx,
                "decisor_nome_cargo": None,
                "decisor_linkedin": None,
                "email_provavel": None
            })
        else:
            for d in decisores:
                email_provavel = gerar_email_provavel(d["nome_cargo"], dominio_oficial)

                resultados.append({
                    "empresa": empresa,
                    "cnpj": cnpj,
                    "linkedin_empresa": linkedin_empresa,
                    "dominio_oficial": dominio_oficial,
                    "dominio_tem_mx": dominio_tem_mx,
                    "decisor_nome_cargo": d["nome_cargo"],
                    "decisor_linkedin": d["linkedin"],
                    "email_provavel": email_provavel
                })

    return {
        "total_processado": len(resultados),
        "dados": resultados
    }

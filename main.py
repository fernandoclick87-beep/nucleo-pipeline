import pandas as pd
import json
import requests
import re
import unicodedata
import dns.resolver
from urllib.parse import urlparse

# ====== CONFIG ======
with open("config.json", "r") as f:
    config = json.load(f)

SERP_API_KEY = config["serpapi_key"]
MAX_DECISORES = config["max_decisores_por_empresa"]
SCORE_MINIMO = config["score_minimo_pronto"]


# ====== UTILS ======
def limpar_hostname(hostname):
    if not hostname:
        return ""
    h = hostname.strip().lower()
    if h.startswith("www."):
        h = h[4:]
    return h


def limpar_nome(nome):
    nome = unicodedata.normalize("NFKD", nome)
    nome = "".join([c for c in nome if not unicodedata.combining(c)])
    nome = re.sub(r"[^A-Za-z\s]", "", nome)
    return nome.lower().strip()


def verificar_mx(dominio):
    try:
        dns.resolver.resolve(dominio, 'MX')
        return True
    except:
        return False


# ====== COLETA ======
def buscar_cnpj(cnpj):
    try:
        url = f"https://brasilapi.com.br/api/cnpj/v1/{cnpj}"
        r = requests.get(url, timeout=10)
        data = r.json()
        telefone = data.get("ddd_telefone_1", "")
        endereco = f'{data.get("logradouro", "")}, {data.get("numero", "")} - {data.get("municipio", "")}/{data.get("uf", "")}'
        return telefone, endereco
    except:
        return "", ""


def buscar_dominio(nome_empresa):
    query = f'"{nome_empresa}" site oficial'
    params = {
        "engine": "google",
        "q": query,
        "api_key": SERP_API_KEY,
        "num": 3
    }
    try:
        response = requests.get("https://serpapi.com/search", params=params)
        data = response.json()
        if "organic_results" in data:
            for item in data["organic_results"]:
                link = item.get("link", "")
                host = urlparse(link).netloc
                host = limpar_hostname(host)
                if host:
                    return host
        return ""
    except:
        return ""


def buscar_decisores(nome_empresa):
    query = f'site:linkedin.com/in "{nome_empresa}" ("Diretor" OR "Head" OR "Gerente" OR "Sustentabilidade" OR "ESG" OR "Institucional" OR "Financeiro")'
    params = {
        "engine": "google",
        "q": query,
        "api_key": SERP_API_KEY,
        "num": MAX_DECISORES
    }
    try:
        response = requests.get("https://serpapi.com/search", params=params)
        data = response.json()
        resultados = []
        if "organic_results" in data:
            for item in data["organic_results"]:
                resultados.append({
                    "titulo": item.get("title", ""),
                    "linkedin": item.get("link", "")
                })
        return resultados
    except:
        return []


# ====== SCORE ======
def calcular_score(titulo):
    score = 0
    t = titulo.lower()

    if "diretor" in t:
        score += 40
    if "head" in t:
        score += 35
    if "gerente" in t:
        score += 25
    if "sustentabilidade" in t or "esg" in t:
        score += 30
    if "marketing" in t:
        score += 25
    if "institucional" in t:
        score += 20
    if "financeiro" in t:
        score += 20

    return score


def gerar_email(titulo, dominio):
    if not titulo or not dominio:
        return ""

    nome = titulo.split(" - ")[0]
    nome = limpar_nome(nome)
    partes = nome.split()

    if len(partes) >= 2:
        return f"{partes[0]}.{partes[-1]}@{dominio}"
    elif len(partes) == 1:
        return f"{partes[0]}@{dominio}"
    return ""


# ====== MAIN ======
def run_pipeline():
    df_input = pd.read_csv("input.csv")
    dados_finais = []

    for _, row in df_input.iterrows():
        empresa = row["empresa"]
        cnpj = str(row["cnpj"])

        print(f"Processando: {empresa}")

        telefone, endereco = buscar_cnpj(cnpj)
        dominio = buscar_dominio(empresa)
        dominio_mx = verificar_mx(dominio) if dominio else False
        decisores = buscar_decisores(empresa)

        for d in decisores:
            score = calcular_score(d["titulo"])
            email = gerar_email(d["titulo"], dominio)

            status = "PRONTO" if dominio_mx and score >= SCORE_MINIMO else "REVISAR"

            dados_finais.append({
                "Empresa": empresa,
                "Telefone_Matriz": telefone,
                "Dominio": dominio,
                "Dominio_Tem_MX": dominio_mx,
                "Decisor": d["titulo"],
                "Score": score,
                "Email_Previsto": email,
                "Status": status,
                "LinkedIn": d["linkedin"]
            })

    df_output = pd.DataFrame(dados_finais)
    df_output.to_csv("output.csv", index=False)

    print("Pipeline finalizado.")


if __name__ == "__main__":
    run_pipeline()

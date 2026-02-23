import os
import io
import re
import unicodedata
import pandas as pd
import requests
import dns.resolver
from bs4 import BeautifulSoup
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import StreamingResponse, JSONResponse

app = FastAPI()
SERPAPI_KEY    = os.getenv("SERPAPI_KEY")
ZEROBOUNCE_KEY = os.getenv("ZEROBOUNCE_KEY")


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


def validar_mx(dominio):
    if not dominio:
        return False
    try:
        dns.resolver.resolve(dominio, "MX")
        return True
    except Exception:
        return False


def nome_simples(nome_empresa):
    ignorar = {
        "sa", "s/a", "ltda", "ltda.", "s.a", "s.a.", "do", "de", "da", "dos", "das",
        "brasil", "brasileira", "grupo", "cia", "companhia", "industria",
        "instituto", "hospital", "clinica", "centro", "fundacao", "associacao",
        "cooperativa", "servicos", "comercio", "solucoes", "tecnologia",
        "nacional", "internacional"
    }
    partes = limpar_texto(nome_empresa).lower().split()
    principais = [p for p in partes if p not in ignorar and len(p) > 3]
    return principais[0] if principais else partes[0] if partes else ""


def nome_para_busca(nome_fantasia, empresa):
    if nome_fantasia and len(nome_fantasia.split()) >= 2:
        return nome_fantasia
    return empresa


def eh_nome_pessoa(nome):
    if not nome:
        return False
    nao_pessoa = [
        "grupo", "assessoria", "consultoria", "agencia", "gestao",
        "solucoes", "servicos", "comercial", "marketing", "comunicacao",
        "holding", "organicos", "associacao", "cooperativa", "industria",
        "empresa", "companhia", "ltda", "s/a"
    ]
    if any(p in nome.lower() for p in nao_pessoa):
        return False
    partes = nome.split()
    if len(partes) < 2:
        return False
    # rejeita se primeira ou ultima palavra for muito curta
    if len(partes[0]) < 3 or len(partes[-1]) < 3:
        return False
    return True


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


def buscar_dados_cnpj(cnpj):
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
            endereco = (
                f"{d.get('logradouro','')}, {d.get('numero','')} - "
                f"{d.get('bairro','')}, {d.get('municipio','')}/{d.get('uf','')} - "
                f"CEP {d.get('cep','')}"
            )
            nome_fantasia = (d.get("nome_fantasia", "") or "").strip()
            return telefone.strip(), endereco.strip(), d.get("uf", ""), nome_fantasia
    except Exception:
        pass
    return "", "", "", ""


def buscar_linkedin_e_dominio(nome_busca, cnpj):
    ignorar_dominio = [
        "linkedin", "facebook", "instagram", "receitafederal",
        "cnpj.info", "empresas.net", "econodata", "jusbrasil",
        "tabelasalarios", "wikipedia", "google", "brasilapi",
        "glassdoor", "indeed", "catho", "infojobs", "mercadolivre",
        "leadiq", "apollo", "hunter", "zoominfo", "reclameaqui",
        "valor.com", "exame.com", "infomoney", "serasaexperian",
        "a16z.com", "hibrazilmarket", "onlineempresas", "gov.br"
    ]

    linkedin_empresa = None
    dominio_oficial  = None
    chave = nome_simples(nome_busca)

    # ETAPA 1: ancora pelo CNPJ
    cnpj_limpo = re.sub(r"\D", "", str(cnpj)).zfill(14)
    for r in serpapi_search(f"{cnpj_limpo} site oficial linkedin"):
        link = r.get("link", "")
        if not linkedin_empresa and "linkedin.com/company" in link:
            linkedin_empresa = link
        if not dominio_oficial and link:
            if not any(i in link.lower() for i in ignorar_dominio):
                match = re.search(r"https?://(?:www\.)?([^/]+)", link)
                if match:
                    dominio_oficial = match.group(1)
        if linkedin_empresa and dominio_oficial:
            break

    # ETAPA 2: fallback por nome
    if not linkedin_empresa or not dominio_oficial:
        palavras_nome = [
            p for p in limpar_texto(nome_busca).lower().split()
            if len(p) > 4 and p not in {"ltda", "brasil", "grupo", "instituto", "hospital", "clinica", "servicos"}
        ]
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


# ---------------------------------------------------------------------------
# CARGOS ALVO para patrocinio cultural
# ---------------------------------------------------------------------------
CARGOS_ALVO = [
    "esg", "sustentabilidade", "responsabilidade social", "relacoes institucionais",
    "relações institucionais", "impacto", "patrocinio", "patrocínio",
    "comunicacao", "comunicação", "marketing", "institucional",
    "diretor", "diretora", "head", "gerente", "presidente", "ceo", "socio"
]


def extrair_nome_cargo_do_texto(texto, nome_empresa):
    """
    Tenta extrair pares (nome, cargo) de um bloco de texto livre.
    Procura padroes como 'Nome Sobrenome, Cargo' ou 'Cargo: Nome Sobrenome'.
    """
    texto_limpo = limpar_texto(texto)
    palavras_empresa = [p for p in limpar_texto(nome_empresa).lower().split() if len(p) > 4]

    # padrao: "Nome Sobrenome, cargo" ou "Nome Sobrenome - cargo"
    padrao = re.findall(
        r'([A-Z][a-z]+ (?:[A-Z][a-z]+ ){0,2}[A-Z][a-z]+)[,\-–]\s*([^\.\n]{5,60})',
        texto_limpo
    )

    for nome, cargo in padrao:
        nome = nome.strip()
        cargo_lower = cargo.lower()
        if not eh_nome_pessoa(nome):
            continue
        if any(c in cargo_lower for c in CARGOS_ALVO):
            return nome, cargo.strip()

    return None, None


def buscar_decisor_via_noticias(nome_busca):
    """
    Busca noticias/press releases que mencionem a empresa e cargos relevantes.
    Extrai nome e cargo do snippet.
    """
    query = (
        f'"{nome_busca}" patrocinio OR "lei rouanet" OR "relacoes institucionais" '
        f'OR "responsabilidade social" OR "impacto comunitario" OR "apoio cultural" '
        f'OR sustentabilidade OR ESG'
    )
    resultados = serpapi_search(query)

    for r in resultados:
        snippet = r.get("snippet", "")
        titulo  = r.get("title", "")
        texto   = titulo + " " + snippet

        nome, cargo = extrair_nome_cargo_do_texto(texto, nome_busca)
        if nome and cargo:
            return nome, cargo, r.get("link", "")

    return None, None, None


def scrape_pagina(url):
    """Faz scrape simples de uma URL e retorna texto limpo."""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, timeout=10, headers=headers)
        if r.status_code != 200:
            return ""
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        return soup.get_text(separator=" ", strip=True)[:5000]
    except Exception:
        return ""


def buscar_decisor_via_site(dominio, nome_busca):
    """
    Tenta paginas comuns de equipe/diretoria no site da empresa.
    Extrai nome e cargo do HTML.
    """
    if not dominio:
        return None, None, None

    paginas_candidatas = [
        f"https://{dominio}/equipe",
        f"https://{dominio}/sobre",
        f"https://{dominio}/quem-somos",
        f"https://{dominio}/diretoria",
        f"https://{dominio}/time",
        f"https://{dominio}/empresa",
        f"https://{dominio}/sobre-nos",
    ]

    for url in paginas_candidatas:
        texto = scrape_pagina(url)
        if not texto:
            continue
        nome, cargo = extrair_nome_cargo_do_texto(texto, nome_busca)
        if nome and cargo:
            return nome, cargo, url

    return None, None, None


def buscar_linkedin_pessoa(nome_decisor, nome_empresa):
    """
    Com o nome da pessoa ja encontrado, busca o perfil LinkedIn diretamente.
    """
    if not nome_decisor:
        return None
    query = f'"{nome_decisor}" "{nome_empresa}" site:linkedin.com/in'
    resultados = serpapi_search(query)
    for r in resultados:
        link = r.get("link", "")
        if "linkedin.com/in" in link:
            # valida que nao esta truncado
            partes = [p for p in link.split("/") if p]
            slug_idx = next((i for i, p in enumerate(partes) if p == "in"), None)
            if slug_idx and slug_idx + 1 < len(partes) and len(partes[slug_idx + 1]) >= 3:
                return link
    return None


def buscar_decisor_completo(nome_busca, dominio):
    """
    Fluxo completo:
    1. Noticias/press releases
    2. Scrape do site
    3. LinkedIn direto com o nome encontrado
    """
    nome, cargo, fonte = None, None, None

    # ETAPA 1: noticias
    nome, cargo, fonte = buscar_decisor_via_noticias(nome_busca)

    # ETAPA 2: scrape do site (se noticias nao acharam)
    if not nome:
        nome, cargo, fonte = buscar_decisor_via_site(dominio, nome_busca)

    # ETAPA 3: linkedin da pessoa encontrada
    linkedin_pessoa = None
    if nome:
        linkedin_pessoa = buscar_linkedin_pessoa(nome, nome_busca)

    return nome, cargo, linkedin_pessoa


def gerar_email_provavel(nome_decisor, dominio):
    if not nome_decisor or not dominio:
        return None
    partes = limpar_texto(nome_decisor).lower().split()
    if len(partes) < 2 or len(partes[0]) < 3 or len(partes[-1]) < 3:
        return None
    return f"{partes[0]}.{partes[-1]}@{dominio}"


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

        telefone, endereco, uf, nome_fantasia = buscar_dados_cnpj(cnpj)
        nome_busca = nome_para_busca(nome_fantasia, empresa)

        linkedin_empresa, dominio_oficial = buscar_linkedin_e_dominio(nome_busca, cnpj)
        dominio_tem_mx = validar_mx(dominio_oficial)

        nome_decisor, cargo_decisor, linkedin_decisor = buscar_decisor_completo(
            nome_busca, dominio_oficial
        )

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
```

Tem uma dependência nova — o `beautifulsoup4` para o scrape. Verifica se o `requirements.txt` no GitHub tem essa linha:
```
beautifulsoup4==4.12.3

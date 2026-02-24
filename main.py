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


def validar_mx(dominio):
    if not dominio:
        return False
    try:
        dns.resolver.resolve(dominio, "MX")
        return True
    except Exception:
        return False


def nome_simples(nome_empresa):
    """Remove sufixos legais e stopwords; retorna tokens relevantes concatenados."""
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


def nome_para_busca(nome_fantasia, empresa):
    if nome_fantasia and len(nome_fantasia.strip()) > 3:
        return nome_fantasia.strip()
    return nome_simples(empresa) or empresa


def _tokens_empresa(nome):
    """Tokens de 4+ chars do nome para validação de relevância."""
    return [t for t in nome_simples(nome).lower().split() if len(t) >= 4]


# ---------------------------------------------------------------------------
# Blacklists de domínio
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
}

EMAILS_GENERICOS = {
    "gmail.com", "hotmail.com", "yahoo.com", "yahoo.com.br",
    "outlook.com", "live.com", "uol.com.br", "bol.com.br",
    "terra.com.br", "ig.com.br", "globo.com",
}


def dominio_bloqueado(dominio):
    if not dominio:
        return True
    d = dominio.lower().lstrip("www.")
    if d in DOMINIOS_BLOQUEADOS:
        return True
    return any(d.endswith("." + b) for b in DOMINIOS_BLOQUEADOS)


def dominio_generico(dominio):
    if not dominio:
        return True
    return dominio.lower().lstrip("www.") in EMAILS_GENERICOS


def _extrair_dominio_url(url):
    m = re.search(r"https?://(?:www\.)?([^/?#]+)", url or "")
    return m.group(1).lower() if m else ""


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


def buscar_dados_cnpj(cnpj, razao_social=""):
    cnpj_limpo = re.sub(r"\D", "", str(cnpj)).zfill(14)
    try:
        r = requests.get(
            f"https://brasilapi.com.br/api/cnpj/v1/{cnpj_limpo}",
            timeout=10
        )
        if r.status_code != 200:
            return "", "", "", "", "", []
        d = r.json()
        t = re.sub(r"\D", "", d.get("ddd_telefone_1", "") or "")
        telefone = f"({t[:2]}) {t[2:]}" if len(t) >= 10 else t
        partes_end = [
            f"{d.get('logradouro','')}, {d.get('numero','')}".strip(", "),
            d.get("bairro", "") or "",
            f"{d.get('municipio','')}/{d.get('uf','')} - CEP {d.get('cep','')}",
        ]
        endereco = " - ".join(p for p in partes_end if p.strip(" -/"))
        nome_fantasia = limpar_texto(d.get("nome_fantasia") or "")
        email_empresa = limpar_texto(d.get("email") or "").lower()
        pessoas_qsa   = extrair_pessoas_qsa(d.get("qsa", []))
        return telefone.strip(), endereco.strip(), d.get("uf", ""), nome_fantasia, email_empresa, pessoas_qsa
    except Exception:
        return "", "", "", "", "", []


# ---------------------------------------------------------------------------
# CAMADA 1 — Domínio oficial
# ---------------------------------------------------------------------------

def resolver_dominio(nome_busca, cnpj, email_empresa):
    """
    1. Email BrasilAPI (mais confiável)
    2. SerpAPI por nome com validação por tokens
    3. SerpAPI por CNPJ com validação por tokens
    """
    cnpj_limpo = re.sub(r"\D", "", str(cnpj)).zfill(14)
    tokens = _tokens_empresa(nome_busca)

    # 1. Email BrasilAPI
    if email_empresa and "@" in email_empresa:
        d = email_empresa.split("@")[-1].strip()
        if not dominio_generico(d) and not dominio_bloqueado(d):
            return d, "brasilapi_email"

    # 2. SerpAPI por nome
    for query in [
        f'"{nome_busca}" site oficial',
        f'"{nome_busca}" contato',
    ]:
        for r in serpapi_search(query, num=5):
            link = r.get("link", "")
            d = _extrair_dominio_url(link)
            if not d or dominio_bloqueado(d):
                continue
            if tokens and any(t in d for t in tokens):
                return d, "serpapi_nome"

    # 3. SerpAPI por CNPJ
    for r in serpapi_search(f"{cnpj_limpo} site oficial", num=5):
        link = r.get("link", "")
        d = _extrair_dominio_url(link)
        if not d or dominio_bloqueado(d):
            continue
        if tokens and any(t in d for t in tokens):
            return d, "serpapi_cnpj"

    return None, None


# ---------------------------------------------------------------------------
# CAMADA 2 — LinkedIn de empresa
# ---------------------------------------------------------------------------

def buscar_linkedin_empresa(nome_busca, cnpj):
    """
    Aceita apenas linkedin.com/company com tokens do nome na URL ou contexto.
    """
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
            contexto = (r.get("title", "") + " " + r.get("snippet", "")).lower()
            if tokens and any(t in link.lower() or t in contexto for t in tokens):
                return link

    # Fallback: CNPJ direto
    for r in serpapi_search(f"{cnpj_limpo} site:linkedin.com/company", num=3):
        link = r.get("link", "")
        if "linkedin.com/company" in link:
            return link

    return None


# ---------------------------------------------------------------------------
# CAMADA 3 — Decisor via QSA + LinkedIn
# ---------------------------------------------------------------------------

def _extrair_cargo_do_titulo(titulo, nome_pessoa):
    partes = titulo.split(" - ")
    if len(partes) >= 2:
        cargo = partes[1].strip()
        cargo = re.sub(r"\s*\|\s*LinkedIn.*", "", cargo).strip()
        primeiro = limpar_texto(nome_pessoa).lower().split()[0]
        if cargo and primeiro not in cargo.lower():
            return cargo
    return ""


def buscar_decisor_via_qsa(pessoas_qsa, nome_busca):
    """
    QSA-first: para cada pessoa (top 3), busca LinkedIn em cascata:
    1. Nome completo + empresa (query exata)
    2. Nome curto (primeiro+último) + empresa
    3. Nome curto + empresa simples (sem aspas, mais amplo)
    """
    if not pessoas_qsa:
        return None, None, None

    nome_busca_simples = nome_simples(nome_busca)

    for pessoa in pessoas_qsa[:3]:
        nome_completo = pessoa["nome"]
        qualificacao  = pessoa["qualificacao"]

        partes   = limpar_texto(nome_completo).lower().split()
        primeiro = partes[0] if partes else ""
        ultimo   = partes[-1] if len(partes) > 1 else ""
        nome_curto = f"{partes[0].title()} {partes[-1].title()}" if len(partes) > 1 else nome_completo

        queries = [
            f'"{nome_completo}" "{nome_busca}" site:linkedin.com/in',
            f'"{nome_curto}" "{nome_busca}" site:linkedin.com/in',
            f'"{nome_curto}" "{nome_busca_simples}" linkedin',
        ]

        for query in queries:
            for r in serpapi_search(query, num=5):
                link = r.get("link", "")
                if "linkedin.com/in" not in link:
                    continue
                titulo   = r.get("title", "")
                contexto = (titulo + " " + r.get("snippet", "")).lower()
                if primeiro in contexto or ultimo in contexto:
                    cargo = _extrair_cargo_do_titulo(titulo, nome_completo) or qualificacao
                    return nome_completo, cargo, link

    return None, None, None


# ---------------------------------------------------------------------------
# CAMADA 4 — Hunter.io
# ---------------------------------------------------------------------------

CARGOS_ALVO_HUNTER = {
    "ceo", "cfo", "coo", "diretor", "director", "vp", "presidente",
    "gerente", "manager", "head", "esg", "sustentabilidade",
    "marketing", "comunicacao", "comunicação", "rh", "recursos humanos",
    "pessoas", "social", "financeiro",
}


def buscar_decisor_hunter(dominio):
    if not HUNTER_KEY or not dominio:
        return None, None, None, None
    try:
        r = requests.get(
            "https://api.hunter.io/v2/domain-search",
            params={"domain": dominio, "api_key": HUNTER_KEY, "limit": 10},
            timeout=10,
        )
        emails = r.json().get("data", {}).get("emails", [])
        for e in emails:
            cargo = (e.get("position") or "").lower()
            if any(c in cargo for c in CARGOS_ALVO_HUNTER):
                nome = f"{e.get('first_name','')} {e.get('last_name','')}".strip()
                return nome or None, e.get("position"), e.get("linkedin") or None, e.get("value") or None
        if emails:
            e = emails[0]
            nome = f"{e.get('first_name','')} {e.get('last_name','')}".strip()
            return nome or None, e.get("position"), e.get("linkedin") or None, e.get("value") or None
    except Exception:
        pass
    return None, None, None, None


# ---------------------------------------------------------------------------
# CAMADA 5 — SerpAPI genérico (último recurso)
# ---------------------------------------------------------------------------

def buscar_decisor_serpapi_generico(nome_busca):
    TERMOS_PJ = {
        "ltda", "s/a", "holding", "group", "grupo", "gmbh", "corp",
        "engenharia", "tecnologia", "construtora", "servicos",
    }
    for query in [
        f"{nome_busca} diretor linkedin",
        f"{nome_busca} CEO linkedin",
    ]:
        for r in serpapi_search(query, num=5):
            link = r.get("link", "")
            if "linkedin.com/in" not in link:
                continue
            titulo = r.get("title", "")
            partes = titulo.split(" - ")
            if len(partes) < 2:
                continue
            candidato = partes[0].strip()
            if any(t in candidato.lower() for t in TERMOS_PJ):
                continue
            if len(candidato.split()) < 2 or len(candidato) < 6:
                continue
            cargo = _extrair_cargo_do_titulo(titulo, candidato)
            return candidato, cargo or "", link
    return None, None, None


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def construir_email(nome_decisor, dominio):
    if not nome_decisor or not dominio:
        return ""
    partes = limpar_texto(nome_decisor).lower().split()
    if len(partes) == 1:
        return f"{partes[0]}@{dominio}"
    return f"{partes[0]}.{partes[-1]}@{dominio}"


def verificar_email(email):
    if not ZEROBOUNCE_KEY or not email:
        return "nao_verificado"
    try:
        r = requests.get(
            "https://api.zerobounce.net/v2/validate",
            params={"api_key": ZEROBOUNCE_KEY, "email": email},
            timeout=10,
        )
        return r.json().get("status", "unknown")
    except Exception:
        return "erro_verificacao"


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
        telefone, endereco, uf, nome_fantasia, email_empresa, pessoas_qsa = \
            buscar_dados_cnpj(cnpj, empresa)

        nome_busca = nome_para_busca(nome_fantasia, empresa)

        # CAMADA 1: Domínio (email BrasilAPI > SerpAPI validado por tokens)
        dominio_oficial, _ = resolver_dominio(nome_busca, cnpj, email_empresa)
        dominio_tem_mx      = validar_mx(dominio_oficial)

        # CAMADA 2: LinkedIn empresa (validado por tokens do nome)
        linkedin_empresa = buscar_linkedin_empresa(nome_busca, cnpj)

        # CAMADA 3: Decisor via QSA + LinkedIn
        nome_decisor, cargo_decisor, linkedin_decisor = \
            buscar_decisor_via_qsa(pessoas_qsa, nome_busca)
        fonte_decisor = "qsa+linkedin" if nome_decisor else ""

        # CAMADA 4: Hunter (só com domínio confiável)
        email_hunter = None
        if dominio_oficial and not dominio_generico(dominio_oficial):
            if not nome_decisor:
                nome_decisor, cargo_decisor, linkedin_decisor, email_hunter = \
                    buscar_decisor_hunter(dominio_oficial)
                if nome_decisor:
                    fonte_decisor = "hunter"
            else:
                _, _, _, email_hunter = buscar_decisor_hunter(dominio_oficial)

        # CAMADA 5: SerpAPI genérico (fallback final)
        if not nome_decisor:
            nome_decisor, cargo_decisor, linkedin_decisor = \
                buscar_decisor_serpapi_generico(nome_busca)
            if nome_decisor:
                fonte_decisor = "serpapi"

        # Email final
        if email_hunter:
            email_previsto = email_hunter
            email_status   = "hunter_verified"
        else:
            email_previsto = construir_email(nome_decisor, dominio_oficial)
            if email_previsto and dominio_tem_mx:
                email_status = verificar_email(email_previsto)
            elif email_previsto:
                email_status = "dominio_sem_mx"
            else:
                email_previsto = ""
                email_status   = "sem email"

        qsa_referencia = " | ".join(
            f"{p['nome']} ({p['qualificacao']})" for p in pessoas_qsa[:2]
        ) if pessoas_qsa else ""

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
            "qsa_referencia":   qsa_referencia,
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

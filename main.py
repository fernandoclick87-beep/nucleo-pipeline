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
# Utilitários
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
    """Extrai palavra-chave principal para buscas menos específicas."""
    ignorar = {
        "sa", "s/a", "ltda", "ltda.", "s.a", "s.a.", "do", "de", "da", "dos", "das",
        "brasil", "brasileira", "grupo", "cia", "companhia", "industria",
        "instituto", "hospital", "clinica", "centro", "fundacao", "associacao",
        "cooperativa", "servicos", "comercio", "solucoes", "tecnologia",
        "nacional", "internacional", "produtos", "quimicos", "sistemas",
        "eletronicos", "seguranca", "industriais"
    }
    partes    = limpar_texto(nome_empresa).lower().split()
    principais = [p for p in partes if p not in ignorar and len(p) > 3]
    return principais[0] if principais else (partes[0] if partes else "")


def nome_para_busca(nome_fantasia, empresa):
    if nome_fantasia and len(nome_fantasia) > 4:
        return nome_fantasia
    return empresa


def eh_nome_pessoa(nome):
    """Verifica se uma string parece ser nome humano (não nome de empresa)."""
    if not nome:
        return False
    palavras_empresa = [
        "grupo", "assessoria", "consultoria", "agencia", "gestao",
        "solucoes", "servicos", "comercial", "marketing", "comunicacao",
        "holding", "organicos", "associacao", "cooperativa", "industria",
        "empresa", "companhia", "ltda", "s/a", "manteiga", "laticinios",
        "aviacao", "quimica", "pier", "cooperativa", "gmbh", "corp",
        "international", "engenharia", "tecnologia", "construtora"
    ]
    if any(p in nome.lower() for p in palavras_empresa):
        return False
    partes = nome.split()
    if len(partes) < 2:
        return False
    if len(partes[0]) < 3 or len(partes[-1]) < 3:
        return False
    return True


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
# BrasilAPI – CNPJ + QSA + email
# ---------------------------------------------------------------------------

PRIORIDADE_QSA = {
    "presidente":            0,
    "diretor presidente":    1,
    "diretor":               2,
    "administrador":         3,
    "socio-administrador":   4,
    "socio administrador":   4,
    "gerente":               5,
    "socio":                 6,
}

def _prioridade_qualificacao(qual):
    qual_lower = limpar_texto(qual).lower()
    for chave, prio in PRIORIDADE_QSA.items():
        if chave in qual_lower:
            return prio
    return 99


def extrair_pessoas_qsa(qsa_list):
    """
    Recebe a lista qsa da BrasilAPI e retorna somente as PESSOAS FÍSICAS,
    ordenadas por prioridade de cargo (presidente > diretor > administrador ...).
    Retorna lista de dicts: {'nome': ..., 'qualificacao': ...}
    """
    pessoas = []
    for item in (qsa_list or []):
        nome  = (item.get("nome_socio") or "").strip()
        qual  = (item.get("qualificacao_socio") or "").strip()
        cpf   = re.sub(r"\D", "", item.get("cnpj_cpf_do_socio") or "")

        # Ignora entidades: CNPJ (14 dígitos) ou nome claramente não-humano
        if len(cpf) == 14:
            continue
        if not eh_nome_pessoa(nome):
            continue

        pessoas.append({
            "nome":         nome.title(),
            "qualificacao": qual,
            "prioridade":   _prioridade_qualificacao(qual),
        })

    pessoas.sort(key=lambda x: x["prioridade"])
    return pessoas


def buscar_dados_cnpj(cnpj, razao_social=""):
    """
    Retorna: (telefone, endereco, uf, nome_fantasia, email_empresa, pessoas_qsa)
    pessoas_qsa = lista de {'nome', 'qualificacao', 'prioridade'}
    """
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

            nome_fantasia  = (d.get("nome_fantasia", "") or "").strip()
            email_empresa  = (d.get("email", "") or "").strip().lower()
            pessoas_qsa    = extrair_pessoas_qsa(d.get("qsa", []))

            return (
                telefone.strip(),
                endereco.strip(),
                d.get("uf", ""),
                nome_fantasia,
                email_empresa,
                pessoas_qsa,
            )
    except Exception:
        pass
    return "", "", "", "", "", []


# ---------------------------------------------------------------------------
# LinkedIn + domínio via SerpAPI
# ---------------------------------------------------------------------------

IGNORAR_DOMINIO = {
    "linkedin", "facebook", "instagram", "receitafederal",
    "cnpj.info", "empresas.net", "econodata", "jusbrasil",
    "tabelasalarios", "wikipedia", "google", "brasilapi",
    "glassdoor", "indeed", "catho", "infojobs", "mercadolivre",
    "leadiq", "apollo", "hunter.io", "zoominfo", "reclameaqui",
    "valor.com", "exame.com", "infomoney", "serasaexperian",
    "a16z.com", "hibrazilmarket", "onlineempresas", "gov.br",
    "oecd.org", "compreaviacao.com.br", "obahortifruti.com.br",
    "kaeferbrasil.com.br", "casadosdados", "cnpj.biz", "consultascnpj",
    "situacaocadastral", "numerodozap", "linkana", "cnpjbrasil",
    "portaldaindustria", "look2agro", "mitel.com", "econodata",
    "consultasocio", "transparencia.cc", "rocketreach", "contactout",
    "finalscout", "zoominfo", "cnpj.services", "informecadastral",
    "cadastroempresa", "diariocidade", "cnpjagora"
}


def buscar_linkedin_e_dominio(nome_busca, cnpj):
    """Busca LinkedIn de empresa e domínio oficial."""
    linkedin_empresa = None
    dominio_oficial  = None
    chave = nome_simples(nome_busca)
    cnpj_limpo = re.sub(r"\D", "", str(cnpj)).zfill(14)

    # Busca 1: pelo CNPJ (mais específica, menos ruído)
    for r in serpapi_search(f"{cnpj_limpo} linkedin site oficial", num=5):
        link = r.get("link", "")
        if not linkedin_empresa and "linkedin.com/company" in link:
            linkedin_empresa = link
        if not dominio_oficial and link:
            if not any(i in link.lower() for i in IGNORAR_DOMINIO):
                m = re.search(r"https?://(?:www\.)?([^/]+)", link)
                if m:
                    dominio_oficial = m.group(1)
        if linkedin_empresa and dominio_oficial:
            break

    # Busca 2: pelo nome (fallback)
    if not linkedin_empresa or not dominio_oficial:
        for r in serpapi_search(f"{nome_busca} site oficial linkedin empresa", num=5):
            link     = r.get("link", "")
            contexto = (r.get("title", "") + " " + r.get("snippet", "")).lower()
            if not linkedin_empresa and "linkedin.com/company" in link:
                if chave in link.lower() or chave in contexto:
                    linkedin_empresa = link
            if not dominio_oficial and link:
                if not any(i in link.lower() for i in IGNORAR_DOMINIO):
                    m = re.search(r"https?://(?:www\.)?([^/]+)", link)
                    if m:
                        dominio_oficial = m.group(1)
            if linkedin_empresa and dominio_oficial:
                break

    return linkedin_empresa, dominio_oficial


# ---------------------------------------------------------------------------
# QSA-first: busca LinkedIn de pessoa específica
# ---------------------------------------------------------------------------

def _extrair_dominio_da_url(url):
    m = re.search(r"https?://(?:www\.)?([^/]+)", url or "")
    return m.group(1) if m else ""


def buscar_decisor_via_qsa(pessoas_qsa, nome_busca):
    """
    Para cada pessoa do QSA, busca LinkedIn com query específica:
    "{nome_pessoa}" "{empresa_simples}" linkedin

    Retorna (nome, cargo, linkedin_url) do primeiro match válido,
    ou (None, None, None) se não encontrar.
    """
    if not pessoas_qsa:
        return None, None, None

    empresa_simples = nome_simples(nome_busca)

    for pessoa in pessoas_qsa[:3]:  # testa os 3 mais importantes
        nome_pessoa = pessoa["nome"]
        qualificacao = pessoa["qualificacao"]

        # Query cirúrgica: nome exato + empresa
        query = f'"{nome_pessoa}" "{nome_busca}" site:linkedin.com/in'
        resultados = serpapi_search(query, num=3)

        # Fallback: nome + empresa simples sem site:
        if not any("linkedin.com/in" in r.get("link", "") for r in resultados):
            query = f'"{nome_pessoa}" "{empresa_simples}" linkedin'
            resultados = serpapi_search(query, num=5)

        for r in resultados:
            link = r.get("link", "")
            if "linkedin.com/in" not in link:
                continue
            titulo = r.get("title", "")
            # Validação básica: nome ou empresa devem aparecer no título/snippet
            contexto = (titulo + " " + r.get("snippet", "")).lower()
            nome_lower = limpar_texto(nome_pessoa).lower()
            primeiro_nome = nome_lower.split()[0] if nome_lower else ""
            sobrenome     = nome_lower.split()[-1] if len(nome_lower.split()) > 1 else ""

            # Aceita se primeiro nome OU sobrenome aparece no contexto
            if primeiro_nome in contexto or sobrenome in contexto:
                cargo_encontrado = _extrair_cargo_do_titulo(titulo, nome_pessoa) or qualificacao
                return nome_pessoa, cargo_encontrado, link

    return None, None, None


def _extrair_cargo_do_titulo(titulo, nome_pessoa):
    """
    Títulos LinkedIn têm formato: "Nome Sobrenome - Cargo - Empresa | LinkedIn"
    Tenta extrair o cargo.
    """
    nome_lower = limpar_texto(nome_pessoa).lower().split()[0]
    partes = titulo.split(" - ")
    if len(partes) >= 2:
        # partes[0] = nome, partes[1] = cargo, partes[2] = empresa (às vezes)
        candidato_cargo = partes[1].strip() if len(partes) >= 2 else ""
        if candidato_cargo and nome_lower not in candidato_cargo.lower():
            # Remove " | LinkedIn" se presente
            candidato_cargo = re.sub(r"\s*\|\s*LinkedIn.*", "", candidato_cargo).strip()
            return candidato_cargo
    return ""


# ---------------------------------------------------------------------------
# Hunter.io
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


def buscar_decisor_hunter(dominio):
    if not HUNTER_KEY or not dominio:
        return None, None, None, None
    try:
        r = requests.get("https://api.hunter.io/v2/domain-search", params={
            "domain":   dominio,
            "api_key":  HUNTER_KEY,
            "limit":    10,
            "seniority": "executive,director,manager"
        }, timeout=15)
        if r.status_code != 200:
            return None, None, None, None
        emails = r.json().get("data", {}).get("emails", [])
        if not emails:
            return None, None, None, None

        for prioridade_lista in [CARGOS_PRIORITARIOS, CARGOS_EXECUTIVOS, None]:
            for e in emails:
                cargo = (e.get("position") or "").lower()
                if prioridade_lista and not any(c in cargo for c in prioridade_lista):
                    continue
                if e.get("first_name") and e.get("last_name"):
                    return (
                        f"{e.get('first_name','')} {e.get('last_name','')}".strip(),
                        e.get("position", ""),
                        e.get("linkedin", ""),
                        e.get("value", "")
                    )
    except Exception:
        pass
    return None, None, None, None


# ---------------------------------------------------------------------------
# SerpAPI fallback (busca genérica por empresa – último recurso)
# ---------------------------------------------------------------------------

def buscar_decisor_serpapi_generico(nome_busca):
    """Fallback: busca genérica por cargo+empresa quando QSA e Hunter falham."""
    chave = nome_simples(nome_busca)
    if not chave:
        return None, None, None

    resultados = serpapi_search(f"{chave} linkedin", num=7)

    for prioridade in [CARGOS_PRIORITARIOS, CARGOS_EXECUTIVOS, None]:
        for r in resultados:
            link   = r.get("link", "")
            titulo = r.get("title", "")
            if "linkedin.com/in" not in link:
                continue
            if prioridade and not any(c in titulo.lower() for c in prioridade):
                continue
            partes = titulo.split(" - ")
            nome   = partes[0].strip()
            cargo  = partes[1].strip() if len(partes) >= 2 else ""
            # Remove " | LinkedIn" do cargo
            cargo  = re.sub(r"\s*\|\s*LinkedIn.*", "", cargo).strip()
            if eh_nome_pessoa(nome):
                return nome, cargo, link

    return None, None, None


# ---------------------------------------------------------------------------
# Verificação de email
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


def construir_email(nome_decisor, dominio, email_empresa_brasilapi):
    """
    Tenta construir email do decisor.
    1. Se temos email da empresa via BrasilAPI → extrai domínio
    2. Usa o dominio_oficial para email nome.sobrenome@dominio
    """
    dominio_email = dominio
    if email_empresa_brasilapi:
        partes_email = email_empresa_brasilapi.split("@")
        if len(partes_email) == 2:
            dominio_email = partes_email[1]

    if nome_decisor and dominio_email:
        partes = limpar_texto(nome_decisor).lower().split()
        if len(partes) >= 2:
            return f"{partes[0]}.{partes[-1]}@{dominio_email}"
    return ""


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

        # 1. BrasilAPI: dados cadastrais + QSA + email empresa
        telefone, endereco, uf, nome_fantasia, email_empresa, pessoas_qsa = \
            buscar_dados_cnpj(cnpj, empresa)

        nome_busca = nome_para_busca(nome_fantasia, empresa)

        # 2. LinkedIn de empresa + domínio oficial
        linkedin_empresa, dominio_oficial = buscar_linkedin_e_dominio(nome_busca, cnpj)
        dominio_tem_mx = validar_mx(dominio_oficial)

        # 3. Decisor: QSA-first (melhor qualidade)
        nome_decisor, cargo_decisor, linkedin_decisor = \
            buscar_decisor_via_qsa(pessoas_qsa, nome_busca)
        fonte_decisor = "qsa+linkedin" if nome_decisor else ""

        # 4. Decisor: Hunter.io (bom para email + cargo real)
        email_hunter = None
        if not nome_decisor:
            nome_decisor, cargo_decisor, linkedin_decisor, email_hunter = \
                buscar_decisor_hunter(dominio_oficial)
            if nome_decisor:
                fonte_decisor = "hunter"
        else:
            # Mesmo achando via QSA, busca email via Hunter se possível
            _, _, _, email_hunter = buscar_decisor_hunter(dominio_oficial)

        # 5. Decisor: SerpAPI genérico (fallback)
        if not nome_decisor:
            nome_decisor, cargo_decisor, linkedin_decisor = \
                buscar_decisor_serpapi_generico(nome_busca)
            fonte_decisor = "serpapi" if nome_decisor else ""

        # 6. Email
        if email_hunter:
            email_previsto = email_hunter
            email_status   = "hunter_verified"
        else:
            email_previsto = construir_email(nome_decisor, dominio_oficial, email_empresa)
            if email_previsto and dominio_tem_mx:
                email_status = verificar_email(email_previsto)
            elif email_previsto:
                email_status = "dominio_sem_mx"
            else:
                email_previsto = ""
                email_status   = "sem email"

        # QSA como referência – primeiro nome para auditoria
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

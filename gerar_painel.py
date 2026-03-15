import feedparser
import os
import time
from datetime import datetime, timezone, timedelta
import re
import html as html_module
import urllib.request
import urllib.error
import http.cookiejar
import concurrent.futures
import json
import gzip
import zlib
import logging

try:
    import brotli
    BROTLI_DISPONIVEL = True
except ImportError:
    BROTLI_DISPONIVEL = False

# ─────────────────────────────────────────────
# CONFIGURAÇÃO DE LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# CONFIGURAÇÃO
# ─────────────────────────────────────────────

fontes_rss = {
    "Londrina": [
        ("Blog da Prefeitura", "https://blog.londrina.pr.gov.br/?feed=rss2"),
        ("CBN Londrina",       "https://news.google.com/rss/search?q=site:cbnlondrina.com.br&hl=pt-BR&gl=BR&ceid=BR:pt"),
        ("Paiquerê",           "https://www.paiquere.com.br/feed/")
    ],
    "Brasil": [
        ("Agência Brasil",      "https://agenciabrasil.ebc.com.br/rss/ultimasnoticias/feed.xml"),
        ("Agência Lupa",        "https://lupa.uol.com.br/feed/"),
        ("Aos Fatos",           "https://aosfatos.org/noticias/feed/"),
        ("CNN Brasil",          "https://www.cnnbrasil.com.br/feed/"),
        ("ICL Notícias",        "https://iclnoticias.com.br/feed/"),
        ("O Globo",             "https://pox.globo.com/rss/oglobo/"),
        ("Revista Piauí",       "https://piaui.uol.com.br/feed/"),
        ("The Intercept Brasil","https://www.intercept.com.br/feed/"),
        ("Valor Econômico",     "https://pox.globo.com/rss/valor")
    ],
    "Mundo": [
        ("Al Jazeera",       "https://www.aljazeera.com/xml/rss/all.xml"),
        ("Associated Press", "https://news.google.com/rss/search?q=site:apnews.com&hl=en-US&gl=US&ceid=US:en"),
        ("Financial Times",  "https://www.ft.com/world?format=rss"),
        ("NPR",              "https://feeds.npr.org/1001/rss.xml"),
        ("The New York Times","https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"),
        ("The Verge",        "https://www.theverge.com/rss/index.xml")
    ]
}

FONTES_SEM_IMAGEM_PROPRIA = {"Paiquerê", "CNN Brasil", "Revista Piauí", "CBN Londrina"}

FALLBACK_IMAGENS = {
    "Paiquerê":         "https://www.paiquere.com.br/wp-content/uploads/2024/02/logo_paiquere.png#163",
    "CNN Brasil":       "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcTq-9elwONA0jhgj5PC85UOewbgTKfMcu8V_w&s",
    "Al Jazeera":       "https://m.media-amazon.com/images/I/31TqBcQUlcL.png",
    "Financial Times":  "https://pbs.twimg.com/profile_images/931161479398686721/FI3te2Sw_400x400.jpg",
    "Associated Press": "https://upload.wikimedia.org/wikipedia/commons/thumb/0/0c/Associated_Press_logo_2012.svg/1280px-Associated_Press_logo_2012.svg.png",
    "Revista Piauí":    "https://piaui.uol.com.br/wp-content/uploads/2023/03/Logo-Piaui-qualidade-boa-256.png",
    "CBN Londrina":     "https://upload.wikimedia.org/wikipedia/commons/0/0a/CBN_Londrina_logo_2019.png",
}

TIMEOUT_REDE   = 10
ITENS_POR_PAG  = 20

# ─────────────────────────────────────────────
# UTILITÁRIOS
# ─────────────────────────────────────────────

def sanitizar_url(url: str) -> str:
    if not url:
        return "#"
    url = url.strip()
    esquema = url.lower().split(":")[0]
    if esquema not in ("http", "https"):
        return "#"
    return url

def pegar_timestamp_e_data(entry):
    timestamp      = 0
    data_formatada = "Data indisponível"
    fuso_br        = timezone(timedelta(hours=-3))

    for campo in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, campo, None)
        if parsed:
            dt_utc         = datetime(*parsed[:6], tzinfo=timezone.utc)
            dt_br          = dt_utc.astimezone(fuso_br)
            timestamp      = dt_br.timestamp()
            data_formatada = dt_br.strftime("%d/%m/%Y às %H:%M")
            break

    return timestamp, data_formatada

def limpar_html_resumo(texto: str) -> str:
    if not texto:
        return ""
    texto_limpo = re.sub(r"<.*?>", "", texto).strip()
    if len(texto_limpo) > 160:
        return texto_limpo[:157] + "..."
    return texto_limpo

def cacador_de_imagens(entry, nome_site: str):
    if nome_site in FONTES_SEM_IMAGEM_PROPRIA:
        return FALLBACK_IMAGENS.get(nome_site)

    if nome_site == "Agência Brasil":
        img_destaque = entry.get("imagem-destaque") or entry.get("imagem_destaque")
        if img_destaque and isinstance(img_destaque, str):
            return sanitizar_url(html_module.unescape(img_destaque))

    for campo in ("media_content", "media_thumbnail"):
        itens = getattr(entry, campo, [])
        if itens and "url" in itens[0]:
            return sanitizar_url(itens[0]["url"])

    for link in getattr(entry, "links", []):
        if link.get("type", "").startswith("image/"):
            return sanitizar_url(link["href"])

    textos = []
    if "summary" in entry:
        textos.append(entry.summary)
    for bloco in entry.get("content", []):
        textos.append(bloco.get("value", ""))

    for texto in textos:
        match = re.search(r'<img[^>]+src=["\']([^"\'>]+)["\']', texto, re.IGNORECASE)
        if match:
            return sanitizar_url(html_module.unescape(match.group(1)))

    return FALLBACK_IMAGENS.get(nome_site)

# ─────────────────────────────────────────────
# BUSCA DE FEEDS
# ─────────────────────────────────────────────

def descomprimir(dados: bytes, encoding: str) -> bytes:
    enc = encoding.lower()
    if enc == "br":
        if BROTLI_DISPONIVEL:
            return brotli.decompress(dados)
    elif enc == "gzip" or (not enc and dados[:2] == b'\x1f\x8b'):
        try:
            return gzip.decompress(dados)
        except gzip.BadGzipFile:
            pass
    elif enc == "deflate":
        try:
            return zlib.decompress(dados)
        except zlib.error:
            try:
                return zlib.decompress(dados, -15)
            except zlib.error:
                pass
    return dados

def fazer_requisicao(opener, url):
    encodings = "gzip, deflate, br" if BROTLI_DISPONIVEL else "gzip, deflate"
    req = urllib.request.Request(url, headers={
        "User-Agent":      (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": encodings,
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Connection":      "keep-alive",
    })
    with opener.open(req, timeout=TIMEOUT_REDE) as response:
        dados    = response.read()
        encoding = response.info().get("Content-Encoding", "")
    return dados, encoding

FONTES_COM_CSRF = set()

def buscar_feed_individual(categoria: str, nome_site: str, url: str) -> list:
    noticias_feed = []
    try:
        jar    = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

        if nome_site in FONTES_COM_CSRF:
            base_url = "/".join(url.split("/")[:3])
            try:
                fazer_requisicao(opener, base_url)
            except Exception as e:
                logger.warning("Falha ao aquecer sessão de '%s': %s", nome_site, e)

        dados, encoding = fazer_requisicao(opener, url)
        dados   = descomprimir(dados, encoding)
        xml_cru = dados.decode("utf-8", errors="ignore")

        if nome_site == "ICL Notícias":
            xml_cru = re.sub(
                r'src=["\']data:image[^"\']*["\']', "", xml_cru, flags=re.IGNORECASE
            )
            xml_cru = xml_cru.replace("data-src=", "src=")

        feed = feedparser.parse(xml_cru)

        for entry in feed.entries:
            timestamp, data_formatada = pegar_timestamp_e_data(entry)
            noticias_feed.append({
                "titulo":    html_module.escape(entry.get("title", "Sem título")),
                "link":      sanitizar_url(entry.get("link", "#")),
                "data_pub":  data_formatada,
                "fonte":     nome_site,
                "categoria": categoria,
                "timestamp": timestamp,
                "resumo":    limpar_html_resumo(entry.get("summary", "")),
                "imagem":    cacador_de_imagens(entry, nome_site),
            })

    except TimeoutError:
        logger.warning("Timeout ao buscar '%s' (%s)", nome_site, url)
    except urllib.error.HTTPError as e:
        logger.warning("HTTP %s ao buscar '%s': %s", e.code, nome_site, e.reason)
    except urllib.error.URLError as e:
        logger.warning("Erro de URL ao buscar '%s': %s", nome_site, e.reason)
    except gzip.BadGzipFile:
        logger.warning("Falha ao descompactar resposta gzip de '%s'", nome_site)
    except Exception as e:
        logger.exception("Erro inesperado ao buscar '%s': %s", nome_site, e)

    return noticias_feed

def buscar_todas_noticias() -> list:
    todas = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        tarefas = {
            executor.submit(buscar_feed_individual, cat, nome, url): nome
            for cat, fontes in fontes_rss.items()
            for nome, url in fontes
        }
        for futuro in concurrent.futures.as_completed(tarefas):
            todas.extend(futuro.result())

    todas.sort(key=lambda x: x["timestamp"], reverse=True)
    return todas

# ─────────────────────────────────────────────
# GERAÇÃO DE HTML
# ─────────────────────────────────────────────

def gerar_html(noticias: list) -> str:
    fontes_nomes = sorted({n["fonte"] for n in noticias})
    dados_json   = json.dumps(noticias,     ensure_ascii=False)
    fontes_json  = json.dumps(fontes_nomes, ensure_ascii=False)

    sidebar_items = ""
    for categoria, fontes in fontes_rss.items():
        sidebar_items += f"""
        <div class="grupo-categoria">
            <a onclick="mudarContexto('categoria', '{categoria}', this)" class="menu-link cat-title" id="menu-{categoria}">{categoria}</a>
"""
        for nome_site, _ in sorted(fontes, key=lambda x: x[0]):
            sidebar_items += (
                f'        <a onclick="mudarContexto(\'fonte\', \'{nome_site}\', this)" '
                f'class="menu-link sub-link">\u21b3 {nome_site}</a>\n'
            )
        sidebar_items += "        </div>\n"

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Meu Agregador de Notícias</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Lora:ital,wght@0,400;0,600;1,400&family=DM+Sans:wght@300;400;500&display=swap');

        :root {{
            --cream:       #f5f0e8;
            --cream-deep:  #ede6d6;
            --sand:        #d4c5a9;
            --terra:       #c96442;
            --terra-light: #e8907a;
            --terra-muted: #f2d5cc;
            --ink:         #2d2318;
            --ink-mid:     #5c4a38;
            --ink-soft:    #9c8b78;
            --sidebar-bg:  #2a1f14;
            --sidebar-mid: #3d2e1e;
            --white:       #fdfaf6;
        }}

        * {{ box-sizing: border-box; margin: 0; padding: 0; }}

        body {{
            font-family: 'DM Sans', sans-serif;
            background-color: var(--cream);
            color: var(--ink);
            height: 100vh;
            overflow: hidden;
            display: flex;
            flex-direction: column;
        }}

        /* ── Top bar (mobile) ── */
        .topbar {{
            display: none;
            align-items: center;
            justify-content: space-between;
            background-color: var(--sidebar-bg);
            padding: 14px 18px;
            flex-shrink: 0;
            z-index: 200;
        }}
        .topbar-title {{
            font-family: 'Lora', serif;
            font-size: 1.05em;
            font-weight: 600;
            color: var(--cream);
            cursor: pointer;
        }}
        .hamburger {{
            background: none;
            border: none;
            cursor: pointer;
            display: flex;
            flex-direction: column;
            gap: 5px;
            padding: 4px;
        }}
        .hamburger span {{
            display: block;
            width: 24px;
            height: 2px;
            background-color: var(--sand);
            border-radius: 2px;
            transition: all 0.25s;
        }}
        .hamburger.aberto span:nth-child(1) {{ transform: translateY(7px) rotate(45deg); }}
        .hamburger.aberto span:nth-child(2) {{ opacity: 0; }}
        .hamburger.aberto span:nth-child(3) {{ transform: translateY(-7px) rotate(-45deg); }}

        /* ── Layout principal ── */
        .main {{
            display: flex;
            flex: 1;
            overflow: hidden;
        }}

        /* ── Sidebar ── */
        .sidebar {{
            width: 272px;
            background-color: var(--sidebar-bg);
            color: var(--sand);
            padding: 28px 20px 48px 20px;
            overflow-y: auto;
            flex-shrink: 0;
            border-right: 1px solid rgba(255,255,255,0.05);
            transition: transform 0.28s ease;
        }}

        .sidebar h2 {{
            font-family: 'Lora', serif;
            font-weight: 600;
            font-size: 1.15em;
            color: var(--cream);
            margin: 0 0 8px 0;
            text-align: center;
            cursor: pointer;
            letter-spacing: 0.01em;
            transition: color 0.2s;
        }}
        .sidebar h2:hover {{ color: var(--terra-light); }}

        .sidebar-subtitle {{
            text-align: center;
            font-size: 0.72em;
            color: var(--ink-soft);
            letter-spacing: 0.08em;
            text-transform: uppercase;
            margin-bottom: 28px;
        }}

        .sidebar-divider {{
            border: none;
            border-top: 1px solid rgba(255,255,255,0.07);
            margin: 6px 0 14px 0;
        }}

        .menu-link {{
            display: block;
            color: var(--sand);
            text-decoration: none;
            padding: 8px 10px;
            transition: all 0.18s;
            cursor: pointer;
            border-radius: 6px;
        }}
        .menu-link.ativo {{
            color: var(--cream);
            background-color: var(--sidebar-mid);
            padding-left: 14px;
        }}

        .grupo-categoria {{ margin-top: 8px; }}

        .cat-title {{
            font-family: 'Lora', serif !important;
            font-size: 0.8em !important;
            font-weight: 600 !important;
            color: var(--terra-light) !important;
            letter-spacing: 0.1em !important;
            text-transform: uppercase !important;
            padding: 10px 10px 4px !important;
            margin-bottom: 2px;
            cursor: pointer;
        }}
        .cat-title:hover {{ color: var(--cream) !important; background-color: transparent !important; }}

        .sub-link {{
            font-size: 0.87em !important;
            padding-left: 20px !important;
            color: #a89880 !important;
            padding-top: 5px;
            padding-bottom: 5px;
        }}
        .sub-link:hover {{
            color: var(--cream) !important;
            background-color: var(--sidebar-mid) !important;
            padding-left: 24px !important;
        }}
        .sub-link.ativo {{
            color: var(--cream) !important;
            background-color: var(--sidebar-mid) !important;
            padding-left: 24px !important;
        }}

        /* ── Content area ── */
        .content {{
            flex: 1;
            padding: 40px 48px;
            overflow-y: auto;
            scroll-behavior: smooth;
            background-color: var(--cream);
        }}

        #header-secao {{
            font-family: 'Lora', serif;
            font-size: 1.9em;
            font-weight: 600;
            color: var(--ink);
            border-bottom: 2px solid var(--sand);
            padding-bottom: 12px;
            margin-bottom: 24px;
            letter-spacing: -0.01em;
        }}

        /* ── Filtros ── */
        .filtros-container {{
            display: flex;
            flex-wrap: wrap;
            gap: 7px;
            margin-bottom: 28px;
        }}
        .filtro-btn {{
            background-color: var(--white);
            color: var(--ink-mid);
            border: 1.5px solid var(--sand);
            padding: 5px 13px;
            border-radius: 20px;
            font-size: 0.8em;
            font-family: 'DM Sans', sans-serif;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.18s;
            letter-spacing: 0.01em;
        }}
        .filtro-btn:hover {{
            background-color: var(--terra-muted);
            border-color: var(--terra-light);
            color: var(--ink);
        }}
        .filtro-btn.inativo {{
            background-color: transparent;
            color: var(--ink-soft);
            border-color: var(--cream-deep);
            text-decoration: line-through;
            opacity: 0.6;
        }}

        /* ── Cards de notícia ── */
        .noticia {{
            background: var(--white);
            padding: 18px 20px;
            border-radius: 10px;
            box-shadow: 0 1px 4px rgba(45,35,24,0.07);
            margin-bottom: 12px;
            border-left: 4px solid var(--terra);
            display: flex;
            gap: 18px;
            align-items: flex-start;
            transition: box-shadow 0.18s, transform 0.18s;
        }}
        .noticia:hover {{
            box-shadow: 0 4px 16px rgba(45,35,24,0.11);
            transform: translateY(-1px);
        }}
        .noticia-img {{
            width: 130px;
            height: 90px;
            object-fit: contain;
            background-color: var(--cream);
            border-radius: 6px;
            flex-shrink: 0;
        }}
        .sem-img {{
            background-color: var(--cream-deep);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.8em;
            color: var(--sand);
        }}
        .noticia-conteudo {{ flex: 1; min-width: 0; }}
        .noticia a {{
            text-decoration: none;
            font-family: 'Lora', serif;
            color: var(--ink);
            font-weight: 600;
            font-size: 1.05em;
            display: block;
            line-height: 1.4;
            margin-bottom: 6px;
            transition: color 0.15s;
        }}
        .noticia a:hover {{ color: var(--terra); }}
        .resumo {{
            font-size: 0.88em;
            color: var(--ink-mid);
            margin: 0 0 10px 0;
            line-height: 1.55;
            font-weight: 300;
        }}
        .data {{
            font-size: 0.78em;
            color: var(--ink-soft);
            letter-spacing: 0.02em;
        }}
        .fonte {{
            font-size: 0.72em;
            font-weight: 500;
            letter-spacing: 0.06em;
            text-transform: uppercase;
            padding: 3px 9px;
            border-radius: 4px;
            display: inline-block;
            margin-bottom: 8px;
        }}
        .fonte-londrina {{ color: #b94f2a; background-color: #f7ddd4; }}
        .fonte-brasil   {{ color: #3a6b4a; background-color: #d4ead9; }}
        .fonte-mundo    {{ color: #2e5080; background-color: #d3e0f0; }}

        /* ── Paginação ── */
        .paginacao {{
            display: flex;
            justify-content: center;
            align-items: center;
            flex-wrap: wrap;
            gap: 5px;
            margin-top: 32px;
            padding-bottom: 40px;
        }}
        .btn-pag {{
            background-color: var(--white);
            border: 1.5px solid var(--sand);
            padding: 7px 13px;
            border-radius: 6px;
            cursor: pointer;
            color: var(--ink-mid);
            font-family: 'DM Sans', sans-serif;
            font-weight: 500;
            font-size: 0.88em;
            transition: all 0.18s;
            min-width: 36px;
        }}
        .btn-pag:hover {{ background-color: var(--terra-muted); border-color: var(--terra-light); color: var(--ink); }}
        .btn-pag.ativo {{ background-color: var(--terra); color: var(--white); border-color: var(--terra); pointer-events: none; }}
        .btn-pag:disabled {{ background-color: transparent; color: var(--sand); border-color: var(--cream-deep); cursor: not-allowed; }}
        .btn-pag.reticencias {{ pointer-events: none; border: none; background: transparent; color: var(--ink-soft); }}
        .pag-ir {{
            display: flex;
            align-items: center;
            gap: 6px;
            margin-left: 10px;
            font-size: 0.85em;
            color: var(--ink-soft);
        }}
        .pag-ir input {{
            width: 50px;
            padding: 6px 8px;
            border: 1.5px solid var(--sand);
            border-radius: 6px;
            text-align: center;
            font-size: 0.9em;
            background: var(--white);
            color: var(--ink);
            font-family: 'DM Sans', sans-serif;
        }}
        .pag-ir input:focus {{ outline: none; border-color: var(--terra-light); }}
        .pag-ir button {{
            padding: 6px 11px;
            border-radius: 6px;
            border: 1.5px solid var(--terra);
            background: var(--terra);
            color: var(--white);
            cursor: pointer;
            font-size: 0.88em;
            font-family: 'DM Sans', sans-serif;
            transition: all 0.18s;
        }}
        .pag-ir button:hover {{ background: var(--terra-light); border-color: var(--terra-light); }}
        .info-pag {{ font-size: 0.85em; color: var(--ink-soft); margin: 0 8px; white-space: nowrap; }}

        /* ── Overlay (fecha sidebar no mobile) ── */
        .overlay {{
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(0,0,0,0.45);
            z-index: 150;
        }}
        .overlay.visivel {{ display: block; }}

        /* ── Scrollbar ── */
        ::-webkit-scrollbar {{ width: 6px; }}
        ::-webkit-scrollbar-track {{ background: transparent; }}
        ::-webkit-scrollbar-thumb {{ background: var(--sand); border-radius: 3px; }}
        ::-webkit-scrollbar-thumb:hover {{ background: var(--terra-light); }}

        /* ── RESPONSIVO ── */
        @media (max-width: 768px) {{
            .topbar {{ display: flex; }}

            .sidebar {{
                position: fixed;
                top: 0;
                left: 0;
                height: 100%;
                z-index: 160;
                transform: translateX(-100%);
                padding-top: 20px;
            }}
            .sidebar.aberta {{ transform: translateX(0); }}

            /* Esconde o h2 dentro da sidebar no mobile (já aparece na topbar) */
            .sidebar h2,
            .sidebar-subtitle,
            .sidebar-divider:first-of-type {{ display: none; }}

            .content {{
                padding: 20px 16px;
            }}

            #header-secao {{
                font-size: 1.35em;
                margin-bottom: 16px;
            }}

            .noticia {{
                flex-direction: column;
                gap: 12px;
                padding: 14px 14px;
            }}

            .noticia-img {{
                width: 100%;
                height: 180px;
                object-fit: cover;
            }}

            .sem-img {{
                height: 80px;
            }}

            .noticia a {{
                font-size: 1em;
            }}

            .pag-ir {{ display: none; }}

            .info-pag {{ font-size: 0.8em; }}
        }}
    </style>
</head>
<body>
    <!-- Topbar mobile -->
    <div class="topbar">
        <span class="topbar-title" onclick="mudarContexto('home', null, null)">Meu Agregador</span>
        <button class="hamburger" id="btn-hamburger" onclick="toggleSidebar()" aria-label="Menu">
            <span></span><span></span><span></span>
        </button>
    </div>

    <div class="overlay" id="overlay" onclick="fecharSidebar()"></div>

    <div class="main">
        <div class="sidebar" id="sidebar">
            <h2 onclick="mudarContexto('home', null, null)" title="Voltar para a Página Inicial">Meu Agregador</h2>
            <p class="sidebar-subtitle">Londrina · Brasil · Mundo</p>
            <hr class="sidebar-divider">
            {sidebar_items}
        </div>

        <div class="content" id="scroll-area">
            <div style="max-width: 900px; margin: 0 auto;">
                <h1 id="header-secao">Página Inicial</h1>
                <div id="container-filtros" class="filtros-container"></div>
                <div id="container-noticias"></div>
                <div id="container-paginacao" class="paginacao"></div>
            </div>
        </div>
    </div>

    <script>
        const todasAsNoticias = {dados_json};
        const todasAsFontes   = {fontes_json};
        const ITENS_POR_PAG   = {ITENS_POR_PAG};

        let estado = {{
            contexto:      'home',
            parametro:     null,
            paginaAtual:   1,
            fontesOcultas: new Set()
        }};

        // ── Sidebar mobile ─────────────────────────────────────────────

        function toggleSidebar() {{
            const sidebar = document.getElementById('sidebar');
            const overlay = document.getElementById('overlay');
            const btn     = document.getElementById('btn-hamburger');
            const aberta  = sidebar.classList.toggle('aberta');
            overlay.classList.toggle('visivel', aberta);
            btn.classList.toggle('aberto', aberta);
        }}

        function fecharSidebar() {{
            document.getElementById('sidebar').classList.remove('aberta');
            document.getElementById('overlay').classList.remove('visivel');
            document.getElementById('btn-hamburger').classList.remove('aberto');
        }}

        // ── Navegação ──────────────────────────────────────────────────

        function mudarContexto(novoContexto, parametro, elementoClicado) {{
            estado.contexto    = novoContexto;
            estado.parametro   = parametro;
            estado.paginaAtual = 1;

            document.querySelectorAll('.menu-link').forEach(l => l.classList.remove('ativo'));
            if (elementoClicado) elementoClicado.classList.add('ativo');

            const header = document.getElementById('header-secao');
            if      (novoContexto === 'home')      header.innerText = 'Todas as Notícias';
            else if (novoContexto === 'categoria') header.innerText = 'Categoria: ' + parametro;
            else if (novoContexto === 'fonte')     header.innerText = 'Fonte: ' + parametro;

            fecharSidebar();
            renderizar();
        }}

        function alternarFiltroFonte(fonte) {{
            if (estado.fontesOcultas.has(fonte)) estado.fontesOcultas.delete(fonte);
            else                                 estado.fontesOcultas.add(fonte);
            estado.paginaAtual = 1;
            renderizar();
        }}

        function irParaPagina(num) {{
            estado.paginaAtual = num;
            renderizar();
            document.getElementById('scroll-area').scrollTop = 0;
        }}

        // ── Filtros ────────────────────────────────────────────────────

        function filtrarNoticias() {{
            let lista = todasAsNoticias;
            if      (estado.contexto === 'categoria') lista = lista.filter(n => n.categoria === estado.parametro);
            else if (estado.contexto === 'fonte')     lista = lista.filter(n => n.fonte     === estado.parametro);
            else if (estado.contexto === 'home')      lista = lista.filter(n => !estado.fontesOcultas.has(n.fonte));
            return lista;
        }}

        // ── Paginação ──────────────────────────────────────────────────

        function renderizarPaginacao(paginaAtual, totalPaginas) {{
            const div = document.getElementById('container-paginacao');
            div.innerHTML = '';
            if (totalPaginas <= 1) return;

            const btnAnt = document.createElement('button');
            btnAnt.className   = 'btn-pag';
            btnAnt.textContent = '«';
            btnAnt.disabled    = paginaAtual === 1;
            btnAnt.onclick     = () => irParaPagina(paginaAtual - 1);
            div.appendChild(btnAnt);

            const paginas = [];
            const delta   = 2;
            const left    = paginaAtual - delta;
            const right   = paginaAtual + delta;

            for (let p = 1; p <= totalPaginas; p++) {{
                if (p === 1 || p === totalPaginas || (p >= left && p <= right)) {{
                    paginas.push(p);
                }}
            }}

            let anterior = null;
            paginas.forEach(p => {{
                if (anterior !== null && p - anterior > 1) {{
                    const dots = document.createElement('button');
                    dots.className   = 'btn-pag reticencias';
                    dots.textContent = '…';
                    div.appendChild(dots);
                }}
                const btn = document.createElement('button');
                btn.className   = 'btn-pag' + (p === paginaAtual ? ' ativo' : '');
                btn.textContent = p;
                btn.onclick     = () => irParaPagina(p);
                div.appendChild(btn);
                anterior = p;
            }});

            const btnProx = document.createElement('button');
            btnProx.className   = 'btn-pag';
            btnProx.textContent = '»';
            btnProx.disabled    = paginaAtual === totalPaginas;
            btnProx.onclick     = () => irParaPagina(paginaAtual + 1);
            div.appendChild(btnProx);

            const info = document.createElement('span');
            info.className   = 'info-pag';
            info.textContent = `Página ${{paginaAtual}} de ${{totalPaginas}}`;
            div.appendChild(info);

            const irDiv = document.createElement('div');
            irDiv.className = 'pag-ir';
            irDiv.innerHTML = `
                Ir para
                <input type="number" id="campo-pagina" min="1" max="${{totalPaginas}}" placeholder="#">
                <button onclick="
                    const v = parseInt(document.getElementById('campo-pagina').value);
                    if (v >= 1 && v <= ${{totalPaginas}}) irParaPagina(v);
                ">\u2192</button>
            `;
            div.appendChild(irDiv);
        }}

        // ── Render principal ───────────────────────────────────────────

        function renderizar() {{
            const noticiasFiltradas = filtrarNoticias();
            const totalPaginas = Math.max(1, Math.ceil(noticiasFiltradas.length / ITENS_POR_PAG));

            if (estado.paginaAtual > totalPaginas) estado.paginaAtual = totalPaginas;

            const divFiltros = document.getElementById('container-filtros');
            divFiltros.innerHTML = '';
            if (estado.contexto === 'home') {{
                todasAsFontes.forEach(fonte => {{
                    const btn     = document.createElement('button');
                    const inativa = estado.fontesOcultas.has(fonte);
                    btn.className   = 'filtro-btn ' + (inativa ? 'inativo' : '');
                    btn.textContent = fonte;
                    btn.onclick     = () => alternarFiltroFonte(fonte);
                    divFiltros.appendChild(btn);
                }});
            }}

            const inicio = (estado.paginaAtual - 1) * ITENS_POR_PAG;
            const fim    = inicio + ITENS_POR_PAG;
            const pagina = noticiasFiltradas.slice(inicio, fim);

            const divNoticias = document.getElementById('container-noticias');
            if (pagina.length === 0) {{
                divNoticias.innerHTML = '<p style="text-align:center;color:#7f8c8d;margin-top:40px;">Nenhuma notícia encontrada.</p>';
            }} else {{
                divNoticias.innerHTML = pagina.map(noti => {{
                    const imgTag = noti.imagem
                        ? `<img src="${{noti.imagem}}" class="noticia-img" loading="lazy" alt="Imagem de ${{noti.fonte}}">`
                        : `<div class="noticia-img sem-img">📰</div>`;
                    const classFonte = noti.categoria === 'Londrina' ? 'fonte-londrina'
                                     : noti.categoria === 'Brasil'   ? 'fonte-brasil'
                                     : 'fonte-mundo';
                    return `
                        <div class="noticia">
                            ${{imgTag}}
                            <div class="noticia-conteudo">
                                <span class="fonte ${{classFonte}}">${{noti.fonte}}</span>
                                <a href="${{noti.link}}" target="_blank" rel="noopener noreferrer">${{noti.titulo}}</a>
                                <p class="resumo">${{noti.resumo}}</p>
                                <div class="data">${{noti.data_pub}}</div>
                            </div>
                        </div>`;
                }}).join('');
            }}

            renderizarPaginacao(estado.paginaAtual, totalPaginas);
        }}

        renderizar();
    </script>
</body>
</html>
"""

# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

def gerar_painel():
    logger.info("Buscando feeds RSS...")
    tempo_inicio = time.time()

    noticias      = buscar_todas_noticias()
    html_completo = gerar_html(noticias)

    # Salva como index.html na raiz (necessário para o GitHub Pages)
    caminho_arquivo = "index.html"
    with open(caminho_arquivo, "w", encoding="utf-8") as f:
        f.write(html_completo)

    tempo_total = time.time() - tempo_inicio
    logger.info("index.html gerado com sucesso.")
    logger.info("Notícias carregadas: %d | Tempo: %.2fs", len(noticias), tempo_total)


if __name__ == "__main__":
    gerar_painel()

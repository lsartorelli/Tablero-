"""
Compras del BCRA en el MLC: genera un gráfico por rueda con 2 columnas
(mes actual y mes anterior) más indicadores del día, y lo abre en el navegador.

Uso:
  python grafico_bcra.py                         -> genera con el último mes disponible
  python grafico_bcra.py --mes 2026-09           -> abre directamente en ese mes
  python grafico_bcra.py --cargar 2026-10-30 85 --reservas 50300
                                                 -> guarda el dato del día (aún no publicado en la API) y genera
  python grafico_bcra.py --riesgo 741           -> guarda el riesgo país de hoy (si la fuente todavía no lo publicó)
  python grafico_bcra.py --borrar 2026-10-30     -> elimina un dato cargado a mano
  python grafico_bcra.py --no-abrir              -> genera el HTML sin abrir el navegador (útil programado)

Requiere solo Python 3.8+ (sin librerías externas).
"""
import argparse, datetime as dt, json, math, os, ssl, sys, urllib.parse, urllib.request, urllib.error, webbrowser

API = "https://api.bcra.gob.ar/estadisticas/v4.0/Monetarias/{id}?desde={desde}&limit=3000"
VAR_COMPRAS, VAR_RESERVAS = 78, 1
RIESGO_URL = "https://api.argentinadatos.com/v1/finanzas/indices/riesgo-pais"  # no lo publica el BCRA
VAR_MINORISTA, VAR_TAMAR, VAR_BADLAR = 4, 44, 7  # TAMAR y BADLAR de bancos privados, TNA
VAR_MAYORISTA, VAR_PISO, VAR_TECHO = 5, 1187, 1188   # tipo de cambio mayorista y bandas cambiarias
SERIES_URL = "https://apis.datos.gob.ar/series/api/series/?ids={ids}&limit=5000&format=json"
IPC_GBA = "148.3_INIVELGBA_DICI_M_21"  # IPC nivel general GBA, base dic 2016
ALIMENTOS = [  # precios medios INDEC, GBA
    ("huevos", "Huevos (docena)", "105.1_I2HG_2016_M_23"),
    ("pollo", "Pollo entero (kg)", "348.1_GBA_POLLO_KG__19"),
    ("asado", "Asado (kg)", "105.1_I2A_2016_M_14"),
    ("nalga", "Nalga (kg)", "105.1_I2N_2016_M_14"),
    ("picada", "Carne picada común (kg)", "105.1_I2CPC_2016_M_27"),
    ("leche", "Leche en sachet (litro)", "105.1_I2LFES_2016_M_35"),
    ("aceite", "Aceite de girasol (1,5 l)", "105.1_I2AG_2016_M_23"),
]
INICIO_BANDAS = "2025-04-14"
DOLARES_URL = "https://api.argentinadatos.com/v1/cotizaciones/dolares/{casa}"  # MEP, CCL y blue
BASE = os.path.dirname(os.path.abspath(__file__))
MANUAL = os.path.join(BASE, "datos_manuales.json")
SALIDA = os.path.join(BASE, "grafico_bcra.html")
CONFIG = os.path.join(BASE, "config.json")
HIST_LOCAL = os.path.join(BASE, "historial_bonos.json")  # cierres que guarda el script para bonos sin histórico en data912

# Valores por defecto de config.json (se crea solo la primera vez; después editás ese archivo)
CONFIG_DEFECTO = {
    "escenarios": {
        "_ayuda": "precio_0 y precio_100 son tus precios objetivo con 0 % y 100 % de probabilidad. "
                  "fuente: bono (data912, precio en USD MEP), yahoo (ticker de Yahoo Finance) o merval_usd (Merval / CCL).",
        "filas": [0, 10, 20, 30, 40, 100],
        "activos": [
            {"nombre": "GD38", "fuente": "bono", "simbolo": "GD38D", "precio_0": 85.18, "precio_100": 45.0},
            {"nombre": "GD35", "fuente": "bono", "simbolo": "GD35D", "precio_0": 82.76, "precio_100": 45.0},
            {"nombre": "Merval USD", "fuente": "merval_usd", "simbolo": "^MERV", "precio_0": 2380, "precio_100": 475}
        ]
    },
    "reservas_netas": {
        "_ayuda": "bis_mult_sedesa y la fecha salen del balance semanal del BCRA; actualizalos a mano. "
                  "oro_onzas_millones: tenencia de oro del BCRA en millones de onzas troy.",
        "swap_cnh_millones": 130000,
        "bis_mult_sedesa": 2611,
        "bis_fecha": "2026-10-07",
        "oro_onzas_millones": 1.98,
        "base": {"fecha": "2023-12-31", "brutas": 23071, "encajes": 9303, "swap": 18237,
                 "bis": 4924, "oro": 4099, "base_monetaria": 35171884}
    }
}
BONOS_URL = "https://data912.com/live/arg_bonds"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{t}?range=5d&interval=1d"
CNY_URL = "https://api.frankfurter.app/latest?from=USD&to=CNY"
ORO_URL = "https://api.gold-api.com/price/XAU"
HIST_BONO_URL = "https://data912.com/historical/bonds/{t}"
SPARK_URL = "https://query1.finance.yahoo.com/v8/finance/spark?symbols={s}&range=ytd&interval=1d"
BONOS = [("Globales (ley Nueva York)", ["GD29", "GD30", "GD35", "GD38", "GD41", "GD46"]),
         ("Bonares (ley argentina)", ["AL29", "AL30", "AL35", "AE38", "AL41"]),
         ("Bonares 2026 (AO / AN)", ["AN29", "AO27", "AO28", "AO29"])]
UST_URL = "https://query1.finance.yahoo.com/v8/finance/spark?symbols=%5EIRX,%5EFVX,%5ETNX,%5ETYX&range=5d&interval=1d"


# ---------- Flujos de fondos de los bonos (por cada 100 nominales originales) ----------
def _sumar_meses(d, n, dia):
    import calendar
    y = d.year + (d.month - 1 + n) // 12
    m = (d.month - 1 + n) % 12 + 1
    return dt.date(y, m, min(dia, calendar.monthrange(y, m)[1]))


def _escalon(tramos):
    """tramos: [(hasta_fecha, tasa_anual)] -> función tasa(fecha de pago)."""
    def f(d):
        for hasta, t in tramos:
            if d <= hasta:
                return t
        return tramos[-1][1]
    return f


_D = dt.date
_CUPONES = {
    "29": _escalon([(_D(2099, 1, 1), 0.01)]),
    "30": _escalon([(_D(2021, 7, 9), 0.00125), (_D(2023, 7, 9), 0.005), (_D(2027, 7, 9), 0.0075), (_D(2099, 1, 1), 0.0175)]),
    "35": _escalon([(_D(2021, 7, 9), 0.00125), (_D(2022, 7, 9), 0.01125), (_D(2023, 7, 9), 0.015), (_D(2024, 7, 9), 0.03625),
                    (_D(2027, 7, 9), 0.04125), (_D(2028, 7, 9), 0.0475), (_D(2099, 1, 1), 0.05)]),
    "38": _escalon([(_D(2021, 7, 9), 0.00125), (_D(2022, 7, 9), 0.02), (_D(2023, 7, 9), 0.03875), (_D(2024, 7, 9), 0.0425), (_D(2099, 1, 1), 0.05)]),
    "41": _escalon([(_D(2021, 7, 9), 0.00125), (_D(2022, 7, 9), 0.025), (_D(2029, 7, 9), 0.035), (_D(2099, 1, 1), 0.04875)]),
    "46": _escalon([(_D(2021, 7, 9), 0.00125), (_D(2022, 7, 9), 0.01125), (_D(2023, 7, 9), 0.015), (_D(2024, 7, 9), 0.03625),
                    (_D(2027, 7, 9), 0.04125), (_D(2028, 7, 9), 0.04375), (_D(2099, 1, 1), 0.05)]),
}
_AMORT = {  # (desde, hasta, % por cuota) en fechas 9-ene / 9-jul
    "29": [(_D(2025, 1, 9), _D(2029, 7, 9), 10)],
    "30": [(_D(2024, 7, 9), _D(2024, 7, 9), 4), (_D(2025, 1, 9), _D(2030, 7, 9), 8)],
    "35": [(_D(2031, 1, 9), _D(2035, 7, 9), 10)],
    "38": [(_D(2027, 7, 9), _D(2038, 1, 9), 100 / 22)],
    "41": [(_D(2028, 1, 9), _D(2041, 7, 9), 100 / 28)],
    "46": [(_D(2025, 1, 9), _D(2046, 7, 9), 100 / 44)],
}
_VTO = {"29": _D(2029, 7, 9), "30": _D(2030, 7, 9), "35": _D(2035, 7, 9), "38": _D(2038, 1, 9), "41": _D(2041, 7, 9), "46": _D(2046, 7, 9)}
_NUEVOS = {"AO27": (_D(2027, 10, 29), 0.06, 1), "AO28": (_D(2028, 10, 31), 0.06, 1),
           "AO29": (_D(2029, 10, 31), 0.06, 1), "AN29": (_D(2029, 11, 30), 0.065, 6)}  # vto, cupón, meses entre pagos


def flujos_bono(t):
    """Lista de (fecha, cupón, amortización, residual antes del pago)."""
    if t in _NUEVOS:
        vto, cup, meses = _NUEVOS[t]
        fechas, d = [], vto
        while d > _D(2025, 11, 1):
            fechas.append(d)
            d = _sumar_meses(d, -meses, vto.day)
        return [(d, 100 * cup * meses / 12, 100 if d == vto else 0, 100.0) for d in sorted(fechas)]
    k = t[2:4]
    tasa, amort = _CUPONES[k], _AMORT[k]
    out, res, d = [], 100.0, _D(2021, 1, 9)
    while res > 1e-9 and d <= _VTO[k]:
        a = sum(p for ini, fin, p in amort if ini <= d <= fin)
        out.append((d, res * tasa(d) / 2, a, res))
        res -= a
        d = _D(d.year, 7, 9) if d.month == 1 else _D(d.year + 1, 1, 9)
    return out


def _d360(a, b):
    return (b.year - a.year) * 360 + (b.month - a.month) * 30 + (min(b.day, 30) - min(a.day, 30))


def metricas_bono(t, precio, liq):
    """TIR efectiva anual, TNA 180/360, duration modificada, convexidad, valor técnico y paridad.
    El precio de BYMA es precio sucio (incluye intereses corridos)."""
    fl = flujos_bono(t)
    fut = [f for f in fl if f[0] > liq]
    pas = [f for f in fl if f[0] <= liq]
    if not fut:
        return None
    res, prox = fut[0][3], fut[0][0]
    ult = pas[-1][0] if pas else None
    corrido = fut[0][1] * _d360(ult, liq) / max(_d360(ult, prox), 1) if ult else 0.0
    vt = res + corrido
    cfs = [((f[0] - liq).days / 365.0, f[1] + f[2]) for f in fut]
    lo, hi = -0.5, 3.0
    for _ in range(200):
        y = (lo + hi) / 2
        if sum(c / (1 + y) ** x for x, c in cfs) > precio:
            lo = y
        else:
            hi = y
    pv = sum(c / (1 + y) ** x for x, c in cfs)
    mac = sum(x * c / (1 + y) ** x for x, c in cfs) / pv
    conv = sum(x * (x + 1) * c / (1 + y) ** (x + 2) for x, c in cfs) / pv
    cupon_hoy = (fut[0][1] / res * (12 / _NUEVOS[t][2]) if t in _NUEVOS else fut[0][1] / res * 2) * 100 if res else 0
    vto = fl[-1][0]
    return {"tir": y * 100, "tna": 2 * ((1 + y) ** 0.5 - 1) * 100, "md": mac / (1 + y), "conv": conv,
            "vt": vt, "paridad": precio / vt * 100, "cupon": cupon_hoy, "vto": vto.isoformat(),
            "flujos": [[f[0].isoformat(), round(f[1], 4), round(f[2], 4)] for f in fut]}


ITCRM_URL = "https://www.bcra.gob.ar/archivos/Pdfs/PublicacionesEstadisticas/ITCRMSerie.xlsx"
INFL_URL = "https://api.argentinadatos.com/v1/finanzas/indices/inflacion"
IPC_SAN_LUIS = "197.1_NIVEL_GENERAL_2014_0_13"  # IPC San Luis: reemplaza al INDEC intervenido 2007-2016
MERVAL_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%5EMERV?range=max&interval=1mo"


def get_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.read()
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), ssl.SSLError):
            with urllib.request.urlopen(req, timeout=90, context=ssl._create_unverified_context()) as r:
                return r.read()
        raise


def itcrm_mensual():
    """Lee la hoja de promedios mensuales del Excel del BCRA sin librerías externas."""
    import io, re as _re, zipfile
    z = zipfile.ZipFile(io.BytesIO(get_bytes(ITCRM_URL)))
    wb = z.read("xl/workbook.xml").decode("utf-8")
    rels = dict(_re.findall(r'Id="(rId\d+)"[^>]*Target="([^"]+)"', z.read("xl/_rels/workbook.xml.rels").decode("utf-8")))
    rid = next(r for n, r in _re.findall(r'<sheet [^>]*name="([^"]+)"[^>]*r:id="([^"]+)"', wb) if "mens" in n.lower())
    xml = z.read("xl/" + rels[rid].lstrip("/").replace("xl/", "")).decode("utf-8")
    out = {}
    for fila in _re.findall(r"<row [^>]*>(.*?)</row>", xml, _re.S):
        celdas = dict(_re.findall(r'<c r="([A-Z]+)\d+"[^>]*?(?:/>|>(?:<f>.*?</f>)?<v>([^<]*)</v>)', fila))
        try:
            f = dt.date(1899, 12, 30) + dt.timedelta(days=int(float(celdas["A"])))
            if f.year >= 1997:
                out[f.strftime("%Y-%m")] = float(celdas["B"])
        except (KeyError, ValueError):
            pass
    return out


def _mes_sig(k):
    y, m = map(int, k.split("-"))
    m += 1
    if m > 12:
        m, y = 1, y + 1
    return f"{y}-{m:02d}"


def ipc_empalmado():
    """Índice de precios mensual desde dic-2001 = 1: INDEC hasta 2006 y desde 2017, IPC San Luis 2007-2016."""
    ind = {x["fecha"][:7]: x["valor"] for x in get_json(INFL_URL)}
    sl = {r[0][:7]: r[1] for r in get_json(SERIES_URL.format(ids=IPC_SAN_LUIS))["data"] if r[1]}
    idx, k = {"2001-12": 1.0}, "2001-12"
    while True:
        n = _mes_sig(k)
        if "2007-01" <= n <= "2016-12":
            if n not in sl or k not in sl:
                break
            r = sl[n] / sl[k]
        else:
            if n not in ind:
                break
            r = 1 + ind[n] / 100
        idx[n] = idx[k] * r
        k = n
    return idx


def promedio_mensual(puntos):
    acc = {}
    for f, v in puntos:
        acc.setdefault(f[:7], []).append(v)
    return {k: sum(v) / len(v) for k, v in acc.items()}


INDEC_PRECIOS_URL = "https://www.indec.gob.ar/ftp/cuadros/economia/sh_ipc_precios_promedio.xls"
INDEC_NOMBRES = {"huevos de gallina": "huevos", "pollo entero": "pollo", "asado": "asado", "nalga": "nalga", "carne picada común": "picada",
                 "leche fresca entera en sachet": "leche", "aceite de girasol": "aceite"}


def _mes_ant(k):
    y, m = map(int, k.split("-"))
    m -= 1
    if m < 1:
        m, y = 12, y - 1
    return f"{y}-{m:02d}"


def precios_indec():
    """Precios promedio en GBA desde el Excel del INDEC (hoja GBA). Requiere xlrd."""
    import xlrd
    hoja = xlrd.open_workbook(file_contents=get_bytes(INDEC_PRECIOS_URL)).sheet_by_name("GBA")
    meses_es = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
    fila_mes = next(r for r in range(10) if str(hoja.cell_value(r, 2)).strip().lower() in meses_es)
    y, m = 2016, meses_es.index(str(hoja.cell_value(fila_mes, 2)).strip().lower()) + 1
    for r in range(fila_mes):  # el año inicial figura en la fila de arriba ("Año 2016")
        t = str(hoja.cell_value(r, 2))
        if "20" in t:
            y = int("".join(ch for ch in t if ch.isdigit())[:4])
    cols = []
    for c in range(2, hoja.ncols):
        if str(hoja.cell_value(fila_mes, c)).strip():
            cols.append((c, f"{y}-{m:02d}-01"))
            m += 1
            if m > 12:
                m, y = 1, y + 1
    out = {}
    for r in range(fila_mes + 1, hoja.nrows):
        clave = INDEC_NOMBRES.get(str(hoja.cell_value(r, 0)).strip().lower())
        if clave:
            out[clave] = {f: float(hoja.cell_value(r, c)) for c, f in cols if isinstance(hoja.cell_value(r, c), float)}
    return out


# ---------- Compras netas de moneda extranjera de "Personas humanas" (balance cambiario mensual del BCRA) ----------
PH_ARCHIVO = os.path.join(BASE, "personas_humanas.json")
PH_URL = "https://www.bcra.gob.ar/archivos/Pdfs/PublicacionesEstadisticas/informes/informe-mercado-cambios-balance-cambiario-{ym}.pdf"
PH_SEED = {  # USD millones netos, del resumen ejecutivo de cada informe (abr-25 sin dato comparable)
    "2025-01": 917, "2025-02": 769, "2025-03": 570, "2025-04": None, "2025-05": 3846, "2025-06": 4357, "2025-07": 5644,
    "2025-08": 3450, "2025-09": 6857, "2025-10": 5068, "2025-11": 1596, "2025-12": 2793, "2026-01": 3146,
    "2026-02": 2552, "2026-03": 2470, "2026-04": 3199, "2026-05": 2667, "2026-06": 2821, "2026-07": 4124,
    "2026-08": 3366, "2026-09": 3410}


def personas_humanas(hoy):
    """Serie mensual; busca los informes nuevos (PDF) y guarda lo que encuentra. Requiere pypdf para meses nuevos."""
    try:
        with open(PH_ARCHIVO, encoding="utf-8") as f:
            datos = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        datos = {}
    for k, v in PH_SEED.items():
        datos.setdefault(k, v)
    k = max(datos)
    tope = _mes_ant(hoy.strftime("%Y-%m"))
    nuevos = False
    while k < tope:
        k = _mes_sig(k)
        try:
            pdf = get_bytes(PH_URL.format(ym=k))
        except Exception:
            break  # todavía no se publicó
        if not pdf.startswith(b"%PDF"):
            break
        try:
            import io, re as _re
            from pypdf import PdfReader
        except ImportError:
            aviso("para leer el informe cambiario nuevo instalá pypdf: pip install pypdf")
            break
        r = PdfReader(io.BytesIO(pdf))
        t = " ".join((pg.extract_text() or "") for pg in r.pages[2:6])
        t = _re.sub(r"\s+", " ", t)
        t = _re.sub(r"(\d) (\d)", r"\1\2", t).replace("US D", "USD")
        i = t.lower().find("personas humanas")
        m = _re.search(r"USD ([\d\.]+) millones", t[i:i + 500]) if i >= 0 else None
        if not m:
            aviso(f"no se encontró el dato de personas humanas en el informe de {k}")
            break
        datos[k] = float(m.group(1).replace(".", ""))
        nuevos = True
    if nuevos:
        with open(PH_ARCHIVO, "w", encoding="utf-8") as f:
            json.dump(dict(sorted(datos.items())), f, ensure_ascii=False, indent=1)
    return sorted(datos.items())


def dia_habil_siguiente(d):
    d += dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


ADRS = [("GGAL", "Grupo Galicia"), ("BMA", "Banco Macro"), ("BBAR", "BBVA Argentina"), ("SUPV", "Supervielle"),
        ("YPF", "YPF"), ("PAM", "Pampa Energía"), ("VIST", "Vista Energy"), ("TGS", "TGS"),
        ("CEPU", "Central Puerto"), ("EDN", "Edenor"), ("TEO", "Telecom"), ("LOMA", "Loma Negra"),
        ("CRESY", "Cresud"), ("IRS", "IRSA"), ("CAAP", "Corporación América"), ("TX", "Ternium"),
        ("TS", "Tenaris"), ("MELI", "Mercado Libre"), ("GLOB", "Globant"), ("BIOX", "Bioceres")]
VAR_INFL_M, VAR_INFL_IA = 27, 28  # inflación mensual e interanual (INDEC, vía BCRA)
VAR_ENCAJES, VAR_BASE = 1256, 15  # cuenta corriente en moneda extranjera en el BCRA (millones de $), base monetaria


AVISOS = []


def aviso(msg):
    """Muestra el problema en consola y lo deja anotado para mostrarlo al pie del tablero."""
    print("Aviso: " + msg)
    AVISOS.append(msg)


def leer_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = CONFIG_DEFECTO
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print(f"Se creó {CONFIG} con los valores por defecto.")
    return cfg


def precio_activo(a, bonos_cache, ccl):
    f, sim = a.get("fuente"), a.get("simbolo")
    if f == "bono":
        if "d" not in bonos_cache:
            bonos_cache["d"] = {x["symbol"]: x for x in get_json(BONOS_URL)}
        x = bonos_cache["d"].get(sim)
        return float(x["c"]) if x and x.get("c") else None
    if f == "yahoo":
        return float(get_json(YAHOO_URL.format(t=urllib.parse.quote(sim)))["chart"]["result"][0]["meta"]["regularMarketPrice"])
    if f == "merval_usd":
        merv = float(get_json(YAHOO_URL.format(t="%5EMERV"))["chart"]["result"][0]["meta"]["regularMarketPrice"])
        return merv / ccl if ccl else None
    return None

# Cierres informados por el BCRA que la API todavía no publicaba al armar el script.
# Se ignoran solos cuando la API ya tiene ese día.
SEED = [
    {"fecha": "2026-10-27", "compra": 0,  "reservas": 50580},
    {"fecha": "2026-10-28", "compra": 0,  "reservas": 50601},
    {"fecha": "2026-10-29", "compra": 47, "reservas": 50154, "riesgo": 741},
]


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.load(r)
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), ssl.SSLError):
            # Algunos Windows no tienen la cadena de certificados del BCRA instalada
            print("Aviso: falló la verificación SSL; reintentando sin verificar el certificado.")
            with urllib.request.urlopen(req, timeout=40, context=ssl._create_unverified_context()) as r:
                return json.load(r)
        raise


def serie(var_id, desde):
    j = get_json(API.format(id=var_id, desde=desde))
    det = (j.get("results") or [{}])[0].get("detalle", [])
    return sorted(({"fecha": x["fecha"], "valor": float(x["valor"])} for x in det), key=lambda x: x["fecha"])


def leer_manuales():
    try:
        with open(MANUAL, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def guardar_manuales(datos):
    with open(MANUAL, "w", encoding="utf-8") as f:
        json.dump(sorted(datos, key=lambda x: x["fecha"]), f, ensure_ascii=False, indent=2)


def mezclar(api, campo, extras):
    tiene = {x["fecha"] for x in api}
    agregado = {}
    for m in extras:
        if m.get(campo) is not None and m["fecha"] not in tiene:
            agregado[m["fecha"]] = {"fecha": m["fecha"], "valor": float(m[campo]), "prov": True}
    return sorted(api + list(agregado.values()), key=lambda x: x["fecha"])


def main():
    ap = argparse.ArgumentParser(description="Gráfico de compras del BCRA por rueda")
    ap.add_argument("--mes", help="Mes a mostrar al abrir, formato AAAA-MM")
    ap.add_argument("--cargar", nargs=2, metavar=("FECHA", "COMPRA"), help="Dato del día: AAAA-MM-DD y compra en USD M")
    ap.add_argument("--reservas", type=float, help="Reservas brutas del día (USD M), junto con --cargar")
    ap.add_argument("--riesgo", type=float, help="Riesgo país del día (puntos); usa la fecha de --cargar o la de hoy")
    ap.add_argument("--borrar", metavar="FECHA", help="Borra un dato cargado a mano")
    ap.add_argument("--no-abrir", action="store_true", help="No abrir el navegador")
    ap.add_argument("--salida", help="Ruta del HTML a generar (por defecto grafico_bcra.html en esta carpeta)")
    a = ap.parse_args()

    manuales = leer_manuales()
    if a.cargar:
        fecha, compra = a.cargar
        dt.date.fromisoformat(fecha)  # valida el formato
        manuales = [m for m in manuales if m["fecha"] != fecha]
        manuales.append({"fecha": fecha, "compra": float(compra.replace(",", ".")), "reservas": a.reservas})
        guardar_manuales(manuales)
        print(f"Guardado {fecha}: compra USD {compra} M" + (f", reservas USD {a.reservas:.0f} M" if a.reservas else ""))
    if a.riesgo is not None:
        fecha = a.cargar[0] if a.cargar else dt.date.today().isoformat()
        ex = next((m for m in manuales if m["fecha"] == fecha), None)
        if ex: ex["riesgo"] = a.riesgo
        else: manuales.append({"fecha": fecha, "compra": None, "reservas": None, "riesgo": a.riesgo})
        guardar_manuales(manuales)
        print(f"Guardado riesgo país {fecha}: {a.riesgo:.0f} puntos")
    if a.borrar:
        manuales = [m for m in manuales if m["fecha"] != a.borrar]
        guardar_manuales(manuales)
        print(f"Borrado el dato del {a.borrar}")

    hoy = dt.date.today()
    desde = dt.date(hoy.year - 1, hoy.month, 1).isoformat()  # 13 meses: selector de 12 meses + el anterior
    print("Consultando la API del BCRA…")
    try:
        api_c = serie(VAR_COMPRAS, desde)
        api_r = serie(VAR_RESERVAS, desde)
        corto = (hoy - dt.timedelta(days=30)).isoformat()
        tc = serie(VAR_MINORISTA, corto)
        tamar = serie(VAR_TAMAR, corto)
        badlar = serie(VAR_BADLAR, corto)
    except Exception as e:
        print(f"No se pudieron traer los datos del BCRA: {e}")
        sys.exit(1)

    try:
        rp = sorted(({"fecha": x["fecha"], "valor": float(x["valor"])} for x in get_json(RIESGO_URL)
                     if x["fecha"] >= corto), key=lambda x: x["fecha"])
    except Exception as e:
        aviso(f"no se pudo traer el riesgo país ({e})")
        rp = []
    dolar = {}
    try:
        may = serie(VAR_MAYORISTA, INICIO_BANDAS)
        dolar["mayorista"] = may[-2:]
        piso, techo = serie(VAR_PISO, INICIO_BANDAS), serie(VAR_TECHO, INICIO_BANDAS)
        dolar["hist"] = {"mayorista": may, "piso": piso, "techo": techo}
        # El BCRA publica las bandas por adelantado: se toma la del día del último mayorista
        f_may = may[-1]["fecha"] if may else hoy.isoformat()
        hasta = lambda ser: next((x for x in reversed(ser) if x["fecha"] <= f_may), None)
        dolar["piso"], dolar["techo"] = hasta(piso), hasta(techo)
        may_por_fecha = {x["fecha"]: x["valor"] for x in may}

        def may_a(fecha):  # mayorista del mismo día o el último anterior
            ant = [f for f in may_por_fecha if f <= fecha]
            return may_por_fecha[max(ant)] if ant else None

        dolar["brechas"] = {}
        for clave, casa in (("mep", "bolsa"), ("ccl", "contadoconliqui"), ("blue", "blue")):
            try:
                hist = [x for x in get_json(DOLARES_URL.format(casa=casa)) if x["fecha"] >= corto][-2:]
                filas = []
                for x in hist:
                    m = may_a(x["fecha"])
                    filas.append({"fecha": x["fecha"], "precio": float(x["venta"]),
                                  "brecha": (float(x["venta"]) / m - 1) * 100 if m else None})
                dolar["brechas"][clave] = filas
            except Exception as e:
                aviso(f"no se pudo traer el dólar {casa} ({e})")
    except Exception as e:
        aviso(f"no se pudo traer el dólar mayorista o las bandas ({e})")
    try:  # brecha CCL contra MEP, últimos 2 años
        desde_b = (hoy - dt.timedelta(days=730)).isoformat()
        mep = {x["fecha"]: float(x["venta"]) for x in get_json(DOLARES_URL.format(casa="bolsa")) if x["fecha"] >= desde_b and x.get("venta")}
        ccl_h = {x["fecha"]: float(x["venta"]) for x in get_json(DOLARES_URL.format(casa="contadoconliqui")) if x["fecha"] >= desde_b and x.get("venta")}
        dolar["ccl_mep"] = [{"fecha": f, "valor": (ccl_h[f] / mep[f] - 1) * 100, "mep": mep[f], "ccl": ccl_h[f]}
                            for f in sorted(set(mep) & set(ccl_h))]
    except Exception as e:
        aviso(f"no se pudo calcular la brecha CCL/MEP ({e})")
    cfg = leer_config()

    # --- Escenarios (Prob K)
    escenarios = {"filas": cfg["escenarios"]["filas"], "activos": []}
    bonos_cache = {}
    ccl = None
    try:
        ccl = float(get_json(DOLARES_URL.format(casa="contadoconliqui"))[-1]["venta"])
    except Exception:
        pass
    for act in cfg["escenarios"]["activos"]:
        try:
            px = precio_activo(act, bonos_cache, ccl)
        except Exception as e:
            aviso(f"no se pudo traer el precio de {act['nombre']} ({e})")
            px = None
        escenarios["activos"].append({"nombre": act["nombre"], "p0": act["precio_0"], "p100": act["precio_100"],
                                      "precio": px, "fuente": act.get("fuente")})

    # --- Reservas netas
    rn_cfg = cfg["reservas_netas"]
    reservas_netas = {"base": rn_cfg["base"], "bis": rn_cfg["bis_mult_sedesa"], "bis_fecha": rn_cfg["bis_fecha"]}
    try:
        _r = mezclar(api_r, "reservas", SEED + manuales)
        res_ult = _r[-1] if _r else None
        enc = serie(VAR_ENCAJES, corto)
        may_e = {x["fecha"]: x["valor"] for x in serie(VAR_MAYORISTA, corto)}
        enc_ult = next((x for x in reversed(enc) if x["fecha"] in may_e), None)
        bm = serie(VAR_BASE, corto)
        reservas_netas.update({
            "brutas": res_ult,
            "encajes": {"fecha": enc_ult["fecha"], "valor": enc_ult["valor"] / may_e[enc_ult["fecha"]]} if enc_ult else None,
            "base_monetaria": bm[-1] if bm else None,
        })
    except Exception as e:
        aviso(f"no se pudieron traer encajes o base monetaria ({e})")
    try:
        cny = float(get_json(CNY_URL)["rates"]["CNY"])
        reservas_netas["swap"] = {"valor": rn_cfg["swap_cnh_millones"] / cny, "tc": cny}
    except Exception as e:
        aviso(f"no se pudo traer el tipo de cambio del yuan ({e})")
    try:
        oro = float(get_json(ORO_URL)["price"])
        reservas_netas["oro"] = {"valor": rn_cfg["oro_onzas_millones"] * oro, "precio": oro}
    except Exception as e:
        aviso(f"no se pudo traer el precio del oro ({e})")

    # --- Bonos soberanos en USD MEP (con TIR, duration, paridad, spreads)
    bonos = []
    try:
        vivo = {x["symbol"]: x for x in get_json(BONOS_URL)}
        liq = dia_habil_siguiente(hoy)  # liquidación T+1
        ust = {}
        try:
            u = get_json(UST_URL)
            ust = {0.25: u["^IRX"]["close"][-1], 5: u["^FVX"]["close"][-1], 10: u["^TNX"]["close"][-1], 30: u["^TYX"]["close"][-1]}
        except Exception as e:
            aviso(f"no se pudo traer la curva del Tesoro de EE.UU. ({e})")

        def ust_a(md):
            if not ust:
                return None
            ks = sorted(ust)
            if md <= ks[0]:
                return ust[ks[0]]
            for a_, b_ in zip(ks, ks[1:]):
                if md <= b_:
                    return ust[a_] + (ust[b_] - ust[a_]) * (md - a_) / (b_ - a_)
            return ust[ks[-1]]

        for grupo, tickers in BONOS:
            filas_b = []
            for t in tickers:
                v, va = vivo.get(t + "D"), vivo.get(t)
                if not (v and v.get("c")):
                    continue
                px = float(v["c"])
                m = metricas_bono(t, px, liq) or {}
                fila = {"t": t, "ley": "NY" if t.startswith("GD") else "ARG", "precio": px,
                        "bid": v.get("px_bid"), "ask": v.get("px_ask"), "d1": v.get("pct_change"),
                        "vol": v.get("v"), "mep": (float(va["c"]) / px) if va and va.get("c") else None}
                fila.update(m)
                if m and ust:
                    fila["spread"] = (m["tir"] - ust_a(m["md"])) * 100
                filas_b.append(fila)
            bonos.append({"grupo": grupo, "filas": filas_b})
        # Desvío contra la curva: ajuste TIR = a + b·ln(duration) por legislación
        for ley in ("NY", "ARG"):
            todos = [f for g in bonos for f in g["filas"] if f.get("ley") == ley and f.get("md", 0) > 0.05]
            pts = [f for f in todos if f["t"] not in _NUEVOS]  # la curva se arma con los bonos de la reestructuración
            if len(pts) >= 3:
                xs = [math.log(f["md"]) for f in pts]
                ys = [f["tir"] for f in pts]
                mx_, my_ = sum(xs) / len(xs), sum(ys) / len(ys)
                b_ = sum((x - mx_) * (y - my_) for x, y in zip(xs, ys)) / sum((x - mx_) ** 2 for x in xs)
                a_ = my_ - b_ * mx_
                for f in todos:
                    f["vs_curva"] = (f["tir"] - (a_ + b_ * math.log(f["md"]))) * 100
    except Exception as e:
        aviso(f"no se pudieron traer los bonos ({e})")

    # --- ADRs argentinos
    adrs = []
    try:
        sp = get_json(SPARK_URL.format(s=",".join(t for t, _ in ADRS)))
        for t, nombre in ADRS:
            x = sp.get(t)
            if not x:
                continue
            c = [v for v in x["close"] if v is not None]
            pc = lambda a: (c[-1] / a - 1) * 100 if a else None
            adrs.append({"t": t, "nombre": nombre, "precio": c[-1],
                         "d1": pc(c[-2] if len(c) > 1 else None), "d5": pc(c[-6] if len(c) > 5 else None),
                         "m1": pc(c[-22] if len(c) > 21 else None), "ytd": pc(x.get("chartPreviousClose"))})
    except Exception as e:
        aviso(f"no se pudieron traer los ADRs ({e})")

    # --- Inflación INDEC
    inflacion = {}
    try:
        im = serie(VAR_INFL_M, (hoy - dt.timedelta(days=800)).isoformat())
        ia = serie(VAR_INFL_IA, (hoy - dt.timedelta(days=800)).isoformat())
        inflacion = {"mensual": [{"mes": x["fecha"][:7], "valor": x["valor"]} for x in im],
                     "interanual": [{"mes": x["fecha"][:7], "valor": x["valor"]} for x in ia]}
        # Acumulado del año: con el índice del INDEC (más preciso que sumar los mensuales redondeados)
        ult_mes = inflacion["mensual"][-1]["mes"]
        anio = ult_mes[:4]
        try:
            idx = {r[0][:7]: r[1] for r in get_json(SERIES_URL.format(ids="148.3_INIVELNAL_DICI_M_26"))["data"] if r[1]}
            inflacion["indice"] = sorted(idx.items())[-30:]
            base = idx.get(f"{int(anio) - 1}-12")
            meses_idx = sorted(k for k in idx if k.startswith(anio))
            nivel = idx[meses_idx[-1]] / base if base and meses_idx else 1.0
            desde_m = meses_idx[-1] if meses_idx else f"{int(anio) - 1}-12"
        except Exception:
            nivel, desde_m = 1.0, f"{int(anio) - 1}-12"
        for x in inflacion["mensual"]:
            if x["mes"].startswith(anio) and x["mes"] > desde_m:
                nivel *= 1 + x["valor"] / 100
        inflacion["acumulado"] = {"valor": (nivel - 1) * 100, "anio": anio}
    except Exception as e:
        aviso(f"no se pudo traer la inflación ({e})")
    # --- Históricos: dólar oficial y CCL ajustados por ITCRM, y Merval ajustado por inflación
    historicos = {}
    try:
        itc = itcrm_mensual()
        base = max(itc)
        a3500 = []
        for ini, fin in (("2012-12-01", "2019-12-31"), ("2020-01-01", hoy.isoformat())):
            j = get_json(f"https://api.bcra.gob.ar/estadisticas/v4.0/Monetarias/{VAR_MAYORISTA}?desde={ini}&hasta={fin}&limit=3000")
            a3500 += [(x["fecha"], float(x["valor"])) for x in j["results"][0]["detalle"]]
        a_m = promedio_mensual(a3500)
        ccl_m = promedio_mensual((x["fecha"], float(x["venta"])) for x in get_json(DOLARES_URL.format(casa="contadoconliqui")) if x.get("venta"))
        factor = a_m[base] / itc[base]
        serie_tcr = []
        for m in sorted(k for k in itc if k >= "2001-12"):
            of = itc[m] * factor
            if m in ccl_m and m in a_m:
                cc = of * ccl_m[m] / a_m[m]
            elif m < "2011-11":  # sin cepo: el CCL coincidía con el oficial
                cc = of
            else:
                cc = None
            serie_tcr.append([m, round(of, 1), round(cc, 1) if cc else None])
        historicos["tcr"] = {"base": base, "serie": serie_tcr}
    except Exception as e:
        aviso(f"no se pudo armar el dólar ajustado por ITCRM ({e})")
    try:
        ipc = ipc_empalmado()
        ult = max(ipc)
        r = get_json(MERVAL_URL)["chart"]["result"][0]
        mv = {}
        for t, c in zip(r["timestamp"], r["indicators"]["quote"][0]["close"]):
            if c:
                mv[dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m")] = c
        # meses sin IPC publicado todavía: se usa el último disponible
        serie_m = [[m, round(v * ipc[ult] / ipc.get(m, ipc[ult]))] for m, v in sorted(mv.items()) if m >= "2001-12"]
        historicos["merval"] = {"base": ult, "serie": serie_m}
    except Exception as e:
        aviso(f"no se pudo armar el Merval ajustado por inflación ({e})")

    try:  # reservas diarias desde dic-2007, por gobierno
        hist_r = {}
        for ini, fin in (("2007-12-01", "2013-12-31"), ("2014-01-01", "2019-12-31"), ("2020-01-01", hoy.isoformat())):
            for x in get_json(f"https://api.bcra.gob.ar/estadisticas/v4.0/Monetarias/{VAR_RESERVAS}?desde={ini}&hasta={fin}&limit=3000")["results"][0]["detalle"]:
                hist_r[x["fecha"]] = float(x["valor"])
        for x in mezclar(api_r, "reservas", SEED + manuales):  # suma los provisorios recientes
            hist_r.setdefault(x["fecha"], x["valor"])
        fs = sorted(hist_r)
        historicos["reservas"] = {"f": fs, "v": [round(hist_r[f] / 1000, 2) for f in fs]}
    except Exception as e:
        aviso(f"no se pudo armar la serie histórica de reservas ({e})")
    try:  # pobreza (EPH, 31 aglomerados): el dato con fecha AAAA-07 es el 1er semestre; AAAA-01, el 2do del año anterior
        pob = []
        for f, v in get_json(SERIES_URL.format(ids="64.2_POBLACION_NUA_0_0_34_74"))["data"]:
            if v is None:
                continue
            y, m = int(f[:4]), int(f[5:7])
            if f == "2016-07-01":
                et = "2T-16"  # primer dato tras la normalización del INDEC: solo el 2do trimestre
            elif m == 7:
                et = f"1S-{str(y)[2:]}"
            else:
                et = f"2S-{str(y - 1)[2:]}"
            if f >= "2016-01-01":  # la serie oficial previa usaba otra metodología y no es comparable
                pob.append([et, round(v * 100, 1), f, "INDEC"])
        # 2006-2015: serie comparable de CEDLAS-UNLP (metodología actual del INDEC con IPC alternativo 2007-2015)
        cedlas = [("2006-07-01", 43.9), ("2007-01-01", 40.6), ("2007-07-01", 38.8), ("2008-01-01", 37.8), ("2008-07-01", 36.7),
                  ("2009-01-01", 36.1), ("2009-07-01", 35.2), ("2010-01-01", 33.5), ("2010-07-01", 33.0), ("2011-01-01", 32.1),
                  ("2011-07-01", 30.0), ("2012-01-01", 28.0), ("2012-07-01", 27.7), ("2013-01-01", 27.9), ("2013-07-01", 27.3),
                  ("2014-01-01", 27.5), ("2014-07-01", 30.8), ("2015-01-01", 32.7), ("2015-07-01", 30.5), ("2016-01-01", None)]
        pre = []
        for f, v in cedlas:
            y, m = int(f[:4]), int(f[5:7])
            pre.append([f"1S-{str(y)[2:]}" if m == 7 else f"2S-{str(y - 1)[2:]}", v, f, "CEDLAS"])
        historicos["pobreza"] = pre + pob
    except Exception as e:
        aviso(f"no se pudo traer la serie de pobreza ({e})")
    try:
        historicos["personas"] = personas_humanas(hoy)
    except Exception as e:
        aviso(f"no se pudo armar la serie de compras de personas humanas ({e})")

    alimentos = {}
    try:
        ids = [IPC_GBA] + [x[2] for x in ALIMENTOS]
        filas = get_json(SERIES_URL.format(ids=",".join(ids)))["data"]
        ipc = {r[0]: r[1] for r in filas if r[1]}
        precios = {clave: {r[0]: r[i] for r in filas if r[i] is not None} for i, (clave, _, _) in enumerate(ALIMENTOS, start=2)}
        # El Excel del INDEC sale el mismo día del dato de inflación; datos.gob.ar tarda semanas más
        try:
            for clave, ser_i in precios_indec().items():
                precios.setdefault(clave, {}).update(ser_i)
        except ImportError:
            aviso("para tener el último mes de precios de alimentos instalá xlrd: pip install xlrd")
        except Exception as e:
            aviso(f"no se pudo leer el Excel de precios del INDEC ({e})")
        # Meses sin IPC GBA publicado en datos.gob.ar: se extiende con la inflación nacional del INDEC
        for x in inflacion.get("mensual", []):
            f = x["mes"] + "-01"
            ant = max((k for k in ipc if k < f), default=None)
            if f not in ipc and ant and ant[:7] == _mes_ant(x["mes"]):
                ipc[f] = ipc[ant] * (1 + x["valor"] / 100)
        base_mes = max(f for f in ipc if any(f in s_ for s_ in precios.values()))
        for clave, nombre, _ in ALIMENTOS:
            pts = [{"fecha": f, "nominal": v, "real": v * ipc[base_mes] / ipc[f]}
                   for f, v in sorted(precios.get(clave, {}).items()) if f in ipc and f <= base_mes]
            if pts:
                alimentos[clave] = {"nombre": nombre, "serie": pts}
        alimentos = {"base": base_mes, "productos": alimentos}
    except Exception as e:
        aviso(f"no se pudieron traer los precios de alimentos ({e})")
    extras = SEED + manuales
    compras = mezclar(api_c, "compra", extras)
    reservas = mezclar(api_r, "reservas", extras)
    datos = {
        "compras": compras,
        "reservas": reservas,
        "ultimoOficial": api_c[-1]["fecha"] if api_c else None,
        "generado": dt.datetime.now().strftime("%d/%m/%Y %H:%M"),
        "mesInicial": a.mes,
        "dolar": dolar,
        "alimentos": alimentos,
        "historicos": historicos,
        "escenarios": escenarios,
        "avisos": AVISOS,
        "inflacion": inflacion,
        "bonos": bonos,
        "adrs": adrs,
        "reservasNetas": reservas_netas,
        "indicadores": {k: v[-2:] for k, v in
                        {"reservas": reservas, "riesgo": mezclar(rp, "riesgo", extras), "minorista": tc, "tamar": tamar, "badlar": badlar}.items()},
    }

    salida = os.path.abspath(a.salida) if a.salida else SALIDA
    os.makedirs(os.path.dirname(salida), exist_ok=True)
    with open(salida, "w", encoding="utf-8") as f:
        f.write(PLANTILLA.replace("/*__DATOS__*/null", json.dumps(datos, ensure_ascii=False)))

    pend = [m["fecha"] for m in extras if m["fecha"] not in {x["fecha"] for x in api_c}]
    print(f"Datos oficiales hasta {api_c[-1]['fecha']}" + (f"; provisorios: {', '.join(sorted(set(pend)))}" if pend else ""))
    print(f"Gráfico generado: {salida}")
    if not a.no_abrir:
        webbrowser.open("file://" + salida.replace("\\", "/"))


PLANTILLA = r"""<!DOCTYPE html>
<html lang="es-AR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>Tablero económico argentino</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root{--bg:#edf6f0;--panel:#ffffff;--panel2:#f2f9f4;--ink:#0f2419;--muted:#4f6b5c;--line:#d3e5d9;
  --cur:#15803d;--prev:#86d6a0;--res:#2b9348;--risk:#0f766e;--fx:#14a38b;--tamar:#65a30d;--badlar:#16a34a;
  --good:#15803d;--bad:#dc2626;--goodbg:#dcf3e3;--badbg:#fde6e6;--hdr1:#0b5d3b;--hdr2:#3fae6e;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#07130d;--panel:#0e1f16;--panel2:#12281c;--ink:#e4f5ea;--muted:#8db3a0;--line:#1d3b2b;
  --cur:#22c55e;--prev:#a7f0bf;--res:#4ade80;--risk:#2dd4bf;--fx:#5eead4;--tamar:#a3e635;--badlar:#86efac;
  --good:#4ade80;--bad:#f87171;--goodbg:#0f3a24;--badbg:#45181d;--hdr1:#0b5d3b;--hdr2:#2f9e5f;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:400 15px/1.5 Archivo,"Segoe UI",system-ui,sans-serif}
.wrap{max-width:1240px;margin:0 auto;padding:0 16px 40px}
header{display:flex;flex-wrap:wrap;gap:12px 20px;align-items:center;justify-content:space-between;margin:0 -16px 6px;padding:22px 20px;
  background:linear-gradient(110deg,var(--hdr1),var(--hdr2));color:#fff;border-radius:0 0 18px 18px}
h1{margin:0;font:700 clamp(22px,3.4vw,32px)/1.15 Archivo,sans-serif;color:#fff}
header .sub{color:rgba(255,255,255,.85)}
header label{color:rgba(255,255,255,.9)!important}
header select{background:rgba(255,255,255,.18);color:#fff;border-color:rgba(255,255,255,.45)}
header select option{color:#111}
.sub,.status{color:var(--muted);font-size:14px;margin:4px 0 0}
.status{margin-top:12px}.status b{color:var(--ink)}
select,button{font:500 14px Archivo,sans-serif;color:var(--ink);background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px 12px;cursor:pointer}
:focus-visible{outline:2px solid var(--cur);outline-offset:2px}
.ind{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin:14px 0 0}
@media (max-width:700px){.ind{grid-template-columns:repeat(2,minmax(0,1fr))}}
.ind>div{--a:var(--cur);background:linear-gradient(180deg,color-mix(in srgb,var(--a) 14%,var(--panel)),var(--panel) 70%);
  border:1px solid color-mix(in srgb,var(--a) 35%,var(--line));border-top:4px solid var(--a);border-radius:12px;padding:12px 14px}
.ind .k{color:var(--a)}
.ind .v{font-size:clamp(19px,2.2vw,25px);margin-top:2px;white-space:nowrap}
.ind .d{font-size:13px;font-variant-numeric:tabular-nums;color:var(--muted)}
.pill{display:inline-block;margin-top:6px;font-size:12.5px;font-weight:600;border-radius:999px;padding:2px 9px;background:var(--panel2);color:var(--ink)}
.pill.good{background:var(--goodbg);color:var(--good)}.pill.bad{background:var(--badbg);color:var(--bad)}
.dol{display:grid;grid-template-columns:repeat(4,minmax(150px,1fr));gap:12px;margin:12px 0 0}
@media (max-width:900px){.dol{grid-template-columns:repeat(auto-fit,minmax(170px,1fr))}}
.dol>div{--a:var(--cur);background:linear-gradient(180deg,color-mix(in srgb,var(--a) 14%,var(--panel)),var(--panel) 70%);
  border:1px solid color-mix(in srgb,var(--a) 35%,var(--line));border-top:4px solid var(--a);border-radius:12px;padding:12px 14px}
.dol .k{color:var(--a)}.dol .v{font-size:clamp(21px,2.4vw,25px);margin-top:2px}
.dol .d{font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums}
.track{position:relative;height:12px;border-radius:999px;margin:16px 0 6px;
  background:linear-gradient(90deg,color-mix(in srgb,var(--good) 35%,var(--panel2)),color-mix(in srgb,var(--res) 30%,var(--panel2)) 60%,color-mix(in srgb,var(--bad) 45%,var(--panel2)))}
.track .mk{position:absolute;top:50%;width:18px;height:18px;border-radius:50%;background:var(--a);border:3px solid var(--panel);transform:translate(-50%,-50%);box-shadow:0 0 0 1px var(--a)}
.ends{display:flex;justify-content:space-between;font-size:12.5px;color:var(--muted);font-variant-numeric:tabular-nums}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:2px 0 12px}
.chips button{border-radius:999px;padding:6px 14px;background:var(--panel2);color:var(--muted)}
.chips button[aria-pressed="true"]{background:var(--cur);border-color:var(--cur);color:#fff}
.fstats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:12px}
.fstats div{background:var(--panel2);border-radius:10px;padding:10px 12px}
.fstats .v{font-size:20px;margin-top:2px}
.duo{display:grid;grid-template-columns:minmax(0,1fr);gap:0}
.esc thead th,.rnt thead th{color:var(--ink)!important}
.esc td,.esc th{padding:7px 8px}
.esc td,.esc th,.rnt td,.rnt th{text-align:right}
.esc th:first-child,.esc td:first-child,.rnt th:first-child,.rnt td:first-child{text-align:left}
.esc thead th{color:var(--ink);background:var(--panel2)}
.esc .ud.pos{background:var(--goodbg);color:var(--good);font-weight:600}
.esc .ud.neg{background:var(--badbg);color:var(--bad);font-weight:600}
.esc tr.hoy td{font-weight:700;border-top:2px solid var(--line)}
.esc tr.prob td{font-weight:700}
.esc tr.prob td.pv{background:color-mix(in srgb,var(--res) 30%,var(--panel));color:var(--ink)}
.rnt tr.tot td{font-weight:700;background:var(--panel2)}
.rnt td.neg{color:var(--bad)} .rnt td.pos{color:var(--good)}
.rnt td.lbl small{color:var(--muted);margin-left:6px;font-size:12px}
.mkt td,.mkt th{text-align:right;padding:6px 10px}
.mkt th:first-child,.mkt td:first-child,.mkt td.nm{text-align:left}
.mkt thead th{color:var(--ink)!important;background:var(--panel2)}
.mkt tr.grp td{background:var(--panel2);font-weight:700;color:var(--muted);font-size:13px;text-align:left}
.mkt td.t{font-weight:700}
.mkt td.nm{color:var(--muted)}
.mkt td.pc{font-variant-numeric:tabular-nums;font-weight:600}
.ind select.nm{font:600 13px Archivo,sans-serif;padding:1px 4px;border-radius:6px;margin:0 2px;background:var(--panel);color:var(--ink)}
.cmp{border-radius:14px;padding:16px 18px;margin:16px 0 0;border:1px solid color-mix(in srgb,var(--good) 40%,var(--line));
  background:linear-gradient(120deg,color-mix(in srgb,var(--good) 16%,var(--panel)),var(--panel))}
.cmp.neg{border-color:color-mix(in srgb,var(--bad) 40%,var(--line));background:linear-gradient(120deg,color-mix(in srgb,var(--bad) 16%,var(--panel)),var(--panel))}
.cmp h2{font:600 14px Archivo,sans-serif;color:var(--muted);margin:0 0 6px}
.cmp .big{font:700 clamp(19px,2.4vw,24px)/1.35 Archivo,sans-serif;margin:0}
.cmp .rows{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:6px 24px;margin-top:10px;font-size:14px;font-variant-numeric:tabular-nums}
.strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin:16px 0 18px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;border-top:4px solid var(--line)}
.k{font:600 13px "Archivo Narrow",Archivo,sans-serif;color:var(--muted)}
.k i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:0}
.v{font:700 clamp(22px,2.6vw,28px)/1.2 Archivo,sans-serif;margin-top:4px;font-variant-numeric:tabular-nums}
.n{font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums}
.up{color:var(--good)}.down{color:var(--bad)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin-bottom:18px}
.phead{display:flex;flex-wrap:wrap;justify-content:space-between;gap:10px;align-items:center;margin-bottom:10px}
h2{margin:0;font:600 17px Archivo,sans-serif}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:999px;overflow:hidden;background:var(--panel2)}
.seg button{border:0;border-radius:999px;padding:6px 14px;background:transparent;color:var(--muted)}
.seg button[aria-pressed="true"]{background:linear-gradient(110deg,var(--hdr1),var(--hdr2));color:#fff}
.chartbox{position:relative;height:380px}.chartbox.sm{height:230px}
.tablewrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:14px;font-variant-numeric:tabular-nums}
th,td{padding:7px 10px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
tbody tr:nth-child(even){background:var(--panel2)}
th:first-child,td:first-child{text-align:left}
th{font:600 13px "Archivo Narrow",Archivo,sans-serif;color:var(--muted)}
thead th:nth-child(2){color:var(--cur)}thead th:nth-child(3){color:color-mix(in srgb,var(--prev) 65%,var(--ink))}
tfoot th{color:var(--ink);font-size:14px;border-top:2px solid var(--line)}
td small{color:var(--muted);margin-left:6px}
.tag{font-size:11px;font-weight:600;color:var(--res);border:1px solid var(--res);border-radius:4px;padding:0 4px;margin-left:6px}
footer{font-size:12.5px;color:var(--muted)}
@media (max-width:640px){.chartbox{height:300px}header{border-radius:0 0 14px 14px}}

/* ===== Estilo terminal ===== */
:root,:root:not([data-theme="light"]){--bg:#000;--panel:#070707;--panel2:#0f0f0f;--ink:#e8e8e8;--muted:#7c7c7c;--line:#262626;
  --cur:#ffb000;--prev:#3a8ee6;--res:#ffb000;--risk:#ffb000;--fx:#ffb000;--tamar:#ffb000;--badlar:#ffb000;
  --good:#00d26a;--bad:#ff3b3b;--goodbg:transparent;--badbg:transparent;--hdr1:#000;--hdr2:#000;--lbl:#5fb3ff;--amb:#ffb000;color-scheme:dark}
body{background:#000;color:var(--ink);font:12.5px/1.4 "IBM Plex Mono",Consolas,"Courier New",monospace}
.wrap{max-width:1500px;padding:0 8px 30px}
header{background:#111;border-radius:0;margin:0 -8px;padding:6px 10px;border-bottom:1px solid var(--amb);display:flex;flex-wrap:wrap;align-items:center;gap:6px 14px}
.cmdk{background:var(--amb);color:#000;font-weight:700;padding:1px 8px}
h1{font:700 14px "IBM Plex Mono",monospace;color:var(--amb);letter-spacing:.05em;margin:0}
header .sub{color:#777;margin:0;font-size:12px}
.hdr-r{margin-left:auto;display:flex;align-items:center;gap:8px;color:#9aa;font-size:12px}
header select{background:#000;color:var(--amb);border:1px solid #444;border-radius:0;font:600 12px "IBM Plex Mono",monospace;padding:2px 6px}
header select option{color:#fff;background:#000}
.live{color:var(--good)}
.fk{display:flex;flex-wrap:wrap;gap:4px;margin:0 -8px;padding:6px 8px;border-bottom:1px solid #222;background:#000}
.fk button{background:#1c1c1c;color:#ddd;border:1px solid #2e2e2e;border-radius:0;padding:8px 16px;font:600 14px "IBM Plex Mono",monospace;cursor:pointer;min-height:38px}
.fk button:hover{background:#2a2a2a}
@media (max-width:700px){.fk button{padding:8px 11px;font-size:13px;flex:1 1 auto}}
.fk button b{color:var(--amb);margin-right:4px}
.fk button[aria-pressed="true"]{background:var(--amb);color:#000}.fk button[aria-pressed="true"] b{color:#000}
.tick{white-space:nowrap;overflow-x:auto;margin:0 -8px;padding:5px 10px;border-bottom:1px solid #222;background:#050505;color:#ccc;font-size:12px}
.tick b{color:var(--amb);margin-right:4px}.tick span.t{margin-right:22px}
.status{color:#777;font-size:11.5px;margin:6px 0 0}.status b{color:var(--ink)}
.panel,.stat,.ind>div,.dol>div,.cmp,.cmp.neg{background:var(--panel)!important;border:1px solid var(--line)!important;border-radius:0!important;box-shadow:none}
.ind>div,.dol>div{border-top:1px solid var(--line)!important;padding:8px 10px}
.stat{border-top:1px solid var(--line)!important}
.panel{padding:0 0 8px;margin-bottom:6px}
.phead{background:#1a1300;margin:0 0 8px;padding:4px 10px;border-bottom:1px solid #2a2200}
.phead h2,.cmp h2{font:700 11.5px "IBM Plex Mono",monospace;color:var(--amb);letter-spacing:.06em;text-transform:uppercase;margin:0}
.panel>.chartbox,.panel>p,.panel>.tablewrap,.panel>.chips,.panel>.fstats,.panel>div:not(.phead){margin-left:10px;margin-right:10px}
.k,.ind .k,.dol .k{font:500 11px "IBM Plex Mono",monospace;color:var(--lbl)!important;text-transform:uppercase;letter-spacing:.03em}
.k i{border-radius:0}
.v,.ind .v,.dol .v{font:700 19px "IBM Plex Mono",monospace;color:#fff}
.n,.d,.ind .d,.dol .d{color:#888;font-size:11.5px}
.pill{border-radius:0;background:transparent!important;padding:0;font-weight:600;color:#888}
.pill.good{color:var(--good)}.pill.bad{color:var(--bad)}
.up{color:var(--good)!important}.down{color:var(--bad)!important}
.ind,.dol{gap:6px;margin-top:6px}.strip{gap:6px;margin:6px 0}
.cmp{margin-top:6px;padding:8px 10px}
.cmp .big{font:600 15px/1.4 "IBM Plex Mono",monospace;color:#fff}
.cmp .rows{font-size:12px;color:#bbb}
.seg{border-radius:0;border:1px solid #333;background:#000}
.seg button{border-radius:0;padding:2px 9px;font:500 11px "IBM Plex Mono",monospace;color:#aaa}
.seg button[aria-pressed="true"]{background:var(--amb)!important;color:#000}
.chips button{border-radius:0;background:#1c1c1c;border:0;color:#ccc;font:500 11.5px "IBM Plex Mono",monospace}
.chips button[aria-pressed="true"]{background:var(--amb);color:#000}
.fstats div{background:#0f0f0f;border-radius:0}
table{font-size:12px}
th{color:var(--lbl)!important;font:500 11px "IBM Plex Mono",monospace;background:transparent!important;border-bottom:1px solid #222}
td{border-bottom:0;padding:3px 10px}
tbody tr:nth-child(even) td{background:#0d0d0d}
.mkt td.t,.esc td:first-child{color:var(--amb);font-weight:600}
.mkt td.nm{color:#999}
.mkt td.pc{background:transparent!important}
.mkt tr.grp td{background:#111!important;color:var(--amb);font-size:11px;letter-spacing:.05em;text-transform:uppercase}
.esc .ud.pos,.esc .ud.neg{background:transparent}
.esc tr.prob td.pv{background:transparent;color:var(--amb)}
.rnt tr.tot td{background:#111}
.tag{border-radius:0;color:var(--amb);border-color:var(--amb)}
footer{color:#555;font-size:10.5px;margin-top:6px}
select.nm{background:#000!important;color:var(--amb)!important;border:1px solid #444!important;border-radius:0!important;font:600 11px "IBM Plex Mono",monospace!important}
[data-sec].oculto{display:none!important}
.gobs{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px;margin:0 10px 8px}
@media (max-width:800px){.gobs{grid-template-columns:repeat(2,minmax(0,1fr))}}
.gobs>div{background:#0d0d0d;border:1px solid var(--line);border-top:3px solid var(--c);padding:8px 10px}
.gobs .nm{color:var(--c);font-weight:700;font-size:12px;letter-spacing:.04em}
.gobs .fe{color:#777;font-size:10.5px}
.gobs .pc{font:700 24px "IBM Plex Mono",monospace;color:var(--c);margin-top:4px}
.gobs .ab{color:#fff;font-weight:600;font-size:12px}
.gobs .rg{color:#888;font-size:11px}
.ley{display:inline-block;border:1px solid #555;color:#aaa;font-size:10.5px;padding:0 4px}.ley.ny{border-color:var(--amb);color:var(--amb)}
#tbon td.tir{color:var(--amb);font-weight:600}
#tbon tr[data-bono]{cursor:pointer}#tbon tr.sel td{background:#1a1300!important}
#tbon td,#tbon th{padding:4px 8px}
.panel>p.n,.panel>p{padding:0 10px}
.mkt td:nth-child(2),.esc td:nth-child(2n){color:#fff;font-weight:600}
.duo{gap:0}
@media (max-width:700px){.hdr-r{margin-left:0}header .sub{display:none}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <span class="cmdk">ARG</span>
    <h1>TABLERO ECONÓMICO ARGENTINO &lt;GO&gt;</h1>
    <span class="sub">BCRA | FX | TASAS | BONOS | ADRS | IPC</span>
    <span class="hdr-r"><label for="mes">MES</label> <select id="mes"></select> <span id="clk"></span> <span class="live">● LIVE</span></span>
  </header>
  <nav class="fk" id="fk" aria-label="Secciones">
    <button type="button" data-tab="todo" aria-pressed="true"><b>1)</b>TODO</button>
    <button type="button" data-tab="resumen" aria-pressed="false"><b>2)</b>RESUMEN</button>
    <button type="button" data-tab="compras" aria-pressed="false"><b>3)</b>COMPRAS</button>
    <button type="button" data-tab="reservas" aria-pressed="false"><b>4)</b>RESERVAS</button>
    <button type="button" data-tab="fx" aria-pressed="false"><b>5)</b>FX/BANDAS</button>
    <button type="button" data-tab="bonos" aria-pressed="false"><b>6)</b>BONOS</button>
    <button type="button" data-tab="adrs" aria-pressed="false"><b>7)</b>ADRS</button>
    <button type="button" data-tab="ipc" aria-pressed="false"><b>8)</b>IPC</button>
  </nav>
  <div class="tick" id="tick"></div>
  <p class="status" id="status"></p>
  <section data-sec="resumen" class="ind" id="ind" aria-label="Indicadores del día"></section>
  <section data-sec="resumen fx" class="dol" id="dol" aria-label="Dólar, bandas y brecha"></section>
  <section data-sec="resumen compras" class="cmp" id="cmp" aria-live="polite"></section>
  <section data-sec="resumen compras" class="strip" id="stats"></section>
  <section data-sec="compras" class="panel">
    <div class="phead">
      <h2 id="t1">Compras por rueda</h2>
      <div class="seg" role="group" aria-label="Modo del gráfico">
        <button type="button" data-mode="daily" aria-pressed="true">Diarias</button>
        <button type="button" data-mode="cum" aria-pressed="false">Acumulado</button>
      </div>
    </div>
    <div class="chartbox"><canvas id="c1"></canvas></div>
  </section>
  <section data-sec="compras" class="panel">
    <div class="phead"><h2>Compras del BCRA por mes, últimos 12 meses</h2><span class="n" id="m12tot"></span></div>
    <div class="chartbox" style="height:320px"><canvas id="c6" aria-label="Compras netas de divisas del BCRA por mes en los últimos 12 meses"></canvas></div>
    <p class="n" id="m12n" style="margin:8px 0 0"></p>
  </section>

  <section data-sec="reservas" class="panel">
    <div class="phead"><h2>Reservas internacionales brutas</h2></div>
    <div class="chartbox sm"><canvas id="c2"></canvas></div>
  </section>
  <section data-sec="fx" class="panel">
    <div class="phead"><h2>Bandas cambiarias y dólar mayorista</h2>
      <div class="seg" role="group" aria-label="Período de bandas">
        <button type="button" data-b="90" aria-pressed="true">3 meses</button>
        <button type="button" data-b="365" aria-pressed="false">1 año</button>
        <button type="button" data-b="0" aria-pressed="false">Todo</button>
      </div></div>
    <div class="chartbox sm" style="height:300px"><canvas id="c3" aria-label="Piso y techo de la banda cambiaria con el dólar mayorista"></canvas></div>
  </section>

  <section data-sec="fx" class="panel">
    <div class="phead"><h2>Brecha CCL / MEP</h2>
      <div class="seg" role="group" aria-label="Período de la brecha">
        <button type="button" data-cm="90" aria-pressed="false">3 meses</button>
        <button type="button" data-cm="365" aria-pressed="true">1 año</button>
        <button type="button" data-cm="0" aria-pressed="false">2 años</button>
      </div></div>
    <div class="chartbox sm" style="height:280px"><canvas id="c5" aria-label="Brecha entre el dólar contado con liquidación y el MEP, en porcentaje"></canvas></div>
    <p class="n" id="ncm" style="margin:8px 0 0"></p>
  </section>

  <section data-sec="ipc" class="panel" id="pinf">
    <div class="phead"><h2>Inflación mensual (INDEC)</h2><span class="n" id="infsub"></span></div>
    <div class="chartbox" style="height:300px"><canvas id="c8" aria-label="Inflación mensual del INDEC de los últimos 24 meses, con la interanual"></canvas></div>
  </section>

  <section data-sec="bonos" class="panel" id="pbon">
    <div class="phead"><h2>Bonos soberanos en dólares (MEP)</h2></div>
    <div class="tablewrap"><table class="mkt" id="tbon"></table></div>
    <p class="n" style="margin:8px 0 0">Precio sucio cada 100 nominales en USD MEP (especie D), liquidación T+1. TIR efectiva anual; vs curva: desvío contra la curva de los bonos de la reestructuración de su misma ley; spread: contra el Tesoro de EE.UU. a igual duration. Hacé clic en un bono para ver sus flujos pendientes. Fuentes: data912, Yahoo Finance.</p>
    <div id="flujos" style="margin-top:8px"></div>
  </section>

  <div class="duo">
    <section data-sec="bonos" class="panel" id="pk">
      <div class="phead"><h2>Escenarios: precio según probabilidad K</h2></div>
      <div class="tablewrap"><table class="esc" id="tesc"></table></div>
      <p class="n" id="nesc" style="margin:8px 0 0"></p>
    </section>
    <section data-sec="reservas" class="panel" id="rn">
      <div class="phead"><h2>Reservas netas y convertibilidad</h2></div>
      <div class="tablewrap"><table class="rnt" id="trn"></table></div>
      <p class="n" id="nrn" style="margin:8px 0 0"></p>
    </section>
  </div>

  <section data-sec="reservas" class="panel" id="pgob">
    <div class="phead"><h2>Reservas internacionales del BCRA por gobierno</h2></div>
    <div class="gobs" id="gobs"></div>
    <div class="chartbox" style="height:420px"><canvas id="c14" aria-label="Reservas internacionales diarias del BCRA desde diciembre de 2007, coloreadas por gobierno"></canvas></div>
    <p class="n" id="ngob" style="margin:8px 0 0"></p>
  </section>

  <section data-sec="fx compras" class="panel" id="pph">
    <div class="phead"><h2>Compras netas de dólares de personas humanas</h2></div>
    <div class="chartbox" style="height:320px"><canvas id="c13" aria-label="Compras netas mensuales de moneda extranjera de las personas humanas, en millones de dólares"></canvas></div>
    <p class="n" id="nph" style="margin:8px 0 0"></p>
  </section>

  <section data-sec="fx" class="panel" id="ptcr">
    <div class="phead"><h2 id="tcrt">Dólar oficial y CCL ajustados por ITCRM</h2></div>
    <div class="chartbox" style="height:380px"><canvas id="c11" aria-label="Dólar oficial y contado con liquidación ajustados por tipo de cambio real multilateral desde 2001"></canvas></div>
    <p class="n" id="ntcr" style="margin:8px 0 0"></p>
  </section>

  <section data-sec="adrs" class="panel" id="padr">
    <div class="phead"><h2>ADRs argentinos en Nueva York</h2></div>
    <div class="tablewrap"><table class="mkt" id="tadr"></table></div>
    <p class="n" style="margin:8px 0 0">Precio en USD y variación porcentual (cierre o último precio disponible). Fuente: Yahoo Finance.</p>
  </section>

  <section data-sec="adrs" class="panel" id="pmerv">
    <div class="phead"><h2 id="mervt">Merval ajustado por inflación desde diciembre 2001</h2></div>
    <div class="chartbox" style="height:340px"><canvas id="c12" aria-label="Índice Merval ajustado por inflación desde diciembre de 2001"></canvas></div>
    <p class="n" id="nmerv" style="margin:8px 0 0"></p>
  </section>

  <section data-sec="ipc" class="panel" id="alim">
    <div class="phead"><h2 id="t4">Precios de alimentos ajustados por inflación</h2>
      <div class="seg" role="group" aria-label="Valores" id="realnom">
        <button type="button" data-v="real" aria-pressed="true">Ajustado</button>
        <button type="button" data-v="nominal" aria-pressed="false">Nominal</button>
      </div></div>
    <div class="chips" id="chips" role="group" aria-label="Producto"></div>
    <div class="fstats" id="fstats"></div>
    <div class="chartbox" style="height:320px"><canvas id="c4"></canvas></div>
    <p class="n" id="fnote" style="margin:8px 0 0"></p>
  </section>

  <section data-sec="ipc" class="panel" id="ppob">
    <div class="phead"><h2>Pobreza: personas bajo la línea de pobreza (%), últimos 20 años</h2></div>
    <div class="gobs" id="pobcards"></div>
    <div class="chartbox" style="height:340px"><canvas id="c16" aria-label="Porcentaje de personas bajo la línea de pobreza por semestre, según la EPH del INDEC"></canvas></div>
    <p class="n" id="npob" style="margin:8px 0 0"></p>
  </section>

  <section data-sec="ipc" class="panel" id="canasta">
    <div class="phead"><h2>Canasta: precios reales en índice (base 100)</h2>
      <div class="seg" role="group" aria-label="Desde">
        <button type="button" data-cb="2023-12" aria-pressed="true">Desde dic-23</button>
        <button type="button" data-cb="12" aria-pressed="false">12 meses</button>
        <button type="button" data-cb="2016-12" aria-pressed="false">Desde 2016</button>
      </div></div>
    <div class="chartbox" style="height:340px"><canvas id="c15" aria-label="Evolución real de los precios de la canasta en índice base 100"></canvas></div>
    <div class="tablewrap"><table class="mkt" id="tcan"></table></div>
    <p class="n" id="ncan" style="margin:8px 0 0"></p>
  </section>

  <div id="avisos"></div>
  <footer>Fuente: API de Estadísticas Monetarias v4.0 del BCRA (variable 78, compras de divisas; 1, reservas; 4, dólar minorista promedio vendedor; 44 y 7, TAMAR y BADLAR de bancos privados en TNA). Riesgo país: índice EMBI de JP Morgan vía ArgentinaDatos. Bandas: variables 1187 y 1188 del BCRA. Alimentos: precios medios del IPC del INDEC en GBA, deflactados por IPC GBA (datos.gob.ar). Brechas: MEP, CCL y blue (ArgentinaDatos, precio de venta) contra el mayorista del mismo día (variable 5). Las tasas se publican con un día hábil de rezago. Los días marcados como provisorios salen del cierre informado por el BCRA o de tu carga manual, y se reemplazan por el dato oficial en la próxima ejecución. Las ruedas se comparan por orden dentro de cada mes, no por fecha.</footer>
</div>
<script>
const D = /*__DATOS__*/null;
const MESES=["enero","febrero","marzo","abril","mayo","junio","julio","agosto","septiembre","octubre","noviembre","diciembre"];
const fmt=(v,d=0)=>v.toLocaleString("es-AR",{minimumFractionDigits:d,maximumFractionDigits:d});
const ym=f=>f.slice(0,7), dmy=f=>f.split("-").reverse().join("/");
const lab=k=>{const[y,m]=k.split("-");const s=MESES[+m-1];return s[0].toUpperCase()+s.slice(1)+" "+y;};
const shift=(k,n)=>{let[y,m]=k.split("-").map(Number);m+=n;while(m<1){m+=12;y--;}while(m>12){m-=12;y++;}return y+"-"+String(m).padStart(2,"0");};
const css=v=>getComputedStyle(document.documentElement).getPropertyValue(v).trim();
let mode="daily",c1,c2;

const keys=[...new Set(D.compras.map(x=>ym(x.fecha)))].sort().reverse().slice(0,12);
let sel=keys.includes(D.mesInicial)?D.mesInicial:keys[0];
const s=document.getElementById("mes");
s.innerHTML=keys.map(k=>`<option value="${k}"${k===sel?" selected":""}>${lab(k)}</option>`).join("");
s.onchange=e=>{sel=e.target.value;render();};
const ultimo=D.compras[D.compras.length-1].fecha;
document.getElementById("status").innerHTML=`Datos oficiales hasta el <b>${dmy(D.ultimoOficial)}</b>${ultimo!==D.ultimoOficial?`, con provisorios hasta el <b>${dmy(ultimo)}</b>`:""}. Generado el ${D.generado}.`;

function comprasCards(){
  const C=D.compras||[]; if(!C.length) return "";
  const u=C[C.length-1], a=C.length>1?C[C.length-2]:null, mes=u.fecha.slice(0,7);
  const delMes=C.filter(x=>x.fecha.startsWith(mes)), tot=delMes.reduce((t,x)=>t+x.valor,0);
  const M=["enero","febrero","marzo","abril","mayo","junio","julio","agosto","septiembre","octubre","noviembre","diciembre"][+mes.slice(5)-1];
  let pill="";
  if(a){const d=u.valor-a.valor; pill=Math.abs(d)<0.05?`<span class="pill">Igual que el ${dmy(a.fecha).slice(0,5)}</span>`:`<span class="pill ${d>0?"good":"bad"}">${d>0?"▲":"▼"} ${fmt(Math.abs(d),1)} M vs ${dmy(a.fecha).slice(0,5)}</span>`;}
  return `<div style="--a:var(--cur)"><div class="k">Compra BCRA del día (USD)</div><div class="v ${u.valor<0?"down":""}">${fmt(u.valor,1)} M</div><div class="d">Rueda ${delMes.length} · ${dmy(u.fecha).slice(0,5)}${u.prov?" (provisorio)":""}</div>${pill}</div>`+
    `<div style="--a:var(--cur)"><div class="k">Compras de ${M} (USD)</div><div class="v ${tot<0?"down":""}">${fmt(tot,1)} M</div><div class="d">${delMes.length} ruedas · prom. ${fmt(tot/delMes.length,1)} M</div></div>`;
}
function inflCards(){
  const F=D.inflacion||{}, M=F.mensual||[];
  if(!M.length) return "";
  const mesTxt=k=>{const[y,m]=k.split("-");return MESES[+m-1];};
  const u=M[M.length-1], a=M.length>1?M[M.length-2]:null, anio=u.mes.slice(0,4);
  const acum=F.acumulado&&F.acumulado.anio===anio?F.acumulado.valor:(M.filter(x=>x.mes.startsWith(anio)).reduce((f,x)=>f*(1+x.valor/100),1)-1)*100;
  const ia=(F.interanual||[]).slice(-1)[0];
  let pill="";
  if(a){const d=u.valor-a.valor; pill=Math.abs(d)<0.005?`<span class="pill">Igual que ${mesTxt(a.mes)}</span>`:`<span class="pill ${d>0?"good":"bad"}">${d>0?"▲":"▼"} ${fmt(Math.abs(d),1)} pp vs ${mesTxt(a.mes)}</span>`;}
  return `<div style="--a:var(--bad)"><div class="k">IPC ${mesTxt(u.mes)} (INDEC)</div><div class="v">${fmt(u.valor,1)} %</div><div class="d">${ia?"Interanual "+fmt(ia.valor,1)+" %":""}</div>${pill}</div>`+
    `<div style="--a:var(--bad)" id="infn"><div class="k">IPC últimos <select class="nm" id="nmeses" aria-label="Cantidad de meses">${Array.from({length:12},(_,i)=>`<option value="${i+1}"${i===11?" selected":""}>${i+1}</option>`).join("")}</select> meses</div><div class="v" id="infnv">–</div><div class="d" id="infnd"></div></div>`+
    `<div style="--a:var(--bad)"><div class="k">IPC acumulado ${anio}</div><div class="v">${fmt(acum,1)} %</div><div class="d">Enero a ${mesTxt(u.mes)}</div></div>`;
}
function inflUltimos(n){
  const F=D.inflacion||{}, M=F.mensual||[]; if(!M.length) return null;
  const idx=Object.fromEntries(F.indice||[]), meses=M.slice(-n), fin=meses[meses.length-1].mes;
  const prev=k=>{let[y,m]=k.split("-").map(Number);m--;if(!m){m=12;y--;}return y+"-"+String(m).padStart(2,"0");};
  const base=prev(meses[0].mes);
  let nivel=1, desde=base;
  if(idx[base]){const conIdx=meses.map(x=>x.mes).filter(k=>idx[k]); if(conIdx.length){desde=conIdx[conIdx.length-1]; nivel=idx[desde]/idx[base];}}
  for(const x of meses) if(x.mes>desde) nivel*=1+x.valor/100;
  const ia=(F.interanual||[]).find(x=>x.mes===fin);
  if(n===12&&ia) return {valor:ia.valor, desde:meses[0].mes, hasta:fin};  // dato oficial interanual
  return {valor:(nivel-1)*100, desde:meses[0].mes, hasta:fin};
}
function pintarUltimos(){
  const sel=document.getElementById("nmeses"); if(!sel) return;
  const r=inflUltimos(+sel.value); if(!r) return;
  const mes=k=>{const[y,m]=k.split("-");return MESES[+m-1].slice(0,3)+" "+y.slice(2);};
  document.getElementById("infnv").textContent=fmt(r.valor,1)+" %";
  document.getElementById("infnd").textContent=r.desde===r.hasta?mes(r.hasta):`${mes(r.desde)} a ${mes(r.hasta)}`;
}
(function indicadores(){
  const I=D.indicadores, out=[];
  const card=(tit,arr,f,unidad,dec,acc,bueno)=>{
    if(!arr||!arr.length) return `<div style="--a:var(${acc})"><div class="k">${tit}</div><div class="v">s/d</div></div>`;
    const u=arr[arr.length-1], a=arr.length>1?arr[arr.length-2]:null;
    let d="";
    if(a){const x=u.valor-a.valor;
      const c=x===0?"":(x>0?"good":"bad");  // estilo terminal: color = dirección
      d=x===0?`<span class="pill">Sin cambios vs ${dmy(a.fecha).slice(0,5)}</span>`
        :`<span class="pill ${c}">${x>0?"▲":"▼"} ${fmt(Math.abs(x),dec)} ${unidad} vs ${dmy(a.fecha).slice(0,5)}</span>`;}
    return `<div style="--a:var(${acc})"><div class="k">${tit}</div><div class="v">${f(u.valor)}</div><div class="d">Al ${dmy(u.fecha).slice(0,5)}${u.prov?" (provisorio)":""}</div>${d}</div>`;};
  document.getElementById("ind").innerHTML=comprasCards()+
    card("Reservas brutas (USD)",I.reservas,v=>`${fmt(v)} M`,"M",0,"--res",true)+
    card("Riesgo país",I.riesgo,v=>`${fmt(v)} pb`,"pb",0,"--risk",false)+
    card("Dólar minorista",I.minorista,v=>`$ ${fmt(v,2)}`,"$",2,"--fx",null)+
    card("TAMAR privados (TNA)",I.tamar,v=>`${fmt(v,2)} %`,"pp",2,"--tamar",null)+
    card("BADLAR privados (TNA)",I.badlar,v=>`${fmt(v,2)} %`,"pp",2,"--badlar",null)+inflCards();
})();
(function dolar(){
  const X=D.dolar||{}, el=document.getElementById("dol");
  const M=X.mayorista||[];
  const u=M[M.length-1], a=M.length>1?M[M.length-2]:null;
  const dm=a?u.valor-a.valor:0;
  const pill=a?`<span class="pill">${dm===0?"Sin cambios":(dm>0?"▲":"▼")+" $ "+fmt(Math.abs(dm),2)} vs ${dmy(a.fecha).slice(0,5)}</span>`:"";
  let banda="";
  if(X.piso&&X.techo){
    const hi=X.techo.valor, falta=hi-u.valor, pf=(hi/u.valor-1)*100;
    banda=`<div class="d" style="margin-top:8px">Banda del día: $ ${fmt(X.piso.valor,2)} a $ ${fmt(hi,2)}</div>
      <div class="d">Al techo le faltan <b style="color:var(--ink)">$ ${fmt(falta,2)} (${fmt(pf,1)} %)</b></div>`;
  }
  const br=(tit,arr,acc)=>{
    if(!arr||!arr.length) return `<div style="--a:var(${acc})"><div class="k">${tit}</div><div class="v">s/d</div></div>`;
    const h=arr[arr.length-1], y=arr.length>1?arr[arr.length-2]:null;
    let p="";
    if(y&&h.brecha!=null&&y.brecha!=null){const d=h.brecha-y.brecha;
      p=Math.abs(d)<0.005?`<span class="pill">Sin cambios vs ${dmy(y.fecha).slice(0,5)}</span>`
        :`<span class="pill ${d>0?"good":"bad"}">${d>0?"▲":"▼"} ${fmt(Math.abs(d),2)} pp vs ${dmy(y.fecha).slice(0,5)}</span>`;}
    return `<div style="--a:var(${acc})"><div class="k">${tit}</div><div class="v">${h.brecha!=null?fmt(h.brecha,1)+" %":"s/d"}</div><div class="d">$ ${fmt(h.precio,2)} al ${dmy(h.fecha).slice(0,5)}</div>${p}</div>`;};
  const B=X.brechas||{};
  let cm="";
  const CM=X.ccl_mep||[];
  if(CM.length){const h=CM[CM.length-1], y=CM.length>1?CM[CM.length-2]:null; let p="";
    if(y){const d=h.valor-y.valor; p=Math.abs(d)<0.005?`<span class="pill">Sin cambios vs ${dmy(y.fecha).slice(0,5)}</span>`
      :`<span class="pill ${d>0?"good":"bad"}">${d>0?"▲":"▼"} ${fmt(Math.abs(d),2)} pp vs ${dmy(y.fecha).slice(0,5)}</span>`;}
    cm=`<div style="--a:var(--badlar)"><div class="k">Brecha CCL / MEP</div><div class="v">${fmt(h.valor,2)} %</div><div class="d">CCL $ ${fmt(h.ccl,2)} · MEP $ ${fmt(h.mep,2)}</div>${p}</div>`;}
  el.innerHTML=br("Brecha MEP",B.mep,"--fx")+br("Brecha CCL",B.ccl,"--risk")+br("Brecha blue",B.blue,"--tamar")+cm;
})();
// Gráfico de bandas
let c3, bDias=90;
function bandas(){
  const H=(D.dolar||{}).hist; const box=document.getElementById("c3");
  if(!H||!H.techo||!H.techo.length){box.parentElement.innerHTML='<p class="n">Sin datos de bandas.</p>';return;}
  const fechas=[...new Set([...H.techo.map(x=>x.fecha),...H.mayorista.map(x=>x.fecha)])].sort();
  let desde=fechas[0];
  if(bDias){const d=new Date(H.mayorista[H.mayorista.length-1].fecha);d.setDate(d.getDate()-bDias);desde=d.toISOString().slice(0,10);}
  const L=fechas.filter(f=>f>=desde), mp=a=>Object.fromEntries(a.map(x=>[x.fecha,x.valor]));
  const T=mp(H.techo),P=mp(H.piso),M=mp(H.mayorista);
  const m=css("--muted"),g=css("--line"),font={family:"IBM Plex Mono",size:11};
  c3?.destroy();
  c3=new Chart(box,{type:"line",data:{labels:L.map(f=>dmy(f).slice(0,5)+(bDias&&bDias<=90?"":"/"+f.slice(2,4))),datasets:[
    {label:"Techo",data:L.map(f=>T[f]??null),borderColor:css("--bad"),borderWidth:2,borderDash:[6,4],pointRadius:0,spanGaps:true,fill:false},
    {label:"Piso",data:L.map(f=>P[f]??null),borderColor:css("--good"),backgroundColor:css("--good")+"18",borderWidth:2,borderDash:[6,4],pointRadius:0,spanGaps:true,fill:"-1"},
    {label:"Mayorista",data:L.map(f=>M[f]??null),borderColor:css("--ink"),borderWidth:3,pointRadius:0,spanGaps:false,tension:.15}]},
    options:{responsive:true,maintainAspectRatio:false,interaction:{mode:"index",intersect:false},
      plugins:{legend:{labels:{color:m,boxWidth:12,font}},tooltip:{callbacks:{label:it=>it.raw==null?null:`${it.dataset.label}: $ ${fmt(it.raw,2)}`}}},
      scales:{x:{grid:{display:false},ticks:{color:m,font,maxTicksLimit:8,maxRotation:0}},y:{grid:{color:g},ticks:{color:m,font,callback:v=>"$ "+fmt(v)}}}}});
}
document.querySelectorAll("[data-b]").forEach(b=>b.onclick=()=>{bDias=+b.dataset.b;document.querySelectorAll("[data-b]").forEach(x=>x.setAttribute("aria-pressed",x===b));bandas();});
bandas();

// Alimentos
let c4, prod=null, vista="real";
function alimentos(){
  const A=D.alimentos, panel=document.getElementById("alim");
  if(!A||!A.productos||!Object.keys(A.productos).length){panel.style.display="none";return;}
  const keys=Object.keys(A.productos); if(!prod) prod=keys[0];
  document.getElementById("chips").innerHTML=keys.map(k=>`<button type="button" data-p="${k}" aria-pressed="${k===prod}">${A.productos[k].nombre}</button>`).join("");
  document.querySelectorAll("[data-p]").forEach(b=>b.onclick=()=>{prod=b.dataset.p;alimentos();});
  const S=A.productos[prod].serie, v=S.map(x=>x[vista]), u=S[S.length-1];
  const mes=f=>{const[y,m]=f.split("-");return MESES[+m-1].slice(0,3)+" "+y;};
  const hace=S.find(x=>x.fecha===shift(u.fecha.slice(0,7),-12)+"-01");
  const mx=S.reduce((a,b)=>b.real>a.real?b:a), mn=S.reduce((a,b)=>b.real<a.real?b:a);
  const prom=S.reduce((s,x)=>s+x.real,0)/S.length;
  const pc=(a,b)=>{const p=(a/b-1)*100;return `<b class="${p>=0?"up":"down"}">${p>0?"+":""}${fmt(p,1)} %</b>`;};
  document.getElementById("fstats").innerHTML=
    `<div><div class="k">Precio ${mes(u.fecha)}</div><div class="v">$ ${fmt(u.nominal,0)}</div></div>`+
    `<div><div class="k">Contra hace un año (real)</div><div class="v">${hace?pc(u.real,hace.real):"s/d"}</div></div>`+
    `<div><div class="k">Contra el promedio histórico</div><div class="v">${pc(u.real,prom)}</div></div>`+
    `<div><div class="k">Máximo real</div><div class="v">$ ${fmt(mx.real,0)}</div><div class="n">${mes(mx.fecha)}</div></div>`+
    `<div><div class="k">Mínimo real</div><div class="v">$ ${fmt(mn.real,0)}</div><div class="n">${mes(mn.fecha)}</div></div>`;
  const m=css("--muted"),g=css("--line"),font={family:"IBM Plex Mono",size:11};
  c4?.destroy();
  c4=new Chart(document.getElementById("c4"),{type:"line",data:{labels:S.map(x=>mes(x.fecha)),
    datasets:[{label:A.productos[prod].nombre,data:v,borderColor:css("--cur"),backgroundColor:css("--cur")+"22",fill:true,borderWidth:2.5,pointRadius:0,tension:.2}]},
    options:{responsive:true,maintainAspectRatio:false,interaction:{mode:"index",intersect:false},
      plugins:{legend:{display:false},tooltip:{callbacks:{label:it=>`$ ${fmt(it.raw,0)}${vista==="real"?" de "+mes(A.base):""}`}}},
      scales:{x:{grid:{display:false},ticks:{color:m,font,maxTicksLimit:10,maxRotation:0}},y:{grid:{color:g},ticks:{color:m,font,callback:v=>"$ "+fmt(v)}}}}});
  document.getElementById("t4").textContent=vista==="real"?"Precios de alimentos ajustados por inflación":"Precios de alimentos (nominales)";
  document.getElementById("fnote").textContent=`Precio promedio en GBA según INDEC. ${vista==="real"?`Ajustado por IPC GBA, en pesos de ${mes(A.base)}.`:"Valores corrientes, sin ajustar."} Verde: más barato en términos reales; rojo: más caro.`;
}
document.querySelectorAll("[data-v]").forEach(b=>b.onclick=()=>{vista=b.dataset.v;document.querySelectorAll("[data-v]").forEach(x=>x.setAttribute("aria-pressed",x===b));alimentos();});
alimentos();

// Canasta en índice base 100 (precios reales)
const CAN_COL=["#ffb000","#00d26a","#3a8ee6","#ff3b3b","#b388ff","#5fd3e6","#ff66c4"];
let c15, canDesde="2023-12";
function canasta(){
  const A=D.alimentos, panel=document.getElementById("canasta");
  if(!A||!A.productos){panel.style.display="none";return;}
  const keys=Object.keys(A.productos), m=css("--muted"), g=css("--line"), font={family:"IBM Plex Mono",size:11};
  const meses=[...new Set(keys.flatMap(k=>A.productos[k].serie.map(x=>x.fecha.slice(0,7))))].sort();
  let base=canDesde;
  if(/^\d+$/.test(canDesde)){base=meses[Math.max(0,meses.length-1-+canDesde)];}
  if(!meses.includes(base)) base=meses.find(x=>x>=base)||meses[0];
  const L=meses.filter(x=>x>=base);
  const series=keys.map(k=>{const mp=Object.fromEntries(A.productos[k].serie.map(x=>[x.fecha.slice(0,7),x.real])); const b=mp[base];
    return {k,nombre:A.productos[k].nombre,data:L.map(x=>b&&mp[x]!=null?mp[x]/b*100:null)};}).filter(sr=>sr.data[0]!=null);
  const prom=L.map((_,i)=>{const v=series.map(sr=>sr.data[i]).filter(x=>x!=null);return v.length?v.reduce((a,b)=>a+b,0)/v.length:null;});
  c15?.destroy();
  c15=new Chart(document.getElementById("c15"),{type:"line",data:{labels:L.map(mesCorto),datasets:[
      ...series.map((sr,i)=>({label:sr.nombre,data:sr.data,borderColor:CAN_COL[i%CAN_COL.length],borderWidth:1.6,pointRadius:0,tension:.2})),
      {label:"Promedio canasta",data:prom,borderColor:"#ffffff",borderWidth:3,borderDash:[6,4],pointRadius:0,tension:.2},
      {label:"Base 100",data:L.map(()=>100),borderColor:"#555",borderWidth:1,pointRadius:0}]},
    options:{responsive:true,maintainAspectRatio:false,interaction:{mode:"index",intersect:false},
      plugins:{legend:{labels:{color:m,boxWidth:12,font,filter:it=>it.text!=="Base 100"}},tooltip:{callbacks:{label:it=>it.raw==null||it.dataset.label==="Base 100"?null:`${it.dataset.label}: ${fmt(it.raw,1)}`}}},
      scales:{x:{grid:{display:false},ticks:{color:m,font,maxTicksLimit:12,maxRotation:0}},y:{grid:{color:g},ticks:{color:m,font}}}}});
  const filas=series.map(sr=>({n:sr.nombre,v:sr.data[sr.data.length-1]})).sort((a,b)=>b.v-a.v);
  const pc=v=>{const x=v-100;return `<td class="pc ${x>0?"up":x<0?"down":""}">${x>0?"+":""}${fmt(x,1)} %</td>`;};
  document.getElementById("tcan").innerHTML=`<thead><tr><th>Producto</th><th>Índice</th><th>Variación real</th></tr></thead><tbody>`+
    filas.map(f=>`<tr><td class="t">${f.n}</td><td>${fmt(f.v,1)}</td>${pc(f.v)}</tr>`).join("")+
    `<tr style="font-weight:700"><td class="t" style="color:#fff">PROMEDIO CANASTA</td><td>${fmt(prom[prom.length-1],1)}</td>${pc(prom[prom.length-1])}</tr></tbody>`;
  document.getElementById("ncan").textContent=`Precio de cada producto ajustado por inflación, igual a 100 en ${mesCorto(base)}. Arriba de 100: se encareció más que la inflación; abajo: se abarató en términos reales. Último dato: ${mesCorto(L[L.length-1])}. Fuente: INDEC, precios promedio en GBA.`;
}
document.querySelectorAll("[data-cb]").forEach(b=>b.onclick=()=>{canDesde=b.dataset.cb;document.querySelectorAll("[data-cb]").forEach(x=>x.setAttribute("aria-pressed",x===b));canasta();});
setTimeout(canasta,0);  // espera a que se definan las utilidades de fechas

pintarUltimos();
document.getElementById("nmeses")?.addEventListener("change",pintarUltimos);

// Brecha CCL/MEP
let c5, cmDias=365;
function cclmep(){
  const S=(D.dolar||{}).ccl_mep||[], box=document.getElementById("c5");
  if(!S.length){box.closest(".panel").style.display="none";return;}
  let desde=S[0].fecha;
  if(cmDias){const d=new Date(S[S.length-1].fecha);d.setDate(d.getDate()-cmDias);desde=d.toISOString().slice(0,10);}
  const P=S.filter(x=>x.fecha>=desde), prom=P.reduce((a,x)=>a+x.valor,0)/P.length;
  const m=css("--muted"),g=css("--line"),font={family:"IBM Plex Mono",size:11};
  c5?.destroy();
  c5=new Chart(box,{type:"line",data:{labels:P.map(x=>dmy(x.fecha).slice(0,5)+"/"+x.fecha.slice(2,4)),datasets:[
    {label:"Brecha CCL/MEP",data:P.map(x=>x.valor),borderColor:css("--cur"),backgroundColor:css("--cur")+"1f",fill:true,borderWidth:2,pointRadius:0,tension:.15},
    {label:"Promedio del período",data:P.map(()=>prom),borderColor:css("--muted"),borderDash:[5,4],borderWidth:1.5,pointRadius:0}]},
    options:{responsive:true,maintainAspectRatio:false,interaction:{mode:"index",intersect:false},
      plugins:{legend:{labels:{color:m,boxWidth:12,font}},tooltip:{callbacks:{label:it=>`${it.dataset.label}: ${fmt(it.raw,2)} %`}}},
      scales:{x:{grid:{display:false},ticks:{color:m,font,maxTicksLimit:9,maxRotation:0}},y:{beginAtZero:true,grid:{color:g},ticks:{color:m,font,callback:v=>fmt(v)+" %"}}}}});
  const u=S[S.length-1], mx=P.reduce((a,b)=>b.valor>a.valor?b:a), mn=P.reduce((a,b)=>b.valor<a.valor?b:a);
  document.getElementById("ncm").textContent=`Hoy ${fmt(u.valor,2)} %, promedio del período ${fmt(prom,2)} %, máximo ${fmt(mx.valor,2)} % (${dmy(mx.fecha)}), mínimo ${fmt(mn.valor,2)} % (${dmy(mn.fecha)}). CCL y MEP a precio de venta, ArgentinaDatos.`;
}
document.querySelectorAll("[data-cm]").forEach(b=>b.onclick=()=>{cmDias=+b.dataset.cm;document.querySelectorAll("[data-cm]").forEach(x=>x.setAttribute("aria-pressed",x===b));cclmep();});
cclmep();

// Compras por mes (12 meses)
let c6;
function meses12(){
  const tot={}, n={}, prov={};
  for(const x of D.compras){const k=ym(x.fecha); tot[k]=(tot[k]||0)+x.valor; n[k]=(n[k]||0)+1; if(x.prov) prov[k]=true;}
  const ks=Object.keys(tot).sort().slice(-12), ult=ks[ks.length-1];
  const vals=ks.map(k=>Math.round(tot[k]*10)/10);
  let acc=0; const cum=vals.map(v=>Math.round((acc+=v)*10)/10);
  const mes=k=>{const[y,m]=k.split("-");return MESES[+m-1].slice(0,3)+" "+y.slice(2);};
  const m=css("--muted"),g=css("--line"),font={family:"IBM Plex Mono",size:11};
  c6?.destroy();
  c6=new Chart(document.getElementById("c6"),{data:{labels:ks.map(k=>mes(k)+(k===ult?"*":"")),datasets:[
    {type:"bar",label:"Compras del mes",data:vals,yAxisID:"y",order:1,borderRadius:4,
      backgroundColor:vals.map((v,i)=>v<0?css("--bad"):(ks[i]===ult?css("--prev"):css("--cur")))},
    {type:"line",label:"Acumulado 12 meses",data:cum,yAxisID:"y2",order:0,borderColor:css("--ink"),backgroundColor:css("--ink"),borderWidth:2.5,borderDash:[6,4],pointRadius:3,tension:.25}]},
    options:{responsive:true,maintainAspectRatio:false,interaction:{mode:"index",intersect:false},
      plugins:{legend:{labels:{color:m,boxWidth:12,font}},tooltip:{callbacks:{
        title:it=>{const k=ks[it[0].dataIndex];return lab(k)+(k===ult?" (mes en curso)":"");},
        label:it=>it.datasetIndex===0?`Compras: USD ${fmt(it.raw,1)} M en ${n[ks[it.dataIndex]]} ruedas${prov[ks[it.dataIndex]]?" (incluye provisorios)":""}`:`Acumulado: USD ${fmt(it.raw,0)} M`}}},
      scales:{x:{grid:{display:false},ticks:{color:m,font}},
        y:{grid:{color:g},ticks:{color:m,font,callback:v=>fmt(v)+" M"},title:{display:true,text:"Por mes",color:m,font}},
        y2:{position:"right",grid:{display:false},ticks:{color:m,font,callback:v=>fmt(v)+" M"},title:{display:true,text:"Acumulado",color:m,font}}}}});
  const total=cum[cum.length-1], best=ks[vals.indexOf(Math.max(...vals))];
  document.getElementById("m12tot").innerHTML=`Total: <b style="color:var(--ink)">USD ${fmt(total,0)} M</b>`;
  document.getElementById("m12n").textContent=`Mejor mes: ${lab(best)} con USD ${fmt(Math.max(...vals),1)} M. * Mes en curso, todavía incompleto. Fuente: BCRA, variable 78.`;
}
meses12();

// Inflación INDEC
let c8;
function inflacion(){
  const F=D.inflacion||{}, M=(F.mensual||[]).slice(-24), IA=Object.fromEntries((F.interanual||[]).map(x=>[x.mes,x.valor]));
  const m=css("--muted"),g=css("--line"),font={family:"IBM Plex Mono",size:11};
  const mes=k=>{const[y,mm]=k.split("-");return MESES[+mm-1].slice(0,3)+" "+y.slice(2);};
  if(M.length){
    const vals=M.map(x=>x.valor), ult=M.length-1;
    c8?.destroy();
    c8=new Chart(document.getElementById("c8"),{data:{labels:M.map(x=>mes(x.mes)),datasets:[
      {type:"bar",label:"Mensual",data:vals,yAxisID:"y",order:1,borderRadius:4,backgroundColor:vals.map((v,i)=>i===ult?css("--bad"):css("--cur"))},
      {type:"line",label:"Interanual",data:M.map(x=>IA[x.mes]??null),yAxisID:"y2",order:0,borderColor:css("--ink"),backgroundColor:css("--ink"),borderDash:[6,4],borderWidth:2,pointRadius:2,tension:.25}]},
      options:{responsive:true,maintainAspectRatio:false,interaction:{mode:"index",intersect:false},
        plugins:{legend:{labels:{color:m,boxWidth:12,font}},tooltip:{callbacks:{label:it=>`${it.dataset.label}: ${fmt(it.raw,1)} %`}}},
        scales:{x:{grid:{display:false},ticks:{color:m,font,maxRotation:0,autoSkip:true}},
          y:{beginAtZero:true,grid:{color:g},ticks:{color:m,font,callback:v=>fmt(v,1)+" %"},title:{display:true,text:"Mensual",color:m,font}},
          y2:{position:"right",beginAtZero:true,grid:{display:false},ticks:{color:m,font,callback:v=>fmt(v)+" %"},title:{display:true,text:"Interanual",color:m,font}}}}});
    document.getElementById("infsub").textContent="Últimos 24 meses · fuente: INDEC vía BCRA";
  } else document.getElementById("pinf").style.display="none";

}
inflacion();

// Históricos: dólar real (ITCRM) y Merval real
const PRESIDENTES=[["2002-01","DUHALDE"],["2003-05","N. KIRCHNER"],["2007-12","C. FERNÁNDEZ"],["2015-12","MACRI"],["2019-12","A. FERNÁNDEZ"],["2023-12","MILEI"]];
const lineasVert={id:"lineasVert",afterDatasetsDraw(ch,_,o){const L=o&&o.marcas; if(!L) return; const {ctx,chartArea:a,scales:{x}}=ch;
  ctx.save(); ctx.font='10px "IBM Plex Mono",monospace'; ctx.textAlign="left";
  L.forEach(([idx,txt])=>{ if(idx<0) return; const px=x.getPixelForValue(idx); ctx.strokeStyle="#b36b00"; ctx.lineWidth=1; ctx.beginPath(); ctx.moveTo(px,a.top); ctx.lineTo(px,a.bottom); ctx.stroke();
    ctx.fillStyle="#ffb000"; ctx.fillText(txt,px+3,a.top+10); }); ctx.restore();}};
const mesCorto=k=>{const[y,m]=k.split("-");return MESES[+m-1].slice(0,3)+"-"+y.slice(2);};
let c11,c12;
function historicos(){
  const H=D.historicos||{}, m=css("--muted"), g=css("--line"), font={family:"IBM Plex Mono",size:11};
  const T=H.tcr;
  if(T&&T.serie.length){
    const S=T.serie, L=S.map(x=>x[0]), of=S.map(x=>x[1]), cc=S.map(x=>x[2]);
    const prom=a=>{const v=a.filter(x=>x!=null);return v.reduce((s,x)=>s+x,0)/v.length;};
    const min17=Math.min(...S.filter(x=>x[0].startsWith("2017")).map(x=>x[1]));
    const pba=S.filter(x=>x[0]>="2025-09"&&x[0]<="2025-10"&&x[2]!=null).map(x=>x[2]); const maxPBA=pba.length?Math.max(...pba):null;
    const pO=prom(of), pC=prom(cc), cte=v=>L.map(()=>v);
    const ref=(lbl,v,col)=>({label:lbl,data:cte(v),borderColor:col,borderDash:[5,4],borderWidth:1.3,pointRadius:0});
    c11?.destroy();
    c11=new Chart(document.getElementById("c11"),{type:"line",data:{labels:L.map(mesCorto),datasets:[
      {label:"Dólar CCL",data:cc,borderColor:"#00d26a",borderWidth:1.8,pointRadius:0,spanGaps:false,tension:.15,order:2},
      {label:"Dólar oficial",data:of,borderColor:"#ffb000",borderWidth:1.8,pointRadius:0,tension:.15,order:1},
      ref("Mín. 2017",min17,"#ff3b3b"), ref("Prom. oficial",pO,"#3a8ee6"), ref("Prom. CCL",pC,"#ff9a3c"),
      ...(maxPBA?[ref("Máx. tras elección PBA",maxPBA,"#b388ff")]:[])]},
      options:{responsive:true,maintainAspectRatio:false,interaction:{mode:"index",intersect:false},
        plugins:{lineasVert:{marcas:PRESIDENTES.map(([k,n])=>[L.indexOf(k),n])},legend:{labels:{color:m,boxWidth:12,font}},
          tooltip:{callbacks:{label:it=>it.raw==null?null:`${it.dataset.label}: $ ${fmt(it.raw,0)}`}}},
        scales:{x:{grid:{display:false},ticks:{color:m,font,maxTicksLimit:14,maxRotation:0}},y:{grid:{color:g},ticks:{color:m,font,callback:v=>"$ "+fmt(v)}}}},plugins:[lineasVert]});
    const u=S[S.length-1];
    document.getElementById("tcrt").textContent=`Dólar oficial y CCL ajustados por ITCRM (base ${mesCorto(T.base)})`;
    document.getElementById("ntcr").textContent=`Cuánto valdría hoy cada dólar del pasado si se mantuviera el mismo tipo de cambio real multilateral. Último: oficial $ ${fmt(u[1],0)}, CCL $ ${u[2]?fmt(u[2],0):"s/d"}. Promedio desde 2001: oficial $ ${fmt(pO,0)}, CCL $ ${fmt(pC,0)}. Mínimo de 2017: $ ${fmt(min17,0)}. El CCL coincide con el oficial antes del cepo de nov-2011; entre nov-2011 y dic-2012 no hay datos. Fuentes: BCRA (ITCRM, A3500), ArgentinaDatos (CCL).`;
  } else document.getElementById("ptcr").style.display="none";
  const M=H.merval;
  if(M&&M.serie.length){
    const S=M.serie, L=S.map(x=>x[0]), v=S.map(x=>x[1]), med=v.reduce((a,b)=>a+b,0)/v.length;
    c12?.destroy();
    c12=new Chart(document.getElementById("c12"),{type:"line",data:{labels:L.map(mesCorto),datasets:[
      {label:"Merval real",data:v,borderColor:"#3a8ee6",backgroundColor:"#3a8ee622",fill:true,borderWidth:1.8,pointRadius:0,tension:.15},
      {label:"Media",data:L.map(()=>med),borderColor:"#00d26a",borderDash:[5,4],borderWidth:1.3,pointRadius:0}]},
      options:{responsive:true,maintainAspectRatio:false,interaction:{mode:"index",intersect:false},
        plugins:{lineasVert:{marcas:PRESIDENTES.map(([k,n])=>[L.indexOf(k),n])},legend:{labels:{color:m,boxWidth:12,font}},tooltip:{callbacks:{label:it=>`${it.dataset.label}: ${fmt(it.raw,0)}`}}},
        scales:{x:{grid:{display:false},ticks:{color:m,font,maxTicksLimit:14,maxRotation:0}},y:{grid:{color:g},ticks:{color:m,font,callback:x=>fmt(x/1e6,1)+" M"}}}},plugins:[lineasVert]});
    const u=v[v.length-1], mx=Math.max(...v), mxk=L[v.indexOf(mx)];
    document.getElementById("mervt").textContent=`Merval ajustado por inflación desde diciembre 2001 (en puntos de ${mesCorto(M.base)})`;
    document.getElementById("nmerv").textContent=`Hoy ${fmt(u,0)} puntos reales, ${fmt((u/med-1)*100,0)} % ${u>=med?"arriba":"abajo"} de la media (${fmt(med,0)}) y ${fmt((u/mx-1)*100,0)} % contra el máximo de ${mesCorto(mxk)}. Inflación: INDEC hasta 2006 y desde 2017; IPC de San Luis entre 2007 y 2016, cuando el INDEC estuvo intervenido. Fuentes: Yahoo Finance (S&P Merval), INDEC, datos.gob.ar.`;
  } else document.getElementById("pmerv").style.display="none";
}
historicos();

// Reservas por gobierno
const GOBIERNOS=[["CRISTINA KIRCHNER","2007-12-10","2015-12-09","#3a8ee6"],["MAURICIO MACRI","2015-12-10","2019-12-09","#ffb000"],
  ["ALBERTO FERNÁNDEZ","2019-12-10","2023-12-09","#00d26a"],["JAVIER MILEI","2023-12-10","2099-12-31","#b388ff"]];
const fondosGob={id:"fondosGob",beforeDatasetsDraw(ch,_,o){const {ctx,chartArea:a,scales:{x}}=ch; const R=o.rangos||[];
  ctx.save(); R.forEach(([i0,i1,col,nom])=>{const x0=Math.max(x.getPixelForValue(i0),a.left),x1=Math.min(x.getPixelForValue(i1),a.right);
    ctx.fillStyle=col+"14"; ctx.fillRect(x0,a.top,x1-x0,a.bottom-a.top); ctx.fillStyle=col; ctx.font='bold 11px "IBM Plex Mono",monospace'; ctx.textAlign="center";
    ctx.fillText(nom,(x0+x1)/2,a.top+14);}); ctx.restore();},
  afterDatasetsDraw(ch,_,o){const {ctx,scales:{x,y},chartArea:a}=ch; ctx.save(); ctx.font='10.5px "IBM Plex Mono",monospace';
    (o.notas||[]).forEach(([i,v,txt,col,arriba])=>{const px=x.getPixelForValue(i),py=y.getPixelForValue(v), ty=arriba?py-18:py+24;
      ctx.strokeStyle="#888"; ctx.beginPath(); ctx.moveTo(px,py); ctx.lineTo(px,ty+(arriba?4:-12)); ctx.stroke();
      ctx.fillStyle=col; ctx.beginPath(); ctx.arc(px,py,3,0,7); ctx.fill();
      ctx.fillStyle="#ddd"; const w=ctx.measureText(txt).width; let tx=Math.min(Math.max(px-w/2,a.left+2),a.right-w-2); ctx.textAlign="left"; ctx.fillText(txt,tx,ty);}); ctx.restore();}};
let c14;
function reservasGob(){
  const R=(D.historicos||{}).reservas; const panel=document.getElementById("pgob");
  if(!R||!R.f.length){panel.style.display="none";return;}
  const F=R.f, V=R.v, m=css("--muted"), g=css("--line"), font={family:"IBM Plex Mono",size:11};
  const MES=["ene","feb","mar","abr","may","jun","jul","ago","sep","oct","nov","dic"], mf=f=>MES[+f.slice(5,7)-1]+" "+f.slice(0,4);
  const tarj=[], rangos=[], notas=[], ds=[];
  GOBIERNOS.forEach(([nom,ini,fin,col],k)=>{
    const idx=F.map((f,i)=>f>=ini&&f<=fin?i:-1).filter(i=>i>=0); if(!idx.length) return;
    const i0=idx[0], i1=idx[idx.length-1], v0=V[i0], v1=V[i1], d=v1-v0, pc=(v1/v0-1)*100;
    const enCurso=fin>"2090";
    tarj.push(`<div style="--c:${col}"><div class="nm">${nom}</div><div class="fe">${dmy(F[i0])} – ${enCurso?"en curso (al "+dmy(F[i1])+")":dmy(F[i1])}</div>
      <div class="pc">${pc>0?"+":"−"}${fmt(Math.abs(pc),0)} %</div><div class="ab">${d>0?"+":"−"}USD ${fmt(Math.abs(d),1)} mil M</div><div class="rg">${fmt(v0,1)} → ${fmt(v1,1)} mil M</div></div>`);
    rangos.push([i0,i1,col,nom.split(" ").slice(-1)[0]==="KIRCHNER"?"CFK":nom.split(" ").slice(-1)[0]]);
    let im=i0; idx.forEach(i=>{if(V[i]>V[im]) im=i;});
    notas.push([im,V[im],`Máx. ${nom.split(" ").slice(-1)[0]==="KIRCHNER"?"CFK":nom.split(" ").slice(-1)[0][0]+nom.split(" ").slice(-1)[0].slice(1).toLowerCase()}: ${fmt(V[im],1)} (${mf(F[im])})`,col,true]);
    ds.push({label:nom,data:V.map((v,i)=>i>=i0&&i<=i1?v:null),borderColor:col,borderWidth:1.5,pointRadius:0,spanGaps:false,tension:0});
  });
  let imin=0; V.forEach((v,i)=>{if(v<V[imin]) imin=i;});
  notas.push([imin,V[imin],`Mín.: ${fmt(V[imin],1)} (${mf(F[imin])})`,"#ff3b3b",false]);
  document.getElementById("gobs").innerHTML=tarj.join("");
  c14?.destroy();
  c14=new Chart(document.getElementById("c14"),{type:"line",data:{labels:F,datasets:ds},
    options:{responsive:true,maintainAspectRatio:false,animation:false,interaction:{mode:"index",intersect:false},
      plugins:{legend:{display:false},fondosGob:{rangos,notas},tooltip:{callbacks:{title:it=>dmy(F[it[0].dataIndex]),label:it=>it.raw==null?null:`${it.dataset.label}: USD ${fmt(it.raw,1)} mil M`}}},
      scales:{x:{grid:{display:false},ticks:{color:m,font,maxRotation:0,autoSkip:false,callback:(v,i)=>{const f=F[i],p=F[i-1]; return (p&&f.slice(0,4)!==p.slice(0,4))?f.slice(0,4):null;}}},
        y:{beginAtZero:true,suggestedMax:Math.ceil(Math.max(...V)*1.15/10)*10,grid:{color:g},ticks:{color:m,font},title:{display:true,text:"Miles de millones de USD",color:m,font}}}},plugins:[fondosGob]});
  document.getElementById("ngob").textContent=`Reservas brutas diarias (${fmt(F.length,0)} observaciones desde ${dmy(F[0])}). La variación de cada gobierno compara el primer y el último dato de su mandato; el de Javier Milei llega al último dato disponible (${dmy(F[F.length-1])}). Fuente: BCRA, variable 1 de la API de estadísticas, más los provisorios cargados.`;
}
reservasGob();

// Pobreza
let c16;
function pobreza(){
  const P=(D.historicos||{}).pobreza||[], panel=document.getElementById("ppob");
  if(!P.length){panel.style.display="none";return;}
  const m=css("--muted"), g=css("--line"), font={family:"IBM Plex Mono",size:11};
  // se colorea según quién gobernó la mayor parte del semestre (mitad del período)
  const medio=f=>{const y=+f.slice(0,4),mm=+f.slice(5,7);return mm===7?`${y}-04`:`${y-1}-10`;};
  const colGob=f=>{const x=medio(f);return x<"2007-12"?"#7f8ea3":x<"2015-12"?"#3a8ee6":x<"2019-12"?"#ffb000":x<"2023-12"?"#00d26a":"#b388ff";};
  const L=P.map(x=>x[0]), V=P.map(x=>x[1]);
  const corte=P.findIndex(x=>x[3]==="INDEC");
  c16?.destroy();
  c16=new Chart(document.getElementById("c16"),{type:"bar",data:{labels:L,datasets:[{label:"Pobreza",data:V,backgroundColor:P.map(x=>colGob(x[2])+(x[3]==="CEDLAS"?"99":"")),borderColor:P.map(x=>colGob(x[2])),borderWidth:P.map(x=>x[3]==="CEDLAS"?1.5:0),borderRadius:0}]},
    options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false},
      lineasVert:{marcas:corte>0?[[corte-0.5,"SERIE OFICIAL INDEC →"]]:[]},
      tooltip:{callbacks:{label:it=>it.raw==null?"sin dato":`${fmt(it.raw,1)} % de las personas (${P[it.dataIndex][3]==="CEDLAS"?"CEDLAS, serie comparable":"INDEC"})`}}},
      scales:{x:{grid:{display:false},ticks:{color:m,font,maxRotation:90,autoSkip:false}},y:{beginAtZero:true,grid:{color:g},ticks:{color:m,font,callback:v=>v+" %"}}}},plugins:[lineasVert]});
  const u=P[P.length-1], a=P[P.length-2], ia=P.find(x=>x[0]===u[0].replace(/\d\d$/,d=>String(+d-1).padStart(2,"0")));
  const OK=P.filter(x=>x[1]!=null), max=OK.reduce((p,x)=>x[1]>p[1]?x:p), min=OK.reduce((p,x)=>x[1]<p[1]?x:p);
  const pp=(x,y)=>{const d=x-y;return `${d>0?"+":""}${fmt(d,1)} pp`;};
  document.getElementById("pobcards").innerHTML=
    `<div style="--c:#ffb000"><div class="nm">ÚLTIMO DATO (${u[0]})</div><div class="pc">${fmt(u[1],1)} %</div><div class="ab">${pp(u[1],a[1])} vs ${a[0]}</div><div class="rg">${ia?pp(u[1],ia[1])+" vs "+ia[0]:""}</div></div>`+
    `<div style="--c:#ff3b3b"><div class="nm">MÁXIMO 20 AÑOS</div><div class="pc">${fmt(max[1],1)} %</div><div class="rg">${max[0]}</div></div>`+
    `<div style="--c:#00d26a"><div class="nm">MÍNIMO 20 AÑOS</div><div class="pc">${fmt(min[1],1)} %</div><div class="rg">${min[0]}</div></div>`+
    `<div style="--c:#7f8ea3"><div class="nm">GOBIERNOS</div><div class="rg" style="margin-top:6px;line-height:1.7"><span style="color:#7f8ea3">■</span> N. Kirchner · <span style="color:#3a8ee6">■</span> C. Fernández<br><span style="color:#ffb000">■</span> Macri · <span style="color:#00d26a">■</span> A. Fernández · <span style="color:#b388ff">■</span> Milei</div></div>`;
  document.getElementById("npob").textContent=`Porcentaje de personas en hogares con ingresos por debajo de la canasta básica total, por semestre (1S: enero a junio; 2S: julio a diciembre), en los 31 aglomerados urbanos de la EPH. Barras translúcidas (2006-2015): serie comparable de CEDLAS-UNLP, que aplica la metodología actual del INDEC a la EPH con índices de precios alternativos para 2007-2015, cuando el INDEC estuvo intervenido; el 2.º semestre de 2015 no tiene dato. Desde el 2.º trimestre de 2016: serie oficial del INDEC, que se publica en marzo y septiembre. Fuentes: CEDLAS-UNLP, INDEC vía datos.gob.ar.`;
}
pobreza();

// Compras de personas humanas
let c13;
function personas(){
  const P=(D.historicos||{}).personas||[];
  if(!P.length){document.getElementById("pph").style.display="none";return;}
  const m=css("--muted"),g=css("--line"),font={family:"IBM Plex Mono",size:11};
  const L=P.map(x=>x[0]), v=P.map(x=>x[1]);
  const idxCepo=L.indexOf("2025-04")>=0?L.indexOf("2025-04"):L.findIndex(k=>k>"2025-04");
  c13?.destroy();
  c13=new Chart(document.getElementById("c13"),{type:"bar",data:{labels:L.map(mesCorto),datasets:[{label:"USD millones",data:v,borderRadius:0,
      backgroundColor:L.map(k=>k===L[L.length-1]?"#3a8ee6":"#ffb000")}]},
    options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false},lineasVert:{marcas:[[idxCepo,"SALIDA DEL CEPO (14/4/25) · ABR-25 SIN DATO"]]},
      tooltip:{callbacks:{label:it=>`USD ${fmt(it.raw,0)} M`}}},
      scales:{x:{grid:{display:false},ticks:{color:m,font,maxRotation:0,autoSkip:true}},y:{beginAtZero:true,grid:{color:g},ticks:{color:m,font,callback:x=>fmt(x)+" M"}}}},plugins:[lineasVert]});
  const ok=P.filter(x=>x[0]>="2025-05"&&x[1]!=null), tot=ok.reduce((a,x)=>a+x[1],0), u=P[P.length-1], prom=tot/ok.length;
  const anio=u[0].slice(0,4), ytd=P.filter(x=>x[0].startsWith(anio)&&x[1]!=null).reduce((a,x)=>a+x[1],0);
  document.getElementById("nph").textContent=`Último dato: ${mesCorto(u[0])} USD ${fmt(u[1],0)} M. En ${anio}: USD ${fmt(ytd,0)} M. Desde la salida del cepo (may-25): USD ${fmt(tot,0)} M, promedio USD ${fmt(prom,0)} M por mes. Incluye billetes, giros sin fines específicos y pagos con tarjeta en el exterior; antes de abril 2025, con cepo, eran casi solo consumos con tarjeta. Abril 2025 no tiene un dato comparable. Sale una vez por mes, a fin del mes siguiente. Fuente: BCRA, Informe de Evolución del Mercado de Cambios y Balance Cambiario.`;
}
personas();

// Tablas de mercado
function celda(v){
  if(v==null) return '<td class="pc">–</td>';
  const a=Math.min(Math.abs(v)/15,1), base=v>=0?"--good":"--bad";
  return `<td class="pc" style="color:var(${base});background:color-mix(in srgb,var(${base}) ${Math.round(6+a*28)}%,transparent)">${v>0?"+":""}${fmt(v,2)} %</td>`;
}
(function mercados(){
  const B=D.bonos||[], A=D.adrs||[], cols="<th>Precio</th><th>1 día</th><th>5 días</th><th>1 mes</th><th>En el año</th>";
  if(B.some(g=>g.filas.length)){
    const f2=(v,d=2)=>v==null?"–":fmt(+v,d), vol=v=>v==null?"–":(v>=1e6?fmt(v/1e6,1)+"M":v>=1e3?fmt(v/1e3,0)+"K":fmt(v,0));
    const fv=iso=>{if(!iso) return "–"; const[y,m,d]=iso.split("-"); return `${d}-${["ene","feb","mar","abr","may","jun","jul","ago","sep","oct","nov","dic"][+m-1]}-${y.slice(2)}`;};
    const bp=v=>v==null?'<td class="pc">–</td>':`<td class="pc ${v>0?"up":v<0?"down":""}">${v>0?"+":""}${fmt(v,0)}</td>`;
    const hdr="<th>Ticker</th><th>Ley</th><th>Cupón</th><th>Vencimiento</th><th>Compra</th><th>Venta</th><th>Último</th><th>Día</th><th>TIR</th><th>TNA 180/360</th><th>Dur. mod.</th><th>Convexidad</th><th>Paridad</th><th>Valor técnico</th><th>Vs curva (pb)</th><th>Spread (pb)</th><th>MEP implícito</th><th>Volumen</th>";
    document.getElementById("tbon").innerHTML=`<thead><tr>${hdr}</tr></thead><tbody>`+
      B.map(g=>`<tr class="grp"><td colspan="18">${g.grupo}</td></tr>`+g.filas.map(r=>`<tr data-bono="${r.t}"><td class="t">${r.t}</td><td><span class="ley ${r.ley==="NY"?"ny":""}">${r.ley}</span></td><td>${r.cupon!=null?fmt(r.cupon,3)+" %":"–"}</td><td>${fv(r.vto)}</td>`+
        `<td>${f2(r.bid)}</td><td>${f2(r.ask)}</td><td class="val">${f2(r.precio)}</td><td class="pc ${r.d1>0?"up":r.d1<0?"down":""}">${r.d1!=null?(r.d1>0?"+":"")+fmt(r.d1,2)+" %":"–"}</td>`+
        `<td class="tir">${r.tir!=null?fmt(r.tir,2)+" %":"–"}</td><td>${r.tna!=null?fmt(r.tna,2)+" %":"–"}</td><td>${f2(r.md)}</td><td>${f2(r.conv,1)}</td><td>${r.paridad!=null?fmt(r.paridad,1)+" %":"–"}</td><td>${f2(r.vt)}</td>`+
        `${bp(r.vs_curva)}<td>${r.spread!=null?fmt(r.spread,0):"–"}</td><td>${r.mep?"$ "+fmt(r.mep,2):"–"}</td><td>${vol(r.vol)}</td></tr>`).join("")).join("")+"</tbody>";
    // Flujos pendientes al hacer clic en un bono
    document.querySelectorAll("#tbon tr[data-bono]").forEach(tr=>tr.onclick=()=>{
      const r=B.flatMap(g=>g.filas).find(x=>x.t===tr.dataset.bono); const el=document.getElementById("flujos");
      if(!r||!r.flujos){el.innerHTML="";return;}
      let tc=0,ta=0;
      el.innerHTML=`<div class="phead"><h2>Flujos pendientes de ${r.t} por cada 100 nominales</h2></div><div class="tablewrap"><table class="mkt"><thead><tr><th>Fecha</th><th>Interés</th><th>Amortización</th><th>Total</th></tr></thead><tbody>`+
        r.flujos.map(f=>{tc+=f[1];ta+=f[2];return `<tr><td>${fv(f[0])}</td><td>${fmt(f[1],3)}</td><td>${fmt(f[2],3)}</td><td class="val">${fmt(f[1]+f[2],3)}</td></tr>`;}).join("")+
        `<tr class="grp"><td>Total</td><td>${fmt(tc,2)}</td><td>${fmt(ta,2)}</td><td>${fmt(tc+ta,2)}</td></tr></tbody></table></div>`;
      document.querySelectorAll("#tbon tr[data-bono]").forEach(x=>x.classList.toggle("sel",x===tr));
    });
  } else document.getElementById("pbon").style.display="none";
  if(A.length){
    document.getElementById("tadr").innerHTML=`<thead><tr><th>ADR</th><th style="text-align:left">Empresa</th>${cols}</tr></thead><tbody>`+
      A.map(r=>`<tr><td class="t">${r.t}</td><td class="nm">${r.nombre}</td><td>${fmt(r.precio,2)}</td>${celda(r.d1)}${celda(r.d5)}${celda(r.m1)}${celda(r.ytd)}</tr>`).join("")+"</tbody>";
  } else document.getElementById("padr").style.display="none";
})();

// Escenarios Prob K
(function escenarios(){
  const E=D.escenarios, t=document.getElementById("tesc");
  if(!E||!E.activos||!E.activos.length){document.getElementById("pk").style.display="none";return;}
  const A=E.activos, pct=v=>`${v>0?"+":""}${fmt(v,2)} %`;
  let h=`<thead><tr><th>Prob K</th>${A.map(a=>`<th>${a.nombre}</th><th>Up/Down</th>`).join("")}</tr></thead><tbody>`;
  for(const p of E.filas){
    h+=`<tr><td>${p} %</td>`+A.map(a=>{const sc=a.p0-(a.p0-a.p100)*p/100, dc=a.p0>500?0:2;
      if(a.precio==null) return `<td>${fmt(sc,dc)}</td><td>–</td>`;
      const u=(sc/a.precio-1)*100; return `<td>${fmt(sc,dc)}</td><td class="ud ${u>=0?"pos":"neg"}">${pct(u)}</td>`;}).join("")+`</tr>`;
  }
  h+=`<tr class="hoy"><td>Precio hoy</td>${A.map(a=>`<td>${a.precio!=null?fmt(a.precio,a.p0>500?0:2):(a.fuente==="pendiente"?"configurar":"s/d")}</td><td></td>`).join("")}</tr>`;
  h+=`<tr class="prob"><td>Prob implícita</td>${A.map(a=>{if(a.precio==null) return `<td>–</td><td></td>`;
    const pr=(a.p0-a.precio)/(a.p0-a.p100)*100; return `<td class="pv">${fmt(pr,1)} %</td><td></td>`;}).join("")}</tr></tbody>`;
  t.innerHTML=h;
  document.getElementById("nesc").textContent="Precio de cada escenario interpolado entre tus precios objetivo con 0 % y 100 % de probabilidad (config.json). Up/Down: ganancia o pérdida desde el precio de hoy. Bonos: precio en USD MEP (data912). Merval USD: S&P Merval dividido por el CCL; con 0 % usa el máximo de enero 2025 y con 100 % el promedio 2020-2023.";
})();

// Reservas netas
(function rnetas(){
  const R=D.reservasNetas, t=document.getElementById("trn");
  if(!R||!R.brutas){document.getElementById("rn").style.display="none";return;}
  const B=R.base, hoy={brutas:R.brutas.valor, encajes:R.encajes?.valor, swap:R.swap?.valor, bis:R.bis, oro:R.oro?.valor, bm:R.base_monetaria?.valor};
  const netas=o=>[o.brutas,o.encajes,o.swap,o.bis].some(v=>v==null)?null:o.brutas-o.encajes-o.swap-o.bis;
  const nH=netas(hoy), nB=netas({brutas:B.brutas,encajes:B.encajes,swap:B.swap,bis:B.bis});
  const tcH=hoy.bm!=null?hoy.bm/hoy.brutas:null, tcB=B.base_monetaria/B.brutas;
  const f=v=>v==null?"s/d":fmt(v,0), neg=v=>v==null?"s/d":"− "+fmt(v,0);
  const d=(a,b)=>a==null||b==null?"":`${a-b>0?"+":""}${fmt(a-b,0)}`;
  const fh=dmy(R.brutas.fecha);
  t.innerHTML=`<thead><tr><th></th><th>${dmy(B.fecha)}</th><th>${fh}</th><th>Diferencia</th></tr></thead><tbody>
   <tr class="tot"><td>Reservas brutas</td><td>${f(B.brutas)}</td><td>${f(hoy.brutas)}</td><td>${d(hoy.brutas,B.brutas)}</td></tr>
   <tr><td class="lbl">Encajes<small>${R.encajes?"al "+dmy(R.encajes.fecha).slice(0,5):""}</small></td><td>${neg(B.encajes)}</td><td>${neg(hoy.encajes)}</td><td></td></tr>
   <tr><td class="lbl">Swap China<small>${R.swap?"CNY "+fmt(R.swap.tc,4):""}</small></td><td>${neg(B.swap)}</td><td>${neg(hoy.swap)}</td><td></td></tr>
   <tr><td class="lbl">BIS, multilaterales y SEDESA<small>bce. ${dmy(R.bis_fecha).slice(0,5)}</small></td><td>${neg(B.bis)}</td><td>${neg(hoy.bis)}</td><td></td></tr>
   <tr class="tot"><td>Reservas netas</td><td class="${nB<0?"neg":"pos"}">${f(nB)}</td><td class="${nH<0?"neg":"pos"}">${f(nH)}</td><td>${d(nH,nB)}</td></tr>
   <tr><td class="lbl">Oro<small>${R.oro?"USD "+fmt(R.oro.precio,0)+"/oz":""}</small></td><td>${f(B.oro)}</td><td>${f(hoy.oro)}</td><td></td></tr>
   <tr><td class="lbl">Base monetaria<small>millones de $</small></td><td>${f(B.base_monetaria)}</td><td>${f(hoy.bm)}</td><td></td></tr>
   <tr class="tot"><td>TC de convertibilidad</td><td>${f(tcB)}</td><td>${f(tcH)}</td><td></td></tr></tbody>`;
  document.getElementById("nrn").textContent="En millones de USD. Encajes: cuenta corriente en moneda extranjera de los bancos en el BCRA, pasada a dólares al mayorista (estimación). Swap: 130.000 M de yuanes al tipo de cambio del día. BIS, multilaterales y SEDESA: dato del balance semanal, cargado en config.json. TC de convertibilidad: base monetaria dividida por reservas brutas.";
})();

function render(){
  const kC=sel,kP=shift(sel,-1);
  const of=k=>D.compras.filter(x=>ym(x.fecha)===k);
  const C=of(kC),P=of(kP);
  const tot=a=>a.reduce((s,x)=>s+x.valor,0), tC=tot(C),tP=tot(P);
  const pct=(a,b)=>b>0?(a/b-1)*100:null, sg=v=>v>0?"+":"";
  const vs=(a,b)=>{const p=pct(a,b);return p===null?`${sg(a-b)}USD ${fmt(a-b,1)} M`:`${sg(p)}${fmt(p,1)} % (${sg(a-b)}${fmt(a-b,1)} M)`;};
  const cls=v=>v>=0?"up":"down";
  const resC=D.reservas.filter(x=>ym(x.fecha)===kC), rU=resC[resC.length-1], rP=D.reservas.filter(x=>ym(x.fecha)===kP).pop();
  const box=(color,k,a)=>`<div class="stat" style="border-top-color:var(${color})"><div class="k"><i style="background:var(${color})"></i>${lab(k)}</div><div class="v ${tot(a)<0?"down":""}">USD ${fmt(tot(a),1)} M</div><div class="n">${a.length} ruedas, promedio ${fmt(a.length?tot(a)/a.length:0,1)} M</div></div>`;
  document.getElementById("stats").innerHTML=
    box("--cur",kC,C)+box("--prev",kP,P)+
    `<div class="stat" style="border-top-color:var(--res)"><div class="k">Variación</div><div class="n" style="margin-top:6px">Contra ${MESES[+kP.slice(5)-1]}: <b class="${cls(tC-tP)}">${vs(tC,tP)}</b></div><div class="n">Reservas: <b>USD ${rU?fmt(rU.valor):"s/d"} M</b>${rU&&rP?` (${sg(rU.valor-rP.valor)}${fmt(rU.valor-rP.valor)} M en el mes)`:""}</div></div>`;

  // Comparativo a la misma rueda
  const r=C.length, aC=tot(C), aP=tot(P.slice(0,r)), nP=P.slice(0,r).length;
  const dif=(a,b)=>{const d=a-b;return `<b class="${cls(d)}">${d>=0?"arriba":"abajo"} por USD ${fmt(Math.abs(d),1)} M${b>0?` (${sg(pct(a,b))}${fmt(pct(a,b),1)} %)`:""}</b>`;};
  const hoyC=C[r-1], hoyP=P[r-1];
  const cmp=document.getElementById("cmp");
  cmp.classList.toggle("neg",aC-aP<0);
  cmp.innerHTML=r?`<h2>Rueda ${r} de ${MESES[+kC.slice(5)-1]} (${dmy(hoyC.fecha)}${hoyC.prov?", provisorio":""})</h2>
    <p class="big">A esta rueda de ${lab(kP).toLowerCase()} el BCRA llevaba USD ${fmt(aP,1)} M; hoy lleva USD ${fmt(aC,1)} M: ${dif(aC,aP)}.</p>
    <div class="rows">
      <div>Solo la rueda ${r}: USD ${fmt(hoyC.valor,1)} M hoy contra ${hoyP?`USD ${fmt(hoyP.valor,1)} M el ${dmy(hoyP.fecha)}`:"sin rueda equivalente el mes anterior"}</div>
      <div>Promedio por rueda: USD ${fmt(aC/r,1)} M hoy contra USD ${fmt(nP?aP/nP:0,1)} M en ${lab(kP).toLowerCase()}</div>
    </div>`:`<p class="big">Todavía no hay ruedas en ${lab(kC)}.</p>`;

  const n=Math.max(C.length,P.length);
  const acc=a=>{let t=0;return a.map(x=>t+=x.valor);};
  const val=a=>mode==="cum"?acc(a):a.map(x=>x.valor);
  const series=[[kP,P,"--prev"],[kC,C,"--cur"]];
  const g=css("--line"),m=css("--muted"),font={family:"IBM Plex Mono",size:11};
  const base={responsive:true,maintainAspectRatio:false,interaction:{mode:"index",intersect:false},
    plugins:{legend:{labels:{color:m,boxWidth:12,font}}},
    scales:{x:{grid:{display:false},ticks:{color:m,font}},y:{grid:{color:g},ticks:{color:m,font,callback:v=>fmt(v)+" M"}}}};
  c1?.destroy();
  c1=new Chart(document.getElementById("c1"),{type:mode==="cum"?"line":"bar",
    data:{labels:Array.from({length:n},(_,i)=>`R${i+1}`),datasets:series.map(([k,a,c])=>({label:lab(k),data:val(a),
      backgroundColor:css(c),borderColor:css(c),borderRadius:3,tension:.25,pointRadius:mode==="cum"?2:0,
      categoryPercentage:.8,barPercentage:.9}))},
    options:{...base,plugins:{...base.plugins,tooltip:{callbacks:{title:it=>`Rueda ${it[0].dataIndex+1}`,
      label:it=>{const r=series[it.datasetIndex][1][it.dataIndex];return r?`${it.dataset.label}: USD ${fmt(it.raw,1)} M (${dmy(r.fecha)}${r.prov?", provisorio":""})`:null;}}}}}});
  document.getElementById("t1").textContent=mode==="cum"?"Compras acumuladas por rueda":"Compras por rueda";

  const R=D.reservas.filter(x=>[kP,kC].includes(ym(x.fecha)));
  c2?.destroy();
  c2=new Chart(document.getElementById("c2"),{type:"line",
    data:{labels:R.map(x=>x.fecha.slice(8)+"/"+x.fecha.slice(5,7)),datasets:[{data:R.map(x=>x.valor),borderColor:css("--res"),backgroundColor:css("--res")+"33",fill:true,borderWidth:2.5,pointRadius:0,tension:.25}]},
    options:{...base,plugins:{legend:{display:false},tooltip:{callbacks:{label:it=>`USD ${fmt(it.raw)} M`}}}}});

}
document.querySelectorAll("[data-mode]").forEach(b=>b.onclick=()=>{mode=b.dataset.mode;document.querySelectorAll("[data-mode]").forEach(x=>x.setAttribute("aria-pressed",x===b));render();});
matchMedia("(prefers-color-scheme: dark)").addEventListener("change",()=>{render();bandas();alimentos();cclmep();meses12();historicos();personas();reservasGob();canasta();pobreza();inflacion();});
render();
function avisos(){const A=D.avisos||[], el=document.getElementById("avisos"); el.innerHTML=A.length?`<section class="panel" style="border-color:var(--bad)"><div class="k" style="color:var(--bad)">Avisos de la última actualización</div><ul class="n" style="margin:6px 0 0;padding-left:18px">${A.map(a=>`<li>${a.replace(/</g,"&lt;")}</li>`).join("")}</ul></section>`:"";}
avisos();

// Pestañas de funciones (estilo terminal)
function verTab(t){
  document.querySelectorAll("[data-sec]").forEach(el=>el.classList.toggle("oculto", t!=="todo" && !el.dataset.sec.split(" ").includes(t)));
  document.querySelectorAll("#fk button").forEach(b=>b.setAttribute("aria-pressed", b.dataset.tab===t));
  window.scrollTo(0,0);
}
document.querySelectorAll("#fk button").forEach(b=>b.onclick=()=>{verTab(b.dataset.tab); history.replaceState(null,"",b.dataset.tab==="todo"?location.pathname:"#"+b.dataset.tab);});
document.addEventListener("keydown",e=>{ if(e.target.tagName==="SELECT"||e.target.tagName==="INPUT") return; const n=+e.key; const bs=[...document.querySelectorAll("#fk button")]; if(n>=1&&n<=bs.length) bs[n-1].click(); });
{const h=location.hash.slice(1); if(h&&document.querySelector(`#fk button[data-tab="${h}"]`)) verTab(h);}
// Cinta de cotizaciones
(function cinta(){
  const I=D.indicadores||{}, X=D.dolar||{}, it=[];
  const ult=a=>a&&a.length?a[a.length-1]:null, pre=a=>a&&a.length>1?a[a.length-2]:null;
  const chg=(u,p,dec,suf="")=>{if(!u||!p) return ""; const d=u.valor-p.valor; return `<span class="${d>0?"up":d<0?"down":""}">${d>0?"+":""}${fmt(d,dec)}${suf}</span>`;};
  const add=(n,a,dec)=>{const u=ult(a); if(u) it.push(`<span class="t"><b>${n}</b>${fmt(u.valor,dec)} ${chg(u,pre(a),dec)}</span>`);};
  {const C=D.compras||[]; const u=C[C.length-1]; if(u) it.push(`<span class="t"><b>BCRA COMPRA</b><span class="${u.valor>0?"up":u.valor<0?"down":""}">${u.valor>0?"+":""}${fmt(u.valor,1)} M</span> ${dmy(u.fecha).slice(0,5)}${u.prov?"P":""}</span>`);}
  add("RES",I.reservas,0); add("EMBI",I.riesgo,0); add("A3500",X.mayorista,2); add("MINOR",I.minorista,2); add("TAMAR",I.tamar,2); add("BADLAR",I.badlar,2);
  const B=X.brechas||{}; const cc=ult(B.ccl), mp=ult(B.mep), bl=ult(B.blue);
  if(mp) it.push(`<span class="t"><b>MEP</b>${fmt(mp.precio,2)}</span>`); if(cc) it.push(`<span class="t"><b>CCL</b>${fmt(cc.precio,2)}</span>`); if(bl) it.push(`<span class="t"><b>BLUE</b>${fmt(bl.precio,2)}</span>`);
  (D.bonos||[]).forEach(g=>g.filas.filter(r=>["AL30","GD30","GD35","GD38"].includes(r.t)).forEach(r=>it.push(`<span class="t"><b>${r.t}</b>${fmt(r.precio,2)} <span class="${r.d1>0?"up":r.d1<0?"down":""}">${r.d1!=null?(r.d1>0?"+":"")+fmt(r.d1,2)+"%":""}</span></span>`)));
  (D.adrs||[]).filter(r=>["GGAL","YPF","PAM","BMA"].includes(r.t)).forEach(r=>it.push(`<span class="t"><b>${r.t}</b>${fmt(r.precio,2)} <span class="${r.d1>0?"up":r.d1<0?"down":""}">${r.d1!=null?(r.d1>0?"+":"")+fmt(r.d1,2)+"%":""}</span></span>`));
  document.getElementById("tick").innerHTML=it.join("");
})();
(function reloj(){const el=document.getElementById("clk"); const f=()=>{el.textContent=new Date().toLocaleString("es-AR",{day:"2-digit",month:"2-digit",year:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false})+" ART";}; f(); setInterval(f,30000);})();
// Redibuja cuando termina de cargar la tipografía, para que las etiquetas midan bien
if(document.fonts&&document.fonts.ready) document.fonts.ready.then(()=>{render();bandas();alimentos();cclmep();meses12();historicos();personas();reservasGob();canasta();pobreza();inflacion();});
// Recarga la página cada 10 minutos para tomar la última versión generada
// Recarga salteando la caché del navegador (agrega ?t=… al link)
const recargar=()=>location.replace(location.pathname+"?t="+Date.now()+location.hash);
setInterval(()=>{ if(!document.hidden) recargar(); }, 10*60*1000);
// Si hay una versión más nueva publicada, recarga sola al abrir o volver a la pestaña
async function hayNueva(){
  if(location.protocol==="file:") return;
  try{ const r=await fetch(location.pathname+"?chk="+Date.now(),{cache:"no-store"}); const t=await r.text();
    const m=t.match(/"generado":\s*"([^"]+)"/); if(m&&m[1]!==D.generado) recargar(); }catch(e){}
}
hayNueva(); document.addEventListener("visibilitychange",()=>{ if(!document.hidden) hayNueva(); });
</script>
</body>
</html>"""

if __name__ == "__main__":
    main()

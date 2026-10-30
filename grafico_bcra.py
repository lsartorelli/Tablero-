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
import argparse, datetime as dt, json, os, ssl, sys, urllib.parse, urllib.request, urllib.error, webbrowser

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
BONOS = [("Bonares (ley local)", ["AO27", "AO28", "AO29", "AN29", "AL29", "AL30", "AL35", "AE38", "AL41"]),
         ("Globales (ley NY)", ["GD29", "GD30", "GD35", "GD38", "GD41", "GD46"])]
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

    # --- Bonos soberanos en USD MEP
    bonos = []
    try:
        vivo = {x["symbol"]: x for x in get_json(BONOS_URL)}
        try:
            with open(HIST_LOCAL, encoding="utf-8") as f:
                local = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            local = {}
        if hoy.weekday() < 5:  # guarda el precio del día de cada bono
            for _, tks in BONOS:
                for t in tks:
                    v = vivo.get(t + "D")
                    if v and v.get("c"):
                        local.setdefault(t, {})[hoy.isoformat()] = float(v["c"])
            with open(HIST_LOCAL, "w", encoding="utf-8") as f:
                json.dump(local, f, ensure_ascii=False, indent=1)
        inicio_anio = f"{hoy.year}-01-01"
        for grupo, tickers in BONOS:
            filas_b = []
            for t in tickers:
                try:
                    try:
                        hh = get_json(HIST_BONO_URL.format(t=t + "D"))
                        h = [x for x in hh if isinstance(x, dict) and x.get("c")]
                    except Exception:
                        h = []
                    if not h and local.get(t):  # sin histórico en data912: usar el que guarda el script
                        h = [{"date": f, "c": c} for f, c in sorted(local[t].items())]
                    v = vivo.get(t + "D")
                    if not (v and v.get("c")) and not h:
                        continue
                    ult = float(v["c"]) if v and v.get("c") else h[-1]["c"]
                    cierres = [x["c"] for x in h]
                    if v and h and h[-1]["date"] == hoy.isoformat():
                        cierres = cierres[:-1]  # el histórico ya trae hoy: comparar contra cierres anteriores
                    ref = lambda n: cierres[-n] if len(cierres) >= n else None
                    prev_anio = [x["c"] for x in h if x["date"] < inicio_anio]
                    pc = lambda a: (ult / a - 1) * 100 if a else None
                    filas_b.append({"t": t, "precio": ult,
                                    "d1": float(v["pct_change"]) if v and v.get("pct_change") is not None else pc(ref(1)),
                                    "d5": pc(ref(5)), "m1": pc(ref(21)), "ytd": pc(prev_anio[-1] if prev_anio else None)})
                except Exception as e:
                    aviso(f"sin datos para {t} ({e})")
            bonos.append({"grupo": grupo, "filas": filas_b})
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
    alimentos = {}
    try:
        ids = [IPC_GBA] + [x[2] for x in ALIMENTOS]
        filas = get_json(SERIES_URL.format(ids=",".join(ids)))["data"]
        ipc = {r[0]: r[1] for r in filas if r[1]}
        base_mes = max(ipc)
        for i, (clave, nombre, _) in enumerate(ALIMENTOS, start=2):
            pts = [{"fecha": r[0], "nominal": r[i], "real": r[i] * ipc[base_mes] / ipc[r[0]]}
                   for r in filas if r[i] is not None and r[0] in ipc]
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
.fk{display:flex;flex-wrap:wrap;gap:2px;margin:0 -8px;padding:4px 8px;border-bottom:1px solid #222;background:#000}
.fk button{background:#1c1c1c;color:#ddd;border:0;border-radius:0;padding:3px 10px;font:500 11.5px "IBM Plex Mono",monospace;cursor:pointer}
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

  <section data-sec="bonos" class="panel" id="pbon">
    <div class="phead"><h2>Bonos soberanos en dólares (MEP)</h2></div>
    <div class="tablewrap"><table class="mkt" id="tbon"></table></div>
    <p class="n" style="margin:8px 0 0">Precio cada 100 nominales en USD MEP (especie D) y variación porcentual. Fuente: data912.</p>
  </section>

  <section data-sec="adrs" class="panel" id="padr">
    <div class="phead"><h2>ADRs argentinos en Nueva York</h2></div>
    <div class="tablewrap"><table class="mkt" id="tadr"></table></div>
    <p class="n" style="margin:8px 0 0">Precio en USD y variación porcentual (cierre o último precio disponible). Fuente: Yahoo Finance.</p>
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

// Tablas de mercado
function celda(v){
  if(v==null) return '<td class="pc">–</td>';
  const a=Math.min(Math.abs(v)/15,1), base=v>=0?"--good":"--bad";
  return `<td class="pc" style="color:var(${base});background:color-mix(in srgb,var(${base}) ${Math.round(6+a*28)}%,transparent)">${v>0?"+":""}${fmt(v,2)} %</td>`;
}
(function mercados(){
  const B=D.bonos||[], A=D.adrs||[], cols="<th>Precio</th><th>1 día</th><th>5 días</th><th>1 mes</th><th>En el año</th>";
  if(B.some(g=>g.filas.length)){
    document.getElementById("tbon").innerHTML=`<thead><tr><th>Bono</th>${cols}</tr></thead><tbody>`+
      B.map(g=>`<tr class="grp"><td colspan="6">${g.grupo}</td></tr>`+g.filas.map(r=>`<tr><td class="t">${r.t}</td><td>${fmt(r.precio,2)}</td>${celda(r.d1)}${celda(r.d5)}${celda(r.m1)}${celda(r.ytd)}</tr>`).join("")).join("")+"</tbody>";
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
  cmp.className="cmp"+(aC-aP<0?" neg":"");
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
matchMedia("(prefers-color-scheme: dark)").addEventListener("change",()=>{render();bandas();alimentos();cclmep();meses12();inflacion();});
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
if(document.fonts&&document.fonts.ready) document.fonts.ready.then(()=>{render();bandas();alimentos();cclmep();meses12();inflacion();});
// Recarga la página cada 10 minutos para tomar la última versión generada
setInterval(()=>{ if(!document.hidden) location.reload(); }, 10*60*1000);
</script>
</body>
</html>"""

if __name__ == "__main__":
    main()


import os
import re
import json
import time
import sqlite3
import logging
import unicodedata
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote

import requests

# ==========================================
# CONFIGURACIÓN
# ==========================================

API_URL = "https://api2.mercadopublico.cl/v2/compra-agil"

TICKET = os.getenv("CHILECOMPRA_TICKET", "").strip()

DB_PATH = os.getenv("DB_PATH", "oportunidades.db")

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "").strip()
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID", "").strip()
WHATSAPP_DESTINO = os.getenv("WHATSAPP_DESTINO", "").strip()
WHATSAPP_TEMPLATE = os.getenv(
    "WHATSAPP_TEMPLATE", "alerta_compra_agil"
).strip()
WHATSAPP_LANGUAGE = os.getenv(
    "WHATSAPP_LANGUAGE", "es"
).strip()
WHATSAPP_API_VERSION = os.getenv(
    "WHATSAPP_API_VERSION", ""
).strip()

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# Primera ejecución: revisar última hora.
# Posteriores: revisar desde el último ciclo exitoso.
VENTANA_INICIAL_MINUTOS = 2880
SOLAPAMIENTO_MINUTOS = 15

REGIONES = {
    5: "Valparaíso",
    13: "Metropolitana"
}

ZONA_CHILE = ZoneInfo("America/Santiago")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# ==========================================
# PALABRAS CLAVE
# ==========================================

CATEGORIAS = {
    "Pantallas LED": [
        "pantalla led", "pantallas led",
        "pantalla gigante", "pantallas gigantes",
        "videowall", "video wall",
        "muro led", "muro de video",
        "panel led", "paneles led",
        "pantalla modular", "pantalla publicitaria",
        "pantalla para escenario",
        "pantalla para eventos",
        "pantalla audiovisual",
        "pantalla exterior",
        "pantalla de gran formato",
        "arriendo de pantalla",
        "alquiler de pantalla",
        "montaje de pantalla",
        "instalacion de pantalla",
        "pantalla p3", "pantalla p4",
        "pantalla p5", "pantalla p6",
        "led wall"
    ],
    "Carpas": [
        "carpa", "carpas", "toldo", "toldos",
        "carpa estructural", "carpa modular",
        "carpa para eventos",
        "carpa tipo araña", "carpa plegable",
        "carpa impermeable",
        "arriendo de carpas",
        "alquiler de carpas",
        "montaje de carpas",
        "instalacion de carpas",
        "carpa tensada",
        "carpa industrial",
        "cubierta para eventos",
        "domo para eventos",
        "estructura para eventos"
    ],
    "Audio": [
        "audio profesional",
        "amplificacion", "amplificacion profesional",
        "servicio de amplificacion",
        "sonorizacion", "sonido profesional",
        "sonido para eventos",
        "arriendo de sonido",
        "arriendo de audio",
        "sistema pa", "line array",
        "parlantes", "altavoces",
        "microfonos", "microfonia",
        "consola de sonido",
        "mesa de sonido",
        "sistema de sonido",
        "equipamiento de audio",
        "produccion sonora",
        "refuerzo sonoro",
        "ingenieria de sonido"
    ],
    "Iluminación": [
        "iluminacion", "iluminacion escenica",
        "iluminacion profesional",
        "iluminacion para eventos",
        "iluminacion arquitectonica",
        "arriendo de iluminacion",
        "luces para eventos",
        "luces de escenario",
        "focos led", "cabezas moviles",
        "moving head", "beam",
        "wash light", "par led",
        "consola de iluminacion",
        "equipamiento luminico",
        "iluminacion de espectaculos"
    ],
    "Eventos": [
        "produccion de eventos",
        "productora de eventos",
        "organizacion de eventos",
        "servicios para eventos",
        "servicio de eventos",
        "evento corporativo",
        "evento municipal",
        "evento institucional",
        "evento cultural",
        "evento masivo",
        "festival", "concierto",
        "espectaculo", "espectaculos",
        "produccion tecnica",
        "montaje de eventos",
        "equipamiento audiovisual",
        "servicio audiovisual",
        "arriendo audiovisual",
        "escenario", "escenarios",
        "tarima", "tarimas",
        "estructura escenica",
        "feria expositiva",
        "feria costumbrista",
        "actividad recreativa",
        "actividad artistica",
        "actividad cultural"
    ]
}

# Términos demasiado amplios, como "evento",
# "audio" o "led" por sí solos, se evitan
# para reducir falsos positivos.

# ==========================================
# FUNCIONES DE TEXTO
# ==========================================

def normalizar(texto):
    texto = str(texto or "").lower()
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(
        c for c in texto
        if not unicodedata.combining(c)
    )
    texto = re.sub(r"[^a-z0-9]+", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def detectar_coincidencias(texto):
    contenido = " " + normalizar(texto) + " "
    resultados = {}

    for categoria, palabras in CATEGORIAS.items():
        encontradas = []

        for palabra in palabras:
            patron = " " + normalizar(palabra) + " "
            if patron in contenido:
                encontradas.append(palabra)

        if encontradas:
            resultados[categoria] = sorted(set(encontradas))

    return resultados


def obtener_texto(item):
    partes = [
        item.get("nombre", ""),
        item.get("descripcion", "")
    ]

    for producto in item.get("productos_solicitados", []) or []:
        partes.append(producto.get("nombre", ""))
        partes.append(producto.get("descripcion", ""))

    return " ".join(str(p or "") for p in partes)


def fecha_utc(valor):
    if not valor:
        return None

    try:
        fecha = datetime.fromisoformat(
            str(valor).replace("Z", "+00:00")
        )
        if fecha.tzinfo is None:
            fecha = fecha.replace(tzinfo=timezone.utc)
        return fecha.astimezone(timezone.utc)
    except ValueError:
        return None


def mostrar_fecha(valor):
    fecha = fecha_utc(valor)
    if not fecha:
        return "No informada"

    return fecha.astimezone(
        ZONA_CHILE
    ).strftime("%d/%m/%Y %H:%M")


def formato_monto(valor, moneda="CLP"):
    if valor is None:
        return "No informado"

    try:
        numero = float(valor)
        if moneda == "CLP":
            return "$" + f"{numero:,.0f}".replace(",", ".") + " CLP"
        return f"{numero:,.2f} {moneda}"
    except (ValueError, TypeError):
        return str(valor)


# ==========================================
# BASE DE DATOS SQLITE
# ==========================================

def conectar_db():
    conexion = sqlite3.connect(DB_PATH)

    conexion.execute("""
        CREATE TABLE IF NOT EXISTS oportunidades (
            codigo TEXT PRIMARY KEY,
            nombre TEXT,
            descripcion TEXT,
            categoria TEXT,
            coincidencias TEXT,
            region TEXT,
            organismo TEXT,
            presupuesto TEXT,
            cierre TEXT,
            enlace TEXT,
            estado TEXT,
            notificado INTEGER DEFAULT 0,
            fecha_registro TEXT,
            fecha_notificacion TEXT
        )
    """)

    conexion.execute("""
        CREATE TABLE IF NOT EXISTS configuracion (
            clave TEXT PRIMARY KEY,
            valor TEXT
        )
    """)

    conexion.commit()
    return conexion


def leer_config(conexion, clave):
    fila = conexion.execute(
        "SELECT valor FROM configuracion WHERE clave=?",
        (clave,)
    ).fetchone()

    return fila[0] if fila else None


def guardar_config(conexion, clave, valor):
    conexion.execute("""
        INSERT INTO configuracion(clave, valor)
        VALUES (?, ?)
        ON CONFLICT(clave)
        DO UPDATE SET valor=excluded.valor
    """, (clave, valor))
    conexion.commit()


def guardar_oportunidad(conexion, oportunidad):
    conexion.execute("""
        INSERT INTO oportunidades (
            codigo, nombre, descripcion,
            categoria, coincidencias,
            region, organismo, presupuesto,
            cierre, enlace, estado,
            fecha_registro
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(codigo) DO UPDATE SET
            nombre=excluded.nombre,
            descripcion=excluded.descripcion,
            categoria=excluded.categoria,
            coincidencias=excluded.coincidencias,
            region=excluded.region,
            organismo=excluded.organismo,
            presupuesto=excluded.presupuesto,
            cierre=excluded.cierre,
            enlace=excluded.enlace,
            estado=excluded.estado
    """, (
        oportunidad["codigo"],
        oportunidad["nombre"],
        oportunidad["descripcion"],
        oportunidad["categoria"],
        oportunidad["coincidencias"],
        oportunidad["region"],
        oportunidad["organismo"],
        oportunidad["presupuesto"],
        oportunidad["cierre"],
        oportunidad["enlace"],
        oportunidad["estado"],
        datetime.now(timezone.utc).isoformat()
    ))

    conexion.commit()


# ==========================================
# API CHILECOMPRA
# ==========================================

SESION = requests.Session()


def consultar_api(ruta, parametros=None):
    if not TICKET:
        raise RuntimeError(
            "Falta CHILECOMPRA_TICKET."
        )

    url = API_URL + ruta

    for intento in range(3):
        try:
            respuesta = SESION.get(
                url,
                headers={
                    "ticket": TICKET,
                    "Accept": "application/json"
                },
                params=parametros,
                timeout=45
            )

            if respuesta.status_code == 429:
                raise RuntimeError(
                    "Cuota de API agotada (429). "
                    "Reintentar en un ciclo posterior."
                )

            if respuesta.status_code in (401, 403):
                raise RuntimeError(
                    "Ticket inválido, inactivo o sin permisos."
                )

            if respuesta.status_code >= 500:
                raise requests.HTTPError(
                    f"Error temporal HTTP {respuesta.status_code}"
                )

            respuesta.raise_for_status()

            datos = respuesta.json()

            if datos.get("success") != "OK":
                raise RuntimeError(
                    f"Error API: {datos.get('errors')}"
                )

            return datos.get("payload")

        except (requests.RequestException, ValueError) as error:
            if intento == 2:
                raise

            logging.warning(
                "Fallo temporal API: %s", error
            )
            time.sleep(2 ** intento)

    raise RuntimeError("No fue posible consultar la API")


def listar_compras(desde, hasta):
    pagina = 1
    resultados = []

    while True:
        parametros = {
            "cambio_desde": desde.isoformat().replace("+00:00", "Z"),
            "cambio_hasta": hasta.isoformat().replace("+00:00", "Z"),
            "estado": "publicada",
            "region": "13,5",
            "tamano_pagina": 50,
            "numero_pagina": pagina,
            "ordenar_por": "FechaUltimaModificacion"
        }

        payload = consultar_api("", parametros)

        if not isinstance(payload, dict):
            raise RuntimeError("Respuesta de listado inesperada")

        items = payload.get("items", [])
        paginacion = payload.get("paginacion", {})

        resultados.extend(items)

        total_paginas = int(
            paginacion.get("total_paginas", 1)
        )

        logging.info(
            "Página %s/%s: %s registros",
            pagina, total_paginas, len(items)
        )

        if pagina >= total_paginas:
            break

        pagina += 1

        # Pausa moderada entre páginas.
        time.sleep(0.3)

    return resultados


def obtener_detalle(codigo):
    return consultar_api("/" + quote(codigo, safe=""))


# ==========================================
# WHATSAPP CLOUD API
# ==========================================

def whatsapp_configurado():
    return all([
        WHATSAPP_TOKEN,
        WHATSAPP_PHONE_ID,
        WHATSAPP_DESTINO,
        WHATSAPP_API_VERSION
    ])


def enviar_whatsapp(oportunidad):
    mensaje = (
        "🚨 NUEVA COMPRA ÁGIL\n\n"
        f"ID: {oportunidad['codigo']}\n"
        f"Nombre: {oportunidad['nombre']}\n"
        f"Región: {oportunidad['region']}\n"
        f"Organismo: {oportunidad['organismo']}\n"
        f"Presupuesto: {oportunidad['presupuesto']}\n"
        f"Cierre: {oportunidad['cierre']}\n"
        f"Categoría: {oportunidad['categoria']}\n"
        f"Coincidencias: {oportunidad['coincidencias']}\n"
        f"Enlace: {oportunidad['enlace']}"
    )

    if DRY_RUN:
        print("\n" + "=" * 50)
        print("SIMULACIÓN DE WHATSAPP")
        print(mensaje)
        print("=" * 50 + "\n")
        return False

    if not whatsapp_configurado():
        logging.warning(
            "WhatsApp no configurado. Alerta pendiente: %s",
            oportunidad["codigo"]
        )
        return False

    # Requiere una plantilla aprobada por Meta:
    # alerta_compra_agil, idioma es,
    # con una variable de texto en el cuerpo: {{1}}.
    #
    # La variable se limita a un resumen compacto
    # para evitar superar los límites de plantilla.

    resumen = (
        f"ID: {oportunidad['codigo']}. "
        f"{oportunidad['nombre'][:100]}. "
        f"Región: {oportunidad['region']}. "
        f"Presupuesto: {oportunidad['presupuesto']}. "
        f"Cierre: {oportunidad['cierre']}. "
        f"Organismo: {oportunidad['organismo'][:70]}. "
        f"Categoría: {oportunidad['categoria'][:80]}."
    )

    url = (
        f"https://graph.facebook.com/"
        f"{WHATSAPP_API_VERSION}/"
        f"{WHATSAPP_PHONE_ID}/messages"
    )

    payload = {
        "messaging_product": "whatsapp",
        "to": WHATSAPP_DESTINO,
        "type": "template",
        "template": {
            "name": WHATSAPP_TEMPLATE,
            "language": {
                "code": WHATSAPP_LANGUAGE
            },
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {
                            "type": "text",
                            "text": resumen
                        }
                    ]
                }
            ]
        }
    }

    respuesta = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {WHATSAPP_TOKEN}",
            "Content-Type": "application/json"
        },
        json=payload,
        timeout=30
    )

    if not respuesta.ok:
        logging.error(
            "Error enviando WhatsApp: HTTP %s - %s",
            respuesta.status_code,
            respuesta.text[:500]
        )
        return False

    logging.info(
        "WhatsApp aceptado por Meta: %s",
        oportunidad["codigo"]
    )

    # Aceptado por Meta no significa entregado al
    # destinatario. Para confirmar entrega se
    # necesita configurar un webhook de estados.
    return True


# ==========================================
# PROCESAMIENTO
# ==========================================

def construir_oportunidad(detalle, coincidencias):
    institucion = detalle.get("institucion") or {}
    fechas = detalle.get("fechas") or {}
    presupuesto = detalle.get("presupuesto") or {}
    montos = detalle.get("montos") or {}

    codigo = str(detalle.get("codigo", ""))

    region_id = institucion.get("region")

    try:
        region_id = int(region_id)
    except (ValueError, TypeError):
        region_id = None

    if region_id not in REGIONES:
        return None

    estado = (detalle.get("estado") or {}).get(
        "codigo", ""
    )

    if estado != "publicada":
        return None

    cierre_raw = fechas.get("fecha_cierre")
    cierre_dt = fecha_utc(cierre_raw)

    if cierre_dt and cierre_dt <= datetime.now(timezone.utc):
        return None

    moneda = (
        presupuesto.get("moneda")
        or montos.get("moneda")
        or "CLP"
    )

    monto = presupuesto.get("monto_disponible")

    if monto is None:
        monto = presupuesto.get("presupuesto_estimado")

    if monto is None:
        monto = montos.get("monto_disponible")

    categorias = list(coincidencias.keys())

    palabras = []
    for lista in coincidencias.values():
        palabras.extend(lista)

    # Se usa un enlace de búsqueda del portal.
    # La URL específica de ficha debe verificarse
    # en la interfaz actual de Mercado Público.
    enlace = (
        "https://www.mercadopublico.cl/"
        "?buscar=" + quote(codigo)
    )

    return {
        "codigo": codigo,
        "nombre": detalle.get("nombre", ""),
        "descripcion": detalle.get("descripcion", ""),
        "categoria": ", ".join(categorias),
        "coincidencias": ", ".join(sorted(set(palabras))),
        "region": REGIONES[region_id],
        "organismo": institucion.get(
            "organismo_comprador", "No informado"
        ),
        "presupuesto": formato_monto(monto, moneda),
        "cierre": mostrar_fecha(cierre_raw),
        "enlace": enlace,
        "estado": estado
    }



def procesar(conexion, item):
    codigo = str(item.get("codigo", "")).strip()

    if not codigo:
        return

    # Primer filtro: información del listado
    texto_preliminar = obtener_texto(item)
    coincidencias_previas = detectar_coincidencias(
        texto_preliminar
    )

    if not coincidencias_previas:
        return

    logging.info(
        "Posible coincidencia: %s | %s",
        codigo,
        ", ".join(coincidencias_previas.keys())
    )

    # Consultar detalle solo si coincide
    detalle = obtener_detalle(codigo)

    if not isinstance(detalle, dict):
        return

    texto_completo = obtener_texto(detalle)
    coincidencias = detectar_coincidencias(
        texto_completo
    )

    if not coincidencias:
        return

    oportunidad = construir_oportunidad(
        detalle, coincidencias
    )

    if oportunidad is None:
        return

    guardar_oportunidad(conexion, oportunidad)

    logging.info(
        "Coincidencia confirmada: %s | %s",
        codigo, oportunidad["categoria"]
    )


def enviar_pendientes(conexion):
    filas = conexion.execute("""
        SELECT codigo, nombre, descripcion,
               categoria, coincidencias,
               region, organismo, presupuesto,
               cierre, enlace, estado
        FROM oportunidades
        WHERE notificado=0
        ORDER BY fecha_registro ASC
        LIMIT 30
    """).fetchall()

    columnas = [
        "codigo", "nombre", "descripcion",
        "categoria", "coincidencias",
        "region", "organismo", "presupuesto",
        "cierre", "enlace", "estado"
    ]

    for fila in filas:
        oportunidad = dict(zip(columnas, fila))

        try:
            enviado = enviar_whatsapp(oportunidad)

            if enviado:
                conexion.execute("""
                    UPDATE oportunidades
                    SET notificado=1,
                        fecha_notificacion=?
                    WHERE codigo=?
                """, (
                    datetime.now(timezone.utc).isoformat(),
                    oportunidad["codigo"]
                ))
                conexion.commit()

            # Evitar ráfagas de mensajes.
            time.sleep(1)

        except requests.RequestException as error:
            logging.error(
                "Error WhatsApp %s: %s",
                oportunidad["codigo"], error
            )


def main():
    logging.info("Iniciando monitor de Compra Ágil")

    conexion = conectar_db()

    ahora = datetime.now(timezone.utc)

    ultima = None

    if ultima:
        desde = fecha_utc(ultima)

        if desde is None:
            raise RuntimeError("Fecha de control inválida")

        desde -= timedelta(
            minutes=SOLAPAMIENTO_MINUTOS
        )
    else:
        desde = ahora - timedelta(
            minutes=VENTANA_INICIAL_MINUTOS
        )

    if desde > ahora:
        desde = ahora - timedelta(hours=1)

    logging.info(
        "Consultando cambios desde %s hasta %s",
        desde.isoformat(), ahora.isoformat()
    )

    compras = listar_compras(desde, ahora)

    logging.info(
        "Compras recibidas: %s", len(compras)
    )

    # Si alguna consulta falla, no avanzamos
    # el cursor de sincronización.
    for item in compras:
        procesar(conexion, item)

    guardar_config(
        conexion,
        "ultima_revision_exitosa",
        ahora.isoformat()
    )

    enviar_pendientes(conexion)

    total = conexion.execute(
        "SELECT COUNT(*) FROM oportunidades"
    ).fetchone()[0]

    pendientes = conexion.execute(
        "SELECT COUNT(*) FROM oportunidades WHERE notificado=0"
    ).fetchone()[0]

    logging.info(
        "Total coincidencias: %s | Pendientes: %s",
        total, pendientes
    )

    conexion.close()


if __name__ == "__main__":
    main()

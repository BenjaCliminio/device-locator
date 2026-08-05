import sys
import json
import socket
import subprocess
import platform
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from typing import Optional
 
import requests
 
# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
 
MYLNIKOV_GEOLOCATE_URL = "https://api.mylnikov.org/geolocation/wifi"
IP_FALLBACK_URL = "http://ip-api.com/json/"
GPS_SERIAL_PORT = None  # ej: "/dev/ttyUSB0" o "COM3" si hay un módulo GPS serial conectado
GPS_BAUDRATE = 9600
OUTPUT_MAP_FILE = "ubicacion.html"
OUTPUT_REPORT_FILE = "reporte_ubicacion.json"
 
 
@dataclass
class LocationResult:
    source: str            # "gps" | "wifi" | "ip"
    latitude: float
    longitude: float
    accuracy_m: Optional[float]
    timestamp: str
    raw_provider_data: Optional[dict] = None
 
 
# ---------------------------------------------------------------------------
# Paso 0: Consentimiento explícito
# ---------------------------------------------------------------------------
 
def pedir_consentimiento() -> bool:
    print("=" * 70)
    print(" DEVICE LOCATOR - Herramienta de geolocalización")
    print("=" * 70)
    print(
        "\nEste programa va a intentar obtener la ubicación geográfica de\n"
        "ESTE dispositivo (el equipo donde se está ejecutando el script).\n\n"
        "Solo debe usarse en dispositivos propios o con autorización\n"
        "explícita del propietario. El uso indebido puede ser ilegal.\n"
    )
    respuesta = input(
        "¿Confirmás que estás ejecutando esto en tu propio dispositivo "
        "y que contás con autorización para hacerlo? [s/n]: "
    ).strip().lower()
    return respuesta in ("s", "si", "sí", "y", "yes")
 
 
# ---------------------------------------------------------------------------
# Paso 1: GPS real (si hay módulo conectado)
# ---------------------------------------------------------------------------
 
def intentar_gps(puerto: Optional[str] = GPS_SERIAL_PORT) -> Optional[LocationResult]:
    """
    Intenta leer coordenadas de un módulo GPS conectado por puerto serie
    (ej. NEO-6M via USB-TTL). Requiere pyserial y pynmea2.
    Si no hay puerto configurado o no hay módulo, devuelve None y el
    script sigue con el siguiente método (fallback).
    """
    if not puerto:
        print("[GPS] No hay puerto serie configurado. Se omite este método.")
        return None
 
    try:
        import serial
        import pynmea2
    except ImportError:
        print("[GPS] Faltan dependencias (pyserial, pynmea2). Se omite este método.")
        return None
 
    try:
        with serial.Serial(puerto, GPS_BAUDRATE, timeout=5) as ser:
            for _ in range(50):  # intenta leer hasta 50 líneas NMEA
                linea = ser.readline().decode("ascii", errors="replace").strip()
                if linea.startswith("$GPGGA") or linea.startswith("$GNGGA"):
                    msg = pynmea2.parse(linea)
                    if msg.latitude and msg.longitude:
                        print("[GPS] Coordenadas obtenidas por GPS real.")
                        return LocationResult(
                            source="gps",
                            latitude=msg.latitude,
                            longitude=msg.longitude,
                            accuracy_m=None,
                            timestamp=datetime.now(timezone.utc).isoformat(),
                        )
    except Exception as e:
        print(f"[GPS] No se pudo leer el módulo GPS: {e}")
 
    return None
 
 
# ---------------------------------------------------------------------------
# Paso 2: Triangulación por WiFi
# ---------------------------------------------------------------------------
 
def escanear_redes_wifi() -> list:
    """
    Escanea redes WiFi cercanas y devuelve una lista de puntos de acceso
    con su MAC (BSSID) y nivel de señal. La implementación varía según
    el sistema operativo.
    """
    sistema = platform.system()
    redes = []
 
    try:
        if sistema == "Linux":
            salida = subprocess.check_output(
                ["nmcli", "-f", "BSSID,SIGNAL", "dev", "wifi", "list"],
                text=True, stderr=subprocess.DEVNULL
            )
            for linea in salida.strip().split("\n")[1:]:
                partes = linea.rsplit(None, 1)
                if len(partes) == 2:
                    bssid, señal = partes
                    redes.append({"macAddress": bssid.replace("\\:", ":"),
                                  "signalStrength": -100 + int(señal)})
 
        elif sistema == "Windows":
            import re
            salida = subprocess.check_output(
                ["netsh", "wlan", "show", "networks", "mode=Bssid"],
                text=True, stderr=subprocess.DEVNULL
            )
            # No confiamos en las etiquetas en inglés ("BSSID", "Signal") porque
            # varían según el idioma de Windows (ej. "Señal" en español).
            # En su lugar, buscamos directamente patrones de MAC y de porcentaje.
            mac_regex = re.compile(r"([0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}")
            pct_regex = re.compile(r"(\d{1,3})\s*%")
 
            bssid_actual = None
            for linea in salida.split("\n"):
                linea = linea.strip()
                mac_match = mac_regex.search(linea)
                pct_match = pct_regex.search(linea)
 
                if mac_match:
                    bssid_actual = mac_match.group(0)
                elif pct_match and bssid_actual:
                    señal_pct = int(pct_match.group(1))
                    redes.append({"macAddress": bssid_actual,
                                  "signalStrength": -100 + señal_pct})
                    bssid_actual = None
 
        elif sistema == "Darwin":  # macOS
            salida = subprocess.check_output(
                ["/System/Library/PrivateFrameworks/Apple80211.framework/"
                 "Versions/Current/Resources/airport", "-s"],
                text=True, stderr=subprocess.DEVNULL
            )
            for linea in salida.strip().split("\n")[1:]:
                campos = linea.split()
                if len(campos) >= 3:
                    redes.append({"macAddress": campos[1], "signalStrength": int(campos[2])})
 
    except Exception as e:
        print(f"[WiFi] No se pudo escanear redes: {e}")
 
    return redes
 
 
def intentar_wifi() -> Optional[LocationResult]:
    print("[WiFi] Escaneando redes cercanas...")
    redes = escanear_redes_wifi()
    print(f"[WiFi] Redes detectadas: {len(redes)}")
 
    if len(redes) < 2:
        print("[WiFi] No se detectaron suficientes redes para triangular (mínimo 2). Se omite este método.")
        return None
 
    # La API de Mylnikov espera un string "mac,señal;mac,señal;..." codificado en Base64
    import base64
    partes = [f"{r['macAddress']},{r['signalStrength']}" for r in redes[:20]]
    search_string = ";".join(partes)
    search_b64 = base64.b64encode(search_string.encode()).decode()
 
    params = {"v": "1.1", "data": "open", "search": search_b64}
 
    try:
        resp = requests.get(MYLNIKOV_GEOLOCATE_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
 
        if data.get("result") != 200:
            print(f"[WiFi] La API no encontró coincidencias para las redes detectadas.")
            return None
 
        info = data["data"]
        print("[WiFi] Coordenadas obtenidas por triangulación WiFi.")
        return LocationResult(
            source="wifi",
            latitude=float(info["lat"]),
            longitude=float(info["lon"]),
            accuracy_m=float(info["range"]) if info.get("range") else None,
            timestamp=datetime.now(timezone.utc).isoformat(),
            raw_provider_data=data,
        )
    except Exception as e:
        print(f"[WiFi] Falló la geolocalización por WiFi: {e}")
        return None
 
 
# ---------------------------------------------------------------------------
# Paso 3: Fallback por IP pública
# ---------------------------------------------------------------------------
 
def intentar_ip() -> Optional[LocationResult]:
    print("[IP] Consultando geolocalización por IP pública...")
    try:
        resp = requests.get(IP_FALLBACK_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
 
        if data.get("status") != "success":
            print(f"[IP] El proveedor devolvió un error: {data.get('message', 'desconocido')}")
            return None
 
        print("[IP] Coordenadas aproximadas obtenidas por IP (baja precisión).")
        return LocationResult(
            source="ip",
            latitude=data["lat"],
            longitude=data["lon"],
            accuracy_m=None,  # la geolocalización por IP no reporta un radio de precisión fiable
            timestamp=datetime.now(timezone.utc).isoformat(),
            raw_provider_data=data,
        )
    except Exception as e:
        print(f"[IP] Falló la geolocalización por IP: {e}")
        return None
 
 
# ---------------------------------------------------------------------------
# Reporte y mapa
# ---------------------------------------------------------------------------
 
def generar_reporte(resultado: LocationResult):
    with open(OUTPUT_REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(asdict(resultado), f, indent=2, ensure_ascii=False)
    print(f"\n[Reporte] Guardado en {OUTPUT_REPORT_FILE}")
 
 
def generar_mapa(resultado: LocationResult):
    try:
        import folium
    except ImportError:
        print("[Mapa] Falta la librería 'folium'. Instalá con: pip install folium")
        return
 
    mapa = folium.Map(location=[resultado.latitude, resultado.longitude], zoom_start=15)
    popup_texto = (
        f"Fuente: {resultado.source.upper()}<br>"
        f"Lat: {resultado.latitude:.6f}<br>"
        f"Lon: {resultado.longitude:.6f}<br>"
        f"Precisión: {resultado.accuracy_m or 'N/D'} m<br>"
        f"Hora (UTC): {resultado.timestamp}"
    )
    folium.Marker(
        [resultado.latitude, resultado.longitude],
        popup=popup_texto,
        tooltip="Ubicación detectada",
        icon=folium.Icon(color="red", icon="crosshairs", prefix="fa"),
    ).add_to(mapa)
 
    if resultado.accuracy_m:
        folium.Circle(
            [resultado.latitude, resultado.longitude],
            radius=resultado.accuracy_m,
            color="red",
            fill=True,
            fill_opacity=0.15,
        ).add_to(mapa)
 
    mapa.save(OUTPUT_MAP_FILE)
    print(f"[Mapa] Guardado en {OUTPUT_MAP_FILE} (abrilo en un navegador)")
 
 
# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
 
def main():
    if not pedir_consentimiento():
        print("\nOperación cancelada por el usuario. Saliendo.")
        sys.exit(0)
 
    print("\nIniciando cadena de geolocalización (GPS -> WiFi -> IP)...\n")
 
    resultado = intentar_gps()
    if resultado is None:
        resultado = intentar_wifi()
    if resultado is None:
        resultado = intentar_ip()
 
    if resultado is None:
        print("\nNo se pudo determinar la ubicación por ningún método disponible.")
        sys.exit(1)
 
    print("\n" + "=" * 70)
    print(" RESULTADO")
    print("=" * 70)
    print(f" Fuente:      {resultado.source.upper()}")
    print(f" Latitud:     {resultado.latitude}")
    print(f" Longitud:    {resultado.longitude}")
    print(f" Precisión:   {resultado.accuracy_m or 'N/D'} m")
    print(f" Timestamp:   {resultado.timestamp}")
    print("=" * 70)
 
    generar_reporte(resultado)
    generar_mapa(resultado)
 
 
if __name__ == "__main__":
    main()
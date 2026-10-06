"""Ferretería El Tornillo · POS con facturación electrónica (Factus) y cobros QR (Factus Pay).

Local:   pip install -r requirements.txt  ->  copia .env.example a .env  ->  python app.py  ->  http://localhost:5000
Render:  gunicorn app:app   (las credenciales van como variables de entorno)
"""
import json, os, time, datetime, base64, requests
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, send_file, Response

try:  # en local lee el archivo .env (en Render se usan las variables de entorno del servicio)
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass
env = os.environ.get

API = env("FACTUS_URL", "https://api-sandbox.factus.com.co")
CRED = {"grant_type": "password", "client_id": env("FACTUS_CLIENT_ID"), "client_secret": env("FACTUS_CLIENT_SECRET"),
        "username": env("FACTUS_USERNAME"), "password": env("FACTUS_PASSWORD")}
PAY = env("FACTUS_PAY_URL", "https://pay-api-sandbox.factus.com.co")
PAY_CRED = {"email": env("FACTUS_PAY_EMAIL"), "password": env("FACTUS_PAY_PASSWORD")}
DB = env("DB_FILE", "tienda.json")
IVA, RENTA = 0.19, float(env("TASA_RENTA", "0.35"))   # RENTA: tarifa renta persona jurídica (estimado)
BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)

def load(): return json.load(open(DB, encoding="utf-8")) if os.path.exists(DB) else {"productos": [], "docs": []}
def save(d): json.dump(d, open(DB, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
def headers():
    tk = requests.post(f"{API}/oauth/token", data=CRED, headers={"Accept": "application/json"}).json()["access_token"]
    return {"Authorization": f"Bearer {tk}", "Accept": "application/json"}

def totales(o):
    """Busca dentro de la respuesta de Factus el bloque con gross_amount / tax_amount / total"""
    if isinstance(o, dict):
        if "tax_amount" in o and "total" in o: return o
        o = list(o.values())
    if isinstance(o, list):
        for v in o:
            r = totales(v)
            if r: return r

def rango(h, palabra):
    """ID del rango activo cuyo documento contiene 'palabra' (ej. 'factura de venta', 'soporte')"""
    l = requests.get(f"{API}/v2/numbering-ranges", params={"filter[is_active]": 1}, headers=h).json().get("data", [])
    l = l.get("data", l) if isinstance(l, dict) else l
    for x in l:
        s = str(x.get("document", "")).lower()
        if palabra in s and "nota" not in s and "ajuste" not in s and not x.get("is_expired"): return x["id"]

def limpiar_pendientes(h):
    """Factus no deja facturar si hay una factura sin validar ante la DIAN: las busca y las elimina"""
    l = requests.get(f"{API}/v2/bills", params={"filter[status]": 0, "filter[per_page]": 50}, headers=h).json().get("data", [])
    l = l.get("data", l) if isinstance(l, dict) else l
    borradas = []
    for b in l or []:
        ref = b.get("reference_code")
        if ref and requests.delete(f"{API}/v2/bills/destroy/reference/{ref}", headers=h).ok: borradas.append(ref)
    print("Facturas pendientes eliminadas:", borradas)
    return borradas

def dv(nit):
    """Dígito de verificación DIAN de un NIT"""
    w = [3, 7, 13, 17, 19, 23, 29, 37, 41, 43, 47, 53, 59, 67, 71]
    r = sum(int(c) * w[i] for i, c in enumerate(reversed(nit))) % 11
    return str(r if r < 2 else 11 - r)

def item(p, cant, precio, extra={}):
    return {"code_reference": p["id"], "name": p["nombre"], "quantity": f"{cant:.2f}", "price": f"{precio:.2f}",
            "unit_measure_code": "94", "standard_code": "999", "taxes": [{"code": "01", "rate": "19.00"}], **extra}

def registrar(db, tipo, r, desc, cant, base):
    t = totales(r.json()) or {}
    gross = float(t.get("gross_amount") or base)
    db["docs"].append({"tipo": tipo, "number": r.json()["data"].get("number"), "fecha": str(datetime.date.today()),
                       "producto": desc, "cantidad": cant, "gross": gross,
                       "tax": float(t.get("tax_amount") or gross * IVA), "total": float(t.get("total") or gross * (1 + IVA))})

def cobrar(ref, monto):
    if not PAY_CRED["email"]: return {"message": "Factus Pay no configurado"}
    tk = requests.post(f"{PAY}/auth", json=PAY_CRED, headers={"Accept": "application/json"}).json()["token"]
    for _ in range(5):
        d = requests.post(f"{PAY}/v1/collections", json={"reference_code": ref, "amount": monto},
                          headers={"Authorization": f"Bearer {tk}", "Accept": "application/json"}).json()
        if d.get("data", {}).get("qr"): break
        time.sleep(1.5)
    return d

@app.get("/")
def home(): return send_file(os.path.join(BASE, "index.html"))

@app.get("/api/productos")
def productos(): return jsonify(load()["productos"])

@app.post("/api/comprar")   # GASTO: compra a proveedor -> Documento Soporte en Factus
def comprar():
    f, db, h = request.json, load(), headers()
    cant, costo = int(f["cantidad"]), float(f["costo"])
    p = next((x for x in db["productos"] if x["id"] == f.get("id")), None)
    if not p:
        p = {"id": f"P{len(db['productos']) + 1:03}", "nombre": f["nombre"], "costo": costo, "precio": float(f["precio"]), "stock": 0}
        db["productos"].append(p)
    body = {"reference_code": f"JP-C-{int(time.time())}", "numbering_range_id": rango(h, "soporte"),
            "payment_details": [{"payment_form": "1", "payment_method_code": "10", "amount": f"{cant * costo * (1 + IVA):.2f}"}],
            "provider": {"identification_document_code": "31", "identification": f["nit"], "dv": dv(f["nit"]),
                         "names": f.get("proveedor") or "Proveedor Mayorista", "address": "Calle 1 # 2-3",
                         "country_code": "CO", "municipality_code": "11001", "legal_organization_code": "1"},
            "items": [item(p, cant, costo, {"discount_rate": "0.00", "period": {"start_date": str(datetime.date.today()), "generation_mode": "2"}})]}
    r = requests.post(f"{API}/v2/support-documents/validate", json=body, headers=h)
    if not r.ok: return jsonify(r.json()), r.status_code
    p["stock"] += cant; p["costo"] = costo
    registrar(db, "compra", r, f"{p['nombre']} ×{cant}", cant, cant * costo); save(db)
    return jsonify(r.json())

@app.post("/api/vender")    # INGRESO: venta con varios productos -> 1 Factura electrónica + cobro Factus Pay
def vender():
    f, db, h = request.json, load(), headers()
    lineas = []
    for it in f["items"]:
        p, cant = next(x for x in db["productos"] if x["id"] == it["id"]), int(it["cantidad"])
        if p["stock"] < cant: return jsonify({"message": f"Solo hay {p['stock']} unidades de {p['nombre']}"}), 400
        lineas.append((p, cant))
    base = sum(c * p["precio"] for p, c in lineas); total = base * (1 + IVA)
    body = {"reference_code": f"JP-V-{int(time.time())}", "document": "01", "operation_type": "10",
            "numbering_range_id": rango(h, "factura de venta"),
            "payment_details": [{"payment_form": "1", "payment_method_code": "10", "amount": f"{total:.2f}"}],
            "customer": {"identification_document_code": "13", "identification": f["cedula"], "names": f["cliente"],
                         "email": f.get("email") or "cliente@ejemplo.com",
                         "legal_organization_code": "2", "country_code": "CO", "municipality_code": "11001"},
            "items": [item(p, c, p["precio"]) for p, c in lineas]}
    r = requests.post(f"{API}/v2/bills/validate", json=body, headers=h)
    if r.status_code == 409 and limpiar_pendientes(h):   # "factura pendiente por enviar a la DIAN" -> limpiar y reintentar
        body["reference_code"] += "-R"
        r = requests.post(f"{API}/v2/bills/validate", json=body, headers=h)
    if not r.ok: return jsonify(r.json()), r.status_code
    for p, c in lineas: p["stock"] -= c
    registrar(db, "venta", r, ", ".join(f"{p['nombre']} ×{c}" for p, c in lineas), sum(c for _, c in lineas), base); save(db)
    j = r.json()
    if total >= 10000: j["pago"] = cobrar(j["data"]["number"], round(total, 2))
    return jsonify(j)

@app.get("/pdf/<tipo>/<number>")  # Descarga el PDF desde Factus y lo muestra en el navegador
def pdf(tipo, number):
    ruta = "bills" if tipo == "venta" else "support-documents"
    j = requests.get(f"{API}/v2/{ruta}/{number}/download-pdf", headers=headers()).json()
    d = j.get("data", j)
    if not d.get("pdf_base_64_encoded"): return jsonify(j), 404
    return Response(base64.b64decode(d["pdf_base_64_encoded"]), mimetype="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{d.get("file_name", number)}.pdf"'})

@app.post("/api/reiniciar")  # Deja la tienda en cero para una demo (los documentos en Factus no se borran)
def reiniciar():
    save({"productos": [], "docs": []})
    return jsonify(ok=True)

@app.get("/api/dashboard")  # Lee cada factura / documento soporte directamente desde Factus
def dashboard():
    db, h = load(), headers()
    def sync(d):
        ruta = "bills" if d["tipo"] == "venta" else "support-documents"
        try:
            t = totales(requests.get(f"{API}/v2/{ruta}/{d['number']}", headers=h, timeout=15).json())
            if t: d.update(gross=float(t["gross_amount"]), tax=float(t["tax_amount"]), total=float(t["total"]), fuente="Factus")
        except Exception: pass
        return d
    docs = list(ThreadPoolExecutor(8).map(sync, db["docs"]))
    s = lambda tipo, k: round(sum(d[k] for d in docs if d["tipo"] == tipo), 2)
    ing, gas, iva_g, iva_d = s("venta", "gross"), s("compra", "gross"), s("venta", "tax"), s("compra", "tax")
    ub = ing - gas; renta = round(max(ub, 0) * RENTA, 2)
    return jsonify({"ingreso_bruto": ing, "gasto_bruto": gas, "utilidad_bruta": ub, "iva_generado": iva_g,
                    "iva_descontable": iva_d, "iva_a_pagar": round(iva_g - iva_d, 2), "renta": renta,
                    "impuestos_total": round(iva_g - iva_d + renta, 2), "utilidad_neta": round(ub - renta, 2),
                    "tasa_renta": RENTA, "docs": docs})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(env("PORT", "5000")), debug=env("DEBUG", "1") == "1")


import os, re, json, smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from pathlib import Path
from playwright.sync_api import sync_playwright

DOCTOR = os.getenv("DOCTOR_APELLIDO", "LAHSEN")
BASE = "https://reserva.clinicalascondes.cl/agendaweb/"
MODOS = {"Presencial": r"Consulta\s+Presencial", "Telemedicina": r"Consulta\s+Telemedicina"}
STATE = Path("state/estado_v3.json")
OUT = Path("out")
STATE.parent.mkdir(exist_ok=True)
OUT.mkdir(exist_ok=True)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")

FECHA_PROX_RE = re.compile(r"pr[oó]xima\s+fecha\s+disponible:?\s*(\d{1,2}-\d{1,2}-\d{4})", re.I)
HORA_LINEA_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


class Bloqueado(Exception):
    pass


def a_fecha(s):
    try:
        return datetime.strptime(s or "", "%d-%m-%Y").date()
    except Exception:
        return None


def bloqueado(page):
    try:
        t = (page.title() + " " + page.inner_text("body")[:2000]).lower()
    except Exception:
        return False
    return any(s in t for s in ("verificación de seguridad", "ser humano", "just a moment", "verify you are human"))


def clic_si_existe(page, patron):
    for rol in ("button", "tab", "link"):
        el = page.get_by_role(rol, name=re.compile(patron, re.I))
        if el.count():
            try:
                el.first.click(timeout=5000)
                page.wait_for_timeout(3000)
                return True
            except Exception:
                pass
    return False


def clic_texto(page, patron, espera=3000):
    el = page.get_by_text(re.compile(patron, re.I))
    if el.count():
        el.first.click(timeout=10000)
        page.wait_for_timeout(espera)
        return True
    return False


def diagnostico(page):
    try:
        info = {"url": page.url, "titulo": page.title()}
        info["campos"] = page.eval_on_selector_all(
            "input:not([type=radio]), textarea, [role=combobox]",
            "els => els.map(e => ({tipo: e.type || e.getAttribute('role'), placeholder: e.placeholder, "
            "visible: !!(e.offsetWidth || e.offsetHeight)}))")
        info["botones"] = page.eval_on_selector_all(
            "button, a, [role=button], [role=option], label",
            "els => els.map(e => (e.innerText || '').trim()).filter(t => t && t.length < 60).slice(0, 60)")
        info["texto"] = page.inner_text("body")[:2000]
        return json.dumps(info, ensure_ascii=False, indent=1)
    except Exception as e:
        return f"(sin diagnóstico: {e})"


def ir_al_menu(page):
    page.goto(BASE, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(8000)
    if bloqueado(page):
        raise Bloqueado()


def marcar_por_profesional(page):
    patron = re.compile(r"Por\s+Profesional", re.I)
    op = page.locator("label, button, [role=radio]").filter(has_text=patron)
    try:
        op.first.wait_for(state="visible", timeout=15000)
        op.first.click()
    except Exception:
        page.get_by_label(patron).first.check(force=True, timeout=10000)
    page.wait_for_timeout(3000)


def elegir_doctor(page):
    for sel in page.locator("select:visible").all():
        textos = sel.eval_on_selector_all("option", "os => os.map(o => o.text)")
        match = [t for t in textos if DOCTOR.lower() in t.lower()]
        if match:
            sel.select_option(label=match[0])
            page.wait_for_timeout(4000)
            return
    campo = page.locator(
        "input[placeholder*='profesional' i]:visible, input[placeholder*='médico' i]:visible, "
        "input[placeholder*='medico' i]:visible, input[placeholder*='nombre' i]:visible, "
        "input[placeholder*='buscar' i]:visible, [role=combobox]:visible, "
        "input[type='search']:visible, input[type='text']:visible"
    )
    if not campo.count():
        raise RuntimeError("Marqué 'Por Profesional' pero no encontré dónde buscar al doctor")
    campo.first.click(timeout=10000)
    campo.first.type(DOCTOR, delay=120)
    page.wait_for_timeout(5000)
    opcion = page.get_by_role("option", name=re.compile(DOCTOR, re.I))
    if not opcion.count():
        opcion = page.locator("li, div[class*='option' i]").filter(has_text=re.compile(DOCTOR, re.I))
    if not opcion.count():
        raise RuntimeError("Escribí LAHSEN pero no apareció en la lista")
    opcion.first.click(timeout=10000)
    page.wait_for_timeout(4000)


def revisar(page, modo, patron_boton):
    try:
        if not clic_texto(page, patron_boton, 6000):
            ir_al_menu(page)
            if not clic_texto(page, patron_boton, 6000):
                raise RuntimeError(f"No encontré el botón {modo} en el menú")
        if bloqueado(page):
            raise Bloqueado()
        if modo == "Presencial":
            clic_texto(page, r"CL[IÍ]NICA\s+LAS\s+CONDES\s+ESTORIL")
        marcar_por_profesional(page)
        elegir_doctor(page)
        clic_si_existe(page, r"buscar|ver horas|continuar|siguiente|consultar")
        page.wait_for_timeout(8000)
        clic_si_existe(page, r"m[aá]s\s+horas")

        texto = page.inner_text("body")
        page.screenshot(path=str(OUT / f"{modo}.png"), full_page=True)
        m = FECHA_PROX_RE.search(texto)
        horas = {l.strip() for l in texto.splitlines() if HORA_LINEA_RE.match(l.strip())}
        return {"fecha": m.group(1) if m else None,
                "horas": sorted(horas, key=lambda h: [int(x) for x in h.split(":")])}
    except Bloqueado:
        raise
    except Exception as e:
        try:
            page.screenshot(path=str(OUT / f"{modo}-error.png"), full_page=True)
        except Exception:
            pass
        raise RuntimeError(f"{str(e)[:300]}\n\nDIAGNÓSTICO {modo}:\n{diagnostico(page)}")


def resumen(modo, r):
    horas = " · ".join(r["horas"]) or "—"
    return f"{modo}\nPróxima fecha: {r['fecha'] or 'sin horas publicadas'}\nHoras: {horas}"


def enviar_correo(asunto, cuerpo, adjuntos=()):
    user, pwd = os.getenv("GMAIL_USER"), os.getenv("GMAIL_APP_PASSWORD")
    to = os.getenv("ALERT_TO") or user
    if not (user and pwd):
        print(asunto, "\n", cuerpo)
        return
    msg = MIMEMultipart()
    msg["Subject"], msg["From"], msg["To"] = asunto, user, to
    msg.attach(MIMEText(cuerpo, "plain", "utf-8"))
    for f in adjuntos:
        p = Path(f)
        if p.exists():
            msg.attach(MIMEImage(p.read_bytes(), name=p.name))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pwd)
        s.send_message(msg)


def main():
    previo = json.loads(STATE.read_text()) if STATE.exists() else {}
    estado, exitos, errores, bloqueo = dict(previo), {}, [], False

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(locale="es-CL", user_agent=UA, viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        try:
            ir_al_menu(page)
            for modo, patron in MODOS.items():
                try:
                    exitos[modo] = revisar(page, modo, patron)
                    estado[modo] = exitos[modo]
                except Bloqueado:
                    bloqueo = True
                    break
                except Exception as e:
                    errores.append(f"[{modo}] {e}")
        except Bloqueado:
            bloqueo = True
        browser.close()

    estado["_bloqueos"] = previo.get("_bloqueos", 0) + 1 if bloqueo else 0
    STATE.write_text(json.dumps(estado, ensure_ascii=False, indent=1))
    pie = f"\n\nReserva: {BASE}\nTeléfono: 22 610 8000 opción 1"

    if estado["_bloqueos"] == 3:
        enviar_correo("🛑 Monitor Dr. Lahsen: la CLC lo está bloqueando",
                      "La verificación de seguridad frenó al monitor 3 veces seguidas. "
                      "Seguirá intentando, pero revisa también a mano." + pie)
    if errores:
        enviar_correo("⚠️ Monitor Dr. Lahsen: no pudo revisar",
                      "\n\n".join(errores) + pie, sorted(OUT.glob("*-error.png")))

    primera, alertas = [], []
    for modo, r in exitos.items():
        ant = previo.get(modo)
        if ant is None:
            primera.append(modo)
            continue
        f_ant, f_nueva = a_fecha(ant.get("fecha")), a_fecha(r["fecha"])
        if f_nueva and (not f_ant or f_nueva < f_ant):
            alertas.append(f"🔥 FECHA MÁS PRONTA (antes: {ant.get('fecha') or 'sin horas'})\n{resumen(modo, r)}")
        elif f_nueva and f_nueva == f_ant and set(r["horas"]) - set(ant.get("horas", [])):
            nuevas = sorted(set(r["horas"]) - set(ant.get("horas", [])))
            alertas.append(f"Horas nuevas ese día: {' · '.join(nuevas)}\n{resumen(modo, r)}")

    if primera:
        enviar_correo("✅ Monitor Dr. Lahsen funcionando",
                      "\n\n".join(resumen(m, exitos[m]) for m in primera) + pie,
                      [OUT / f"{m}.png" for m in primera])
    if alertas:
        mejor = min((exitos[m]["fecha"] for m in exitos if exitos[m]["fecha"]), key=a_fecha, default="")
        enviar_correo(f"🚨 Hora Dr. Lahsen disponible: {mejor}", "\n\n".join(alertas) + pie)
    print("Bloqueo:", bloqueo, "| Errores:", len(errores), "| Alertas:", len(alertas))


if __name__ == "__main__":
    main()

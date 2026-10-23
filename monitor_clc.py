
import os, re, json, smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from pathlib import Path
from playwright.sync_api import sync_playwright

DOCTOR = os.getenv("DOCTOR_APELLIDO", "LAHSEN")
RUT = os.getenv("PACIENTE_RUT", "")
BASE = "https://reserva.clinicalascondes.cl/agendaweb/"
MODOS = {"Presencial": r"Consulta\s+Presencial", "Telemedicina": r"Consulta\s+Telemedicina"}
STATE = Path("state/estado_v2.json")
OUT = Path("out")
STATE.parent.mkdir(exist_ok=True)
OUT.mkdir(exist_ok=True)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")

HORA_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")
FECHA_RE = re.compile(
    r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})\b|"
    r"\b(lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)\b[^\n]{0,25}\d{1,2}",
    re.I,
)
CLAVES_JSON = ("fecha", "hora", "inicio", "slot", "cupo", "disponib")


class Bloqueado(Exception):
    pass


def bloqueado(page):
    try:
        t = (page.title() + " " + page.inner_text("body")[:2000]).lower()
    except Exception:
        return False
    return any(s in t for s in ("verificación de seguridad", "ser humano", "just a moment", "verify you are human"))


def extraer_de_json(obj, acc):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (str, int)) and any(c in k.lower() for c in CLAVES_JSON):
                s = str(v)
                if HORA_RE.search(s) or FECHA_RE.search(s):
                    acc.add(f"{k}={s}")
            else:
                extraer_de_json(v, acc)
    elif isinstance(obj, list):
        for x in obj:
            extraer_de_json(x, acc)


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


def diagnostico(page):
    try:
        info = {"url": page.url, "titulo": page.title()}
        info["campos"] = page.eval_on_selector_all(
            "input, select, textarea",
            "els => els.map(e => ({tipo: e.type, placeholder: e.placeholder, name: e.name, "
            "id: e.id, visible: !!(e.offsetWidth || e.offsetHeight)}))")
        info["botones"] = page.eval_on_selector_all(
            "button, a, [role=button], [role=tab], [role=option], li",
            "els => els.map(e => (e.innerText || '').trim()).filter(t => t && t.length < 60).slice(0, 60)")
        info["texto"] = page.inner_text("body")[:1500]
        return json.dumps(info, ensure_ascii=False, indent=1)
    except Exception as e:
        return f"(sin diagnóstico: {e})"


def ir_al_menu(page):
    page.goto(BASE, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(8000)
    if bloqueado(page):
        raise Bloqueado()


def revisar(page, modo, patron_boton, capturas):
    capturas.clear()
    try:
        boton = page.get_by_text(re.compile(patron_boton, re.I))
        if not boton.count():
            page.go_back()
            page.wait_for_timeout(5000)
            boton = page.get_by_text(re.compile(patron_boton, re.I))
        if not boton.count():
            ir_al_menu(page)
            boton = page.get_by_text(re.compile(patron_boton, re.I))
        boton.first.click(timeout=10000)
        page.wait_for_timeout(6000)
        if bloqueado(page):
            raise Bloqueado()

        if RUT:
            rut = page.locator("input[placeholder*='rut' i]:visible, input[name*='rut' i]:visible")
            if rut.count():
                rut.first.fill(RUT)
                page.keyboard.press("Enter")
                page.wait_for_timeout(4000)

        clic_si_existe(page, r"(por )?(m[eé]dico|profesional|doctor)")

        campo = page.locator(
            "input[placeholder*='médico' i]:visible, input[placeholder*='medico' i]:visible, "
            "input[placeholder*='profesional' i]:visible, input[placeholder*='nombre' i]:visible, "
            "input[placeholder*='buscar' i]:visible, input[type='search']:visible"
        )
        if not campo.count():
            campo = page.locator("input[type='text']:visible, input:not([type]):visible")
        if not campo.count():
            raise RuntimeError("No encontré un buscador visible después de elegir el tipo de consulta")

        campo.first.click(timeout=10000)
        campo.first.type(DOCTOR, delay=120)
        page.wait_for_timeout(5000)
        opcion = page.get_by_text(re.compile(DOCTOR, re.I))
        if opcion.count():
            opcion.first.click(timeout=10000)
            page.wait_for_timeout(4000)
        clic_si_existe(page, r"buscar|ver horas|continuar|siguiente")
        page.wait_for_timeout(8000)

        texto = page.inner_text("body")
        page.screenshot(path=str(OUT / f"{modo}.png"))
        (OUT / f"{modo}.txt").write_text(texto, encoding="utf-8")
        (OUT / f"{modo}.json").write_text(json.dumps(capturas, ensure_ascii=False)[:5_000_000], encoding="utf-8")

        horas = set()
        for linea in texto.splitlines():
            linea = linea.strip()
            if linea and len(linea) < 120 and (HORA_RE.search(linea) or FECHA_RE.search(linea)):
                horas.add(linea)
        for c in capturas:
            extraer_de_json(c["body"], horas)
        return sorted(horas)
    except Bloqueado:
        raise
    except Exception as e:
        try:
            page.screenshot(path=str(OUT / f"{modo}-error.png"))
        except Exception:
            pass
        raise RuntimeError(f"{str(e)[:300]}\n\nDIAGNÓSTICO {modo}:\n{diagnostico(page)}")


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
    capturas = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(locale="es-CL", user_agent=UA, viewport={"width": 1366, "height": 900})
        page = ctx.new_page()

        def on_resp(r):
            try:
                if "json" in (r.headers.get("content-type") or ""):
                    capturas.append({"url": r.url, "body": r.json()})
            except Exception:
                pass

        page.on("response", on_resp)
        try:
            ir_al_menu(page)
            for modo, patron in MODOS.items():
                try:
                    exitos[modo] = revisar(page, modo, patron, capturas)
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
    link = f"Agenda: {BASE}\nTeléfono: 22 610 8000 opción 1"

    if estado["_bloqueos"] == 3:
        enviar_correo("🛑 Monitor Dr. Lahsen: la CLC lo está bloqueando",
                      f"La verificación de seguridad de la CLC frenó al monitor 3 veces seguidas. "
                      f"Seguirá intentando, pero revisa también a mano.\n\n{link}")

    if errores:
        enviar_correo("⚠️ Monitor Dr. Lahsen: no pudo revisar",
                      "\n\n".join(errores) + f"\n\n{link}",
                      sorted(OUT.glob("*-error.png")))

    primera = {m: h for m, h in exitos.items() if m not in previo}
    if primera:
        cuerpo = "\n".join(f"\n[{m}] {len(h)} hallazgos:\n" + "\n".join(h[:40]) for m, h in primera.items())
        enviar_correo("✅ Monitor Dr. Lahsen funcionando", f"Esto detectó:\n{cuerpo}\n\n{link}",
                      [OUT / f"{m}.png" for m in primera])

    nuevas = {m: [h for h in hs if h not in set(previo.get(m, []))]
              for m, hs in exitos.items() if m in previo}
    nuevas = {m: hs for m, hs in nuevas.items() if hs}
    if nuevas:
        cuerpo = "\n".join(f"\n[{m}]\n" + "\n".join(hs[:30]) for m, hs in nuevas.items())
        enviar_correo("🚨 Nueva hora Dr. Lahsen - CLC", f"Horas nuevas:\n{cuerpo}\n\nReserva YA:\n{link}")
    print("Bloqueo:", bloqueo, "| Errores:", len(errores), "| Nuevas:", nuevas or "ninguna")


if __name__ == "__main__":
    main()

import os, re, json, smtplib, sys
from email.mime.text import MIMEText
from pathlib import Path
from playwright.sync_api import sync_playwright

DOCTOR = os.getenv("DOCTOR_APELLIDO", "LAHSEN")
RUT = os.getenv("PACIENTE_RUT", "")
MODOS = {
    "Presencial": "https://reserva.clinicalascondes.cl/agendaweb/",
    "Telemedicina": "https://reserva.clinicalascondes.cl/agendaWeb/telemedicina",
}
STATE = Path("state/state.json")
OUT = Path("out")
STATE.parent.mkdir(exist_ok=True)
OUT.mkdir(exist_ok=True)

HORA_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")
FECHA_RE = re.compile(
    r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})\b|"
    r"\b(lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)\b[^\n]{0,25}\d{1,2}",
    re.I,
)
CLAVES_JSON = ("fecha", "hora", "inicio", "slot", "cupo", "disponib")


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
    btn = page.get_by_role("button", name=re.compile(patron, re.I))
    if btn.count():
        btn.first.click()
        page.wait_for_timeout(3000)


def revisar(browser, modo, url):
    page = browser.new_page(locale="es-CL")
    capturas = []

    def on_resp(r):
        try:
            if "json" in (r.headers.get("content-type") or ""):
                capturas.append({"url": r.url, "body": r.json()})
        except Exception:
            pass

    page.on("response", on_resp)
    page.goto(url, wait_until="networkidle", timeout=90000)

    if RUT:
        rut = page.locator("input[placeholder*='rut' i], input[name*='rut' i]")
        if rut.count():
            rut.first.fill(RUT)
            page.keyboard.press("Enter")
            page.wait_for_timeout(3000)

    campo = page.locator(
        "input[placeholder*='médico' i], input[placeholder*='medico' i], "
        "input[placeholder*='profesional' i], input[placeholder*='nombre' i], "
        "input[placeholder*='buscar' i]"
    )
    if not campo.count():
        campo = page.locator("input[type='text'], input:not([type])")
    campo.first.click()
    campo.first.type(DOCTOR, delay=120)
    page.wait_for_timeout(4000)

    opcion = page.get_by_text(re.compile(DOCTOR, re.I))
    if opcion.count():
        opcion.first.click()
        page.wait_for_timeout(3000)
    clic_si_existe(page, r"buscar|ver horas|continuar|siguiente")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(5000)

    texto = page.inner_text("body")
    page.screenshot(path=str(OUT / f"{modo}.png"), full_page=True)
    (OUT / f"{modo}.txt").write_text(texto, encoding="utf-8")
    (OUT / f"{modo}.json").write_text(json.dumps(capturas, ensure_ascii=False, indent=1)[:5_000_000], encoding="utf-8")

    horas = set()
    for linea in texto.splitlines():
        linea = linea.strip()
        if linea and len(linea) < 120 and (HORA_RE.search(linea) or FECHA_RE.search(linea)):
            horas.add(linea)
    for c in capturas:
        extraer_de_json(c["body"], horas)
    page.close()
    return sorted(horas)


def enviar_correo(asunto, cuerpo):
    user, pwd = os.getenv("GMAIL_USER"), os.getenv("GMAIL_APP_PASSWORD")
    to = os.getenv("ALERT_TO") or user
    if not (user and pwd):
        print("Sin credenciales de correo:\n", asunto, "\n", cuerpo)
        return
    msg = MIMEText(cuerpo, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = asunto, user, to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pwd)
        s.send_message(msg)


def main():
    previo = json.loads(STATE.read_text()) if STATE.exists() else None
    actual, errores = {}, []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for modo, url in MODOS.items():
            try:
                actual[modo] = revisar(browser, modo, url)
            except Exception as e:
                errores.append(f"{modo}: {e}")
                actual[modo] = (previo or {}).get(modo, [])
        browser.close()

    STATE.write_text(json.dumps(actual, ensure_ascii=False, indent=1))
    links = "\n".join(f"{m}: {u}" for m, u in MODOS.items())

    if previo is None:
        resumen = "\n".join(f"\n[{m}] {len(h)} hallazgos:\n" + "\n".join(h[:30]) for m, h in actual.items())
        enviar_correo(
            "✅ Monitor Dr. Lahsen activo (revisa que detecte bien)",
            f"Primera ejecución. Detectó:\n{resumen}\n\nErrores: {errores or 'ninguno'}\n\n{links}",
        )
        return

    nuevas = {m: [h for h in hs if h not in set(previo.get(m, []))] for m, hs in actual.items()}
    nuevas = {m: hs for m, hs in nuevas.items() if hs}
    if nuevas:
        cuerpo = "\n".join(f"\n[{m}]\n" + "\n".join(hs[:30]) for m, hs in nuevas.items())
        enviar_correo("🚨 Nueva hora Dr. Lahsen - CLC", f"Horas nuevas:\n{cuerpo}\n\nReserva YA:\n{links}\nTeléfono: 22 610 8000 opción 1")
    print("Nuevas:", nuevas or "ninguna", "| Errores:", errores or "ninguno")
    if errores and not any(actual.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()

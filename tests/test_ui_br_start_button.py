# -*- coding: utf-8 -*-
"""F-229: проверка кнопки ▶ BR-инстанса (ofBrRobotApi) на РЕАЛЬНОМ app.js.

Прогоняет tests/ui_br_start_button_sim.js (Node + vm) по 5 сценариям и проверяет инварианты:
  alive          — процесс жив: health → POST /start, C# и супервизор НЕ трогаем;
  dead-cs404-sup — процесса нет, C# не знает робота (404) → супервизор поднимает → /start уходит;
  dead-cs401-sup — сессия истекла (401) → супервизор поднимает → /start уходит;
  dead-all-fail  — ни C#, ни супервизор не смогли → внятная ошибка, /start НЕ отправлен;
  old-browser    — браузер без AbortSignal.timeout → путь не падает.

Запуск: python tests/test_ui_br_start_button.py  (проверяет src и bin копии app.js)
"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SIM = os.path.join(HERE, "ui_br_start_button_sim.js")
TARGETS = [
    os.path.join(REPO, "src", "Server", "wwwroot", "js", "app.js"),
    os.path.join(REPO, "src", "Server", "bin", "Debug", "net10.0", "wwwroot", "js", "app.js"),
]

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s %s" % (name, detail))


def run(app, scenario):
    out = os.path.join(tempfile.gettempdir(), "br_sim_%s.txt" % scenario)
    if os.path.exists(out):
        os.remove(out)
    r = subprocess.run(["node", SIM, app, scenario, out], capture_output=True, text=True, timeout=120)
    txt = ""
    if os.path.exists(out):
        with open(out, encoding="utf-8") as f:
            txt = f.read()
    if not txt.strip():
        raise RuntimeError("симулятор не дал отчёта: rc=%s stderr=%s" % (r.returncode, (r.stderr or "")[:300]))
    return txt


def field(txt, key):
    """Строка отчёта вида '  <key>: value' или '  <key> (3): value'."""
    m = re.search(r"^  %s(?: \(\d+\))?: (.*)$" % re.escape(key), txt, re.M)
    return m.group(1).strip() if m else ""


for app in TARGETS:
    label = "src" if "\\bin\\" not in app else "bin"
    print("=== %s: %s ===" % (label, app))
    check("%s: файл существует" % label, os.path.exists(app), app)
    if not os.path.exists(app):
        continue

    t = run(app, "alive")
    check("%s alive: /start отправлен" % label, field(t, "/start роботу") == "true", field(t, "/start роботу"))
    check("%s alive: C# не дёргали" % label, field(t, "C# process-start") == "false", field(t, "C# process-start"))
    check("%s alive: супервизор не дёргали" % label, field(t, "супервизор 5099") == "false", field(t, "супервизор 5099"))
    check("%s alive: успешный тост" % label, "запущен" in field(t, "тосты"), field(t, "тосты")[:160])

    t = run(app, "dead-cs404-sup")
    check("%s dead-cs404: C# вызван" % label, field(t, "C# process-start") == "true", field(t, "C# process-start"))
    check("%s dead-cs404: супервизор вызван" % label, field(t, "супервизор 5099") == "true", field(t, "супервизор 5099"))
    check("%s dead-cs404: /start отправлен" % label, field(t, "/start роботу") == "true", field(t, "/start роботу"))
    check("%s dead-cs404: успешный тост" % label, "запущен" in field(t, "тосты"), field(t, "тосты")[:160])

    t = run(app, "dead-cs401-sup")
    check("%s dead-cs401: 401 в журнале" % label, "401" in field(t, "журнал запуска"), field(t, "журнал запуска")[:200])
    check("%s dead-cs401: супервизор вызван" % label, field(t, "супервизор 5099") == "true", field(t, "супервизор 5099"))
    check("%s dead-cs401: /start отправлен" % label, field(t, "/start роботу") == "true", field(t, "/start роботу"))

    t = run(app, "dead-all-fail")
    check("%s dead-all-fail: /start НЕ отправлен" % label, field(t, "/start роботу") == "false", field(t, "/start роботу"))
    check("%s dead-all-fail: понятная ошибка" % label,
          "не поднялся" in field(t, "тосты") or "не поднял" in field(t, "журнал запуска"),
          field(t, "тосты")[:160])

    t = run(app, "old-browser")
    check("%s old-browser: без исключения" % label, field(t, "исключение") == "нет", field(t, "исключение"))
    check("%s old-browser: /start отправлен" % label, field(t, "/start роботу") == "true", field(t, "/start роботу"))

print("\n===== RESULTS: PASS=%d FAIL=%d =====" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)

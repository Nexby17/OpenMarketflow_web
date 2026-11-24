"""F-228: кнопка ▶ MXU6 — 4 сценария на РЕАЛЬНОМ коде app.js (без браузера).

Прогоняет настоящие функции showToast / robotStartLog / makeTimeoutSignal / ofMxRobotApi
из app.js в песочнице Node с фейковыми DOM и fetch.

usage:
    python tests/test_ui_start_button.py                 # проверяет ОПУБЛИКОВАННЫЙ app.js (bin, его отдаёт сервер)
    python tests/test_ui_start_button.py <путь_к_app.js>  # любой другой файл

Каждый сценарий обязан дать непустой отчёт без исключений; пустой отчёт = провал
(урок 23.11: пустой прогон симулятора не должен сходить за успех).
"""
import io
import json
import os
import re
import subprocess
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
SIM = os.path.join(HERE, "ui_start_button_sim.js")
OUTDIR = os.path.join(HERE, "_ui_start_button_out")
os.makedirs(OUTDIR, exist_ok=True)
PUBLISHED = os.path.join(PROJ, "src", "Server", "bin", "Debug", "net10.0", "wwwroot", "js", "app.js")
SOURCE = os.path.join(PROJ, "src", "Server", "wwwroot", "js", "app.js")
APP = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else (PUBLISHED if os.path.exists(PUBLISHED) else SOURCE)
print("app.js:", APP)
print("существует:", os.path.exists(APP))
SCEN = ("alive-ok", "session-401", "robot-dead", "old-browser")

results = {}
for sc in SCEN:
    out = os.path.join(OUTDIR, "sim_" + sc + ".txt")
    if os.path.exists(out):
        os.remove(out)
    try:
        r = subprocess.run(["node", SIM, APP, sc, out], capture_output=True, text=True, timeout=60)
        rc, err = r.returncode, (r.stderr or "")
    except FileNotFoundError:
        rc, err = -1, "node не найден в PATH"
    txt = io.open(out, encoding="utf-8").read() if os.path.exists(out) else ""
    if not txt.strip():
        txt = "ПУСТОЙ ОТЧЁТ\nnode exit=%s\nstderr=%s" % (rc, err[-500:])
    results[sc] = txt

for sc in SCEN:
    print(results[sc])


def field(txt, label):
    m = re.search(re.escape(label) + r":\s*(.*)", txt)
    return m.group(1).strip() if m else None


def toasts_of(txt):
    m = re.search(r"тосты \(\d+\): (.*)", txt)
    return json.loads(m.group(1)) if m else []


def startlog_of(txt):
    m = re.search(r"журнал запуска \((\d+)\): (.*)", txt)
    return json.loads(m.group(2)) if m else []


ok = True


def need(cond, msg):
    global ok
    print(("PASS  " if cond else "FAIL  ") + msg)
    ok = ok and cond


def healthy(txt):
    return ("ПУСТОЙ ОТЧЁТ" not in txt and "SCRIPT ERROR" not in txt and "TIMER CB ERROR" not in txt
            and "RL READ ERROR" not in txt and field(txt, "исключение") == "нет")


for sc in SCEN:
    need(healthy(results[sc]), sc + ": отчёт есть, исключений нет")

a = results["alive-ok"]
need(field(a, "/start отправлен") == "true", "робот жив -> /start уходит напрямую")
need(any("MXU6 start" in t for t in toasts_of(a)), "робот жив -> тост с результатом")
need("11500" in a and "12000" in a, "робот жив -> тост результата живёт 12с (а не 5с)")
need(field(a, "ERROR в журнале") == "нет", "робот жив -> без ошибок")
need("/api/of/process" not in a, "робот жив -> C# не дёргаем")

s = results["session-401"]
need("/api/of/process/of_mx/start" in s, "сессия истекла -> робот мёртв, идём в C#")
need(field(s, "/start отправлен") == "false", "сессия истекла -> /start в пустоту не шлём")
need(any("Сессия истекла" in t for t in toasts_of(s)), "сессия истекла -> тост с причиной")
need("ERROR" in (field(s, "ERROR в журнале") or ""), "сессия истекла -> запись в журнал")

d = results["robot-dead"]
need("/api/of/process/of_mx/start" in d, "робот мёртв -> поднимаем через C#")
need(field(d, "/start отправлен") == "true", "робот мёртв -> после подъёма /start уходит")
_derr = [x for x in (field(d, "ERROR в журнале") or "").split(" || ") if x.startswith("ERROR")]
need(len(_derr) == 1 and "health недоступен" in _derr[0],
     "робот мёртв (сессия ок) -> единственная ERROR-запись про health")
need(not any("ОШИБКА" in x for x in startlog_of(d)), "робот мёртв (сессия ок) -> сбоев запуска нет")

o = results["old-browser"]
need(field(o, "/start отправлен") == "true", "старый браузер (нет AbortSignal.timeout) -> кнопка работает")
need(field(o, "ERROR в журнале") == "нет", "старый браузер -> без ложных ошибок")

# Журнал запуска: каждая запись — "ЧЧ:ММ:СС MXU6 ▶: шаг". Первый шаг всегда «нажато».
for sc in SCEN:
    rl = startlog_of(results[sc])
    need(len(rl) > 0, sc + ": журнал запуска не пуст")
    need(any("нажато" in x for x in rl), sc + ": журнал содержит шаг «нажато»")
    need(all("MXU6 ▶" in x for x in rl), sc + ": все записи журнала подписаны MXU6 ▶")
    need(all(re.match(r"\d\d:\d\d:\d\d ", x) for x in rl), sc + ": у каждой записи время")

need(any("шлю /start напрямую" in x for x in startlog_of(results["alive-ok"])), "робот жив: журнал «шлю /start напрямую»")
need(any("/start HTTP 200" in x for x in startlog_of(results["alive-ok"])), "робот жив: журнал «/start HTTP 200»")
need(any("поднимаю через C#" in x for x in startlog_of(results["session-401"])), "сессия истекла: журнал «поднимаю через C#»")
need(not any("/start HTTP" in x for x in startlog_of(results["session-401"])),
     "сессия истекла: /start в журнал не попадал (не отправлялся)")
need(any("поднимаю через C#" in x for x in startlog_of(results["robot-dead"])), "робот мёртв: журнал «поднимаю через C#»")
need(any("/start HTTP 200" in x for x in startlog_of(results["robot-dead"])), "робот мёртв: после подъёма «/start HTTP 200»")
need(any("шлю /start напрямую" in x for x in startlog_of(results["old-browser"])), "старый браузер: журнал пишется")

print("")
print("ИТОГ:", "ВСЕ СЦЕНАРИИ PASS" if ok else "ЕСТЬ ПРОВАЛЫ")
sys.exit(0 if ok else 1)

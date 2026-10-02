"""F-059: single-instance lock for robots (cross-platform, stdlib only).

Инцидент 02.10.2026: три копии main_of_mx.py одновременно слушали :5081
(Windows допускает повторный bind при SO_REUSEADDR; серверный UI-стоп гасил
одну копию, зомби продолжали торговать после нажатия Стоп).

Usage (первые строки блока if __name__ == "__main__":):
    if not acquire_instance_lock("of_mx", log=log):
        log.error("... already running ...")
        sys.exit(2)

Замок держится открытым файловым дескриптором всё время жизни процесса и
освобождается автоматически при смерти процесса (в т.ч. taskkill /F).
"""
import os
import sys

_FH = None  # дескриптор lock-файла (держим до смерти процесса)


def acquire_instance_lock(robot_key: str, port=None, log=None) -> bool:
    """Взять эксклюзивный замок на (robot_key[, port]).

    False => копия этого робота уже запущена, вызывающий обязан немедленно
    завершиться (sys.exit(2)) ДО любых подписок/ордеров.
    """
    global _FH
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        key = f".lock.{robot_key}" + (f".p{port}" if port else "")
        path = os.path.join(here, key)
        fd = os.open(path, os.O_CREAT | os.O_RDWR)
        fh = os.fdopen(fd, "r+")
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        _FH = fh  # держим ссылку: замок жив, пока жив процесс
        if log:
            log.info(f"F-059 single-instance lock acquired: {key} pid={os.getpid()}")
        return True
    except (BlockingIOError, PermissionError, OSError):
        if log:
            log.error(f"F-059 single-instance: '{robot_key}' is already running (lock held) - exiting")
        return False


def release_instance_lock():
    """Освободить замок (для тестов/шатдауна; обычно не нужен - снятие при смерти процесса)."""
    global _FH
    fh = _FH
    if fh is not None:
        try:
            fh.close()
        except Exception:
            pass
        _FH = None

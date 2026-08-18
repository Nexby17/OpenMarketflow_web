"""Воспроизводимые патчи SDK finam-trade-api 4.3.3 (изолированная копия в py4/).

Запуск при пересоздании py4: python3 patch_sdk.py
Причины патчей задокументированы в papercuts.md (PC-004).
"""
import io
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PY4 = os.path.join(HERE, "py4")


def patch_file(rel: str, old: str, new: str, label: str):
    p = os.path.join(PY4, rel)
    s = io.open(p, encoding="utf-8").read()
    if new in s:
        print(f"[skip] {label} (уже применён)")
        return
    if old not in s:
        raise RuntimeError(f"[FAIL] {label}: якорь не найден в {rel}")
    s = s.replace(old, new)
    io.open(p, "w", encoding="utf-8", newline="").write(s)
    print(f"[ok] {label}")


# 1) Finam не отдаёт average_price/daily_pnl для нулевых позиций → Optional
patch_file(
    r"finam_trade_api\account\model.py",
    """class Position(BaseModel):
    symbol: str
    quantity: FinamDecimal
    average_price: FinamDecimal
    current_price: FinamDecimal
    maintenance_margin: FinamDecimal | None = None
    daily_pnl: FinamDecimal
    unrealized_pnl: FinamDecimal""",
    """class Position(BaseModel):
    symbol: str
    quantity: FinamDecimal
    average_price: FinamDecimal | None = None   # PATCH lab: Finam не отдаёт для нулевых позиций
    current_price: FinamDecimal
    maintenance_margin: FinamDecimal | None = None
    daily_pnl: FinamDecimal | None = None       # PATCH lab
    unrealized_pnl: FinamDecimal""",
    "Position.optional_fields",
)

# 2) TokenClient не должен слать устаревший JWT на /sessions (второй refresh падает code=3)
patch_file(
    r"finam_trade_api\access\access_token.py",
    "        uri = f\"{self._base_url}{url}\"\n\n        async with httpx.AsyncClient(headers=self._auth_headers, http2=True) as client:",
    "        uri = f\"{self._base_url}{url}\"\n\n        # PATCH lab: /sessions и /sessions/details не должны слать устаревший JWT в Authorization\n        _hdrs = None if url in (\"/sessions\", \"/sessions/details\") else self._auth_headers\n        async with httpx.AsyncClient(headers=_hdrs, http2=True) as client:",
    "TokenClient.no_auth_on_sessions",
)

print("patch_sdk: done")

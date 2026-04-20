--[[
  OpenMarketflow — QUIK Bridge v2 (HTTP)
  
  Обмен через HTTP с сервером OpenMarketflow.
  QUIK Lua скрипт работает как HTTP клиент:
  - Раз в секунду шлёт котировки, стакан, сделки, позиции
  - Получает команды (ордера) от сервера
  
  Запуск: В QUIK → Сервисы → Lua скрипты → Добавить → quik_bridge.lua → Запустить
  
  НАСТРОЙКИ: изменить SERVER_URL на адрес вашего сервера
]]

-- ============ НАСТРОЙКИ ============
local SERVER_URL = "http://216.57.106.32:5050"  -- Адрес OpenMarketflow сервера
local API_KEY    = ""                            -- Опционально: ключ API
local POLL_SEC   = 1      -- интервал опроса команд (сек)
local PUSH_SEC   = 1      -- интервал отправки данных (сек)
local HEARTBEAT  = 3      -- интервал heartbeat (сек)

-- Подписанные инструменты
local SUBSCRIBED = {
    -- Фьючерсы SI (формат: {class, sec_code})
    {class = "SPBFUT", sec = "SiM6"},
    -- Добавьте нужные инструменты:
    -- {class = "SPBFUT", sec = "BRK6"},
    -- {class = "SPBFUT", sec = "MXM6"},
    -- {class = "TQBR", sec = "SBER"},
    -- {class = "TQBR", sec = "GAZP"},
}

-- ============ СОСТОЯНИЕ ============
local is_running = true
local last_push = 0
local last_poll = 0
local last_hb = 0

-- ============ HTTP КЛИЕНТ ============
-- QUIK Lua не имеет встроенного HTTP, используем os.execute + curl
-- или LuaSocket если доступен

local function http_post(path, data)
    local json = encode_json(data)
    local cmd = string.format(
        'curl -s -m 5 -X POST -H "Content-Type: application/json" -d \'%s\' "%s%s" 2>nul',
        json, SERVER_URL, path
    )
    local f = io.popen(cmd, "r")
    if not f then return nil end
    local result = f:read("*a")
    f:close()
    return result
end

local function http_get(path)
    local cmd = string.format('curl -s -m 5 "%s%s" 2>nul', SERVER_URL, path)
    local f = io.popen(cmd, "r")
    if not f then return nil end
    local result = f:read("*a")
    f:close()
    return result
end

-- ============ JSON ============

function as_array(t) return setmetatable(t or {}, {__jsontype = "array"}) end

function encode_json(val)
    local t = type(val)
    if t == "nil" then return "null"
    elseif t == "boolean" then return val and "true" or "false"
    elseif t == "number" then
        if val ~= val or val == math.huge or val == -math.huge then return "null" end
        return string.format("%.8g", val)
    elseif t == "string" then
        return '"' .. val:gsub('\\','\\\\'):gsub('"','\\"'):gsub('\n','\\n'):gsub('\r','\\r') .. '"'
    elseif t == "table" then
        local mt = getmetatable(val)
        local force_array = mt and mt.__jsontype == "array"
        if force_array or #val > 0 then
            local p = {}
            for i = 1, #val do p[i] = encode_json(val[i]) end
            return "[" .. table.concat(p, ",") .. "]"
        else
            local p = {}
            for k, v in pairs(val) do
                if type(k) == "string" then
                    table.insert(p, encode_json(k) .. ":" .. encode_json(v))
                end
            end
            return "{" .. table.concat(p, ",") .. "}"
        end
    end
    return "null"
end

function decode_json(str)
    if not str or #str == 0 then return nil end
    local pos = 1
    local function skip_ws() pos = str:find("[^ \t\n\r]", pos) or pos end
    local function parse_string()
        pos = pos + 1; local result = ""
        while pos <= #str do
            local c = str:sub(pos, pos)
            if c == '"' then pos = pos + 1; return result
            elseif c == '\\' then pos = pos + 1; local e = str:sub(pos, pos)
                if e == 'n' then result = result .. '\n' elseif e == 't' then result = result .. '\t'
                else result = result .. e end
            else result = result .. c end
            pos = pos + 1
        end
        return result
    end
    local parse_value
    local function parse_object()
        pos = pos + 1; local obj = {}; skip_ws()
        if str:sub(pos, pos) == "}" then pos = pos + 1; return obj end
        while pos <= #str do
            skip_ws(); local key = parse_string(); skip_ws()
            pos = pos + 1; skip_ws(); obj[key] = parse_value(); skip_ws()
            if str:sub(pos, pos) == "}" then pos = pos + 1; return obj end
            pos = pos + 1
        end
        return obj
    end
    local function parse_array()
        pos = pos + 1; local arr = {}; skip_ws()
        if str:sub(pos, pos) == "]" then pos = pos + 1; return arr end
        while pos <= #str do
            skip_ws(); table.insert(arr, parse_value()); skip_ws()
            if str:sub(pos, pos) == "]" then pos = pos + 1; return arr end
            pos = pos + 1
        end
        return arr
    end
    parse_value = function()
        skip_ws(); local c = str:sub(pos, pos)
        if c == '"' then return parse_string()
        elseif c == '{' then return parse_object()
        elseif c == '[' then return parse_array()
        elseif c == 't' then pos = pos + 4; return true
        elseif c == 'f' then pos = pos + 5; return false
        elseif c == 'n' then pos = pos + 4; return nil
        else
            local n = str:match("^%-?%d+%.?%d*[eE]?[+-]?%d*", pos)
            if n then pos = pos + #n; return tonumber(n) end
        end
        return nil
    end
    return parse_value()
end

-- ============ СБОР ДАННЫХ ============

function get_param(class_code, sec_code, param)
    local v = getParamEx(class_code, sec_code, param)
    return tonumber(v and v.param_value) or 0
end

function collect_quotes()
    local quotes = as_array({})
    for _, s in ipairs(SUBSCRIBED) do
        table.insert(quotes, {
            ticker = s.sec,
            class = s.class,
            last = get_param(s.class, s.sec, "LAST"),
            bid = get_param(s.class, s.sec, "BID"),
            ask = get_param(s.class, s.sec, "OFFER"),
            high = get_param(s.class, s.sec, "HIGH"),
            low = get_param(s.class, s.sec, "LOW"),
            open = get_param(s.class, s.sec, "OPEN"),
            volume = get_param(s.class, s.sec, "VOLTODAY"),
            oi = get_param(s.class, s.sec, "NUMCONTRACTS"),
            change = get_param(s.class, s.sec, "CHANGE"),
            change_pct = get_param(s.class, s.sec, "CHANGEPRCNT")
        })
    end
    return quotes
end

function collect_orderbook(sec_code, class_code)
    local book = getQuoteLevel2(class_code or "SPBFUT", sec_code)
    if not book then return nil end
    local bids, asks = as_array({}), as_array({})
    if book.bid then for i = 1, #book.bid do
        table.insert(bids, {price = tonumber(book.bid[i].price) or 0, qty = tonumber(book.bid[i].quantity) or 0})
    end end
    if book.offer then for i = 1, #book.offer do
        table.insert(asks, {price = tonumber(book.offer[i].price) or 0, qty = tonumber(book.offer[i].quantity) or 0})
    end end
    return {ticker = sec_code, bids = bids, asks = asks}
end

function collect_positions()
    local positions = as_array({})
    for i = 0, getNumberOf("futures_client_holding") - 1 do
        local row = getItem("futures_client_holding", i)
        if row and row.totalnet ~= 0 then
            table.insert(positions, {
                ticker = row.sec_code or "",
                lots = row.totalnet or 0,
                avg_price = row.avrposnprice or 0,
                pnl = row.openbuys ~= 0 and row.varmargin or -row.varmargin or 0
            })
        end
    end
    return positions
end

function collect_orders()
    local orders = as_array({})
    for i = 0, getNumberOf("orders") - 1 do
        local row = getItem("orders", i)
        if row and row.flags and bit.band(row.flags, 0x1) == 1 then
            table.insert(orders, {
                order_id = tostring(row.order_num or ""),
                ticker = row.sec_code or "",
                direction = bit.band(row.flags, 0x4) == 4 and "Sell" or "Buy",
                price = row.price or 0,
                quantity = row.qty or 0,
                filled = (row.qty or 0) - (row.balance or row.qty or 0)
            })
        end
    end
    return orders
end

function collect_balance()
    local acc = getInfoParam("TRADERACCOUNT") or ""
    local money = getMoney("SPBFUT", acc, "SUR", "LIMIT_KIND")
    return {
        account = acc,
        balance = money and money.currentbal or 0,
        available = money and money.freespare or 0,
        equity = money and money.equity or 0
    }
end

function collect_trades()
    local trades = as_array({})
    local total = getNumberOf("trades")
    for i = math.max(0, total - 50), total - 1 do
        local row = getItem("trades", i)
        if row then
            table.insert(trades, {
                trade_id = tostring(row.trade_num or ""),
                ticker = row.sec_code or "",
                direction = bit.band(row.flags or 0, 0x4) == 4 and "Sell" or "Buy",
                price = row.price or 0,
                quantity = row.qty or 0,
                time = string.format("%04d-%02d-%02dT%02d:%02d:%02d",
                    row.datetime.year or 0, row.datetime.month or 0, row.datetime.day or 0,
                    row.datetime.hour or 0, row.datetime.min or 0, row.datetime.sec or 0)
            })
        end
    end
    return trades
end

-- ============ ОТПРАВКА ДАННЫХ ============

function push_data()
    local data = {
        quotes = collect_quotes(),
        positions = collect_positions(),
        orders = collect_orders(),
        balance = collect_balance(),
        trades = collect_trades(),
        timestamp = os.time()
    }
    
    -- Добавляем стакан для первого подписанного инструмента
    if #SUBSCRIBED > 0 then
        data.orderbook = collect_orderbook(SUBSCRIBED[1].sec, SUBSCRIBED[1].class)
    end
    
    local result = http_post("/quik/data", data)
    return result ~= nil
end

-- ============ ПОЛУЧЕНИЕ КОМАНД ============

function poll_commands()
    local result = http_get("/quik/commands")
    if not result or #result == 0 then return end
    
    local cmds = decode_json(result)
    if not cmds then return end
    
    local arr = cmds.commands or cmds
    if type(arr) ~= "table" then return end
    
    for _, cmd in ipairs(arr) do
        if cmd.command == "place_order" then
            local tid = tostring(os.time() % 100000)
            local result = sendTransaction({
                CLASSCODE = cmd.class_code or "SPBFUT",
                SECCODE = cmd.sec_code,
                ACTION = "NEW_ORDER",
                OPERATION = cmd.action == "BUY" and "B" or "S",
                PRICE = tostring(cmd.price or 0),
                QUANTITY = tostring(cmd.quantity or 1),
                ACCOUNT = getInfoParam("TRADERACCOUNT") or "",
                CLIENT_CODE = getInfoParam("CLIENTCODE") or "",
                COMMENT = cmd.comment or "OpenMarketflow",
                TRANS_ID = tid,
                TYPE = cmd.order_type == "MARKET" and "M" or "L"
            })
            http_post("/quik/command-result", {request_id = cmd.request_id, success = result == "", order_id = tid, error = result})
        elseif cmd.command == "cancel_order" then
            local result = sendTransaction({
                CLASSCODE = cmd.class_code or "SPBFUT",
                SECCODE = cmd.sec_code or "",
                ACTION = "KILL_ORDER",
                ORDER_KEY = tostring(cmd.order_id),
                TRANS_ID = tostring(os.time() % 100000)
            })
            http_post("/quik/command-result", {request_id = cmd.request_id, success = result == "", error = result})
        end
    end
end

-- ============ CALLBACKS ============

function OnTrade(trade)
    -- Отправляем сделку немедленно
    http_post("/quik/trade", {
        ticker = trade.sec_code or "",
        direction = bit.band(trade.flags or 0, 0x4) == 4 and "Sell" or "Buy",
        price = trade.price or 0,
        quantity = trade.qty or 0,
        trade_id = tostring(trade.trade_num or ""),
        order_id = tostring(trade.order_num or "")
    })
end

function OnOrder(order)
    local flags = order.flags or 0
    http_post("/quik/order-update", {
        order_id = tostring(order.order_num or ""),
        ticker = order.sec_code or "",
        direction = bit.band(flags, 0x4) == 4 and "Sell" or "Buy",
        price = order.price or 0,
        quantity = order.qty or 0,
        filled = (order.qty or 0) - (order.balance or 0),
        status = bit.band(flags, 0x1) == 0 and "cancelled" or (bit.band(flags, 0x2) == 2 and "filled" or "active")
    })
end

function OnFuturesClientHolding(hold)
    http_post("/quik/position-update", {
        ticker = hold.sec_code or "",
        lots = hold.totalnet or 0,
        avg_price = hold.avrposnprice or 0
    })
end

-- ============ ОСНОВНОЙ ЦИКЛ ============

function main()
    message("[OpenMarketflow] QUIK Bridge v2 (HTTP) запущен")
    message("[OpenMarketflow] Сервер: " .. SERVER_URL)
    
    -- Проверка curl
    local f = io.popen("curl --version 2>&1", "r")
    if not f then
        message("[OpenMarketflow] ❌ curl не найден! Скачайте curl для Windows")
        return
    end
    local ver = f:read("*a")
    f:close()
    if ver and #ver > 0 then
        message("[OpenMarketflow] curl OK")
    else
        message("[OpenMarketflow] ❌ curl не работает")
        return
    end
    
    -- Проверка связи с сервером
    local f2 = io.popen('curl -s -m 5 "' .. SERVER_URL .. '/health"', 'r')
    if f2 then
        local r = f2:read("*a")
        f2:close()
        if r and #r > 0 then
            message("[OpenMarketflow] ✅ Сервер доступен")
        else
            message("[OpenMarketflow] ⚠️ Сервер не ответил")
        end
    else
        message("[OpenMarketflow] ❌ Не удалось подключиться к серверу")
        return
    end
    
    -- Регистрация
    local reg = http_post("/quik/register", {
        version = "2.0",
        hostname = os.getenv("COMPUTERNAME") or "QUIK",
        instruments = SUBSCRIBED
    })
    if reg then
        message("[OpenMarketflow] ✅ Зарегистрирован на сервере")
    else
        message("[OpenMarketflow] ⚠️ Сервер недоступен, повтор...")
    end
    
    while is_running do
        local now = os.time()
        
        -- Отправка данных (каждые PUSH_SEC сек)
        if now - last_push >= PUSH_SEC then
            push_data()
            last_push = now
        end
        
        -- Получение команд (каждые POLL_SEC сек)
        if now - last_poll >= POLL_SEC then
            poll_commands()
            last_poll = now
        end
        
        -- Heartbeat
        if now - last_hb >= HEARTBEAT then
            http_post("/quik/heartbeat", {timestamp = now})
            last_hb = now
        end
        
        sleep(100)
    end
    
    message("[OpenMarketflow] QUIK Bridge остановлен")
end

function OnStop()
    is_running = false
    http_post("/quik/disconnect", {reason = "script stopped"})
end

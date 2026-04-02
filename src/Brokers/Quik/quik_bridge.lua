--[[
  OpenMarketflow — QUIK Bridge (Lua)
  БЕЗ ВНЕШНИХ ЗАВИСИМОСТЕЙ.
  
  Обмен через 2 файла (JSONL — по строке на сообщение):
  - bridge/to_app.jsonl    — QUIK пишет, приложение читает
  - bridge/to_quik.jsonl   — приложение пишет, QUIK читает
  - bridge/heartbeat       — QUIK обновляет (timestamp)
  
  Запуск: В QUIK → Сервисы → Lua скрипты → Добавить → quik_bridge.lua → Запустить
]]

-- ============ НАСТРОЙКИ ============
local BRIDGE_DIR = getScriptPath() .. "\\bridge"
local TO_APP     = BRIDGE_DIR .. "\\to_app.jsonl"
local TO_QUIK    = BRIDGE_DIR .. "\\to_quik.jsonl"
local HEARTBEAT  = BRIDGE_DIR .. "\\heartbeat"
local POLL_MS    = 200  -- чтение inbox каждые 200мс (не 50!)

-- ============ СОСТОЯНИЕ ============
local is_running = true
local out_buffer = {}  -- буфер исходящих сообщений
local subscribed_orderbooks = {}
local subscribed_quotes = {}
local subscribed_candles = {}
local last_flush = 0
local last_heartbeat = 0
local last_inbox_check = 0

-- ============ ФАЙЛОВЫЙ I/O ============

function ensure_dirs()
    os.execute('mkdir "' .. BRIDGE_DIR .. '" 2>nul')
end

-- Добавить сообщение в буфер (будет записано пачкой)
function queue_msg(msg_type, data)
    data._type = msg_type
    table.insert(out_buffer, encode_json(data))
end

-- Сбросить буфер в файл (append)
function flush_outbox()
    if #out_buffer == 0 then return end
    
    local f = io.open(TO_APP, "a")
    if not f then return end
    
    for _, line in ipairs(out_buffer) do
        f:write(line .. "\n")
    end
    f:close()
    out_buffer = {}
end

-- Прочитать команды из inbox (весь файл → обработать → очистить)
function read_inbox()
    local f = io.open(TO_QUIK, "r")
    if not f then return end
    
    local lines = {}
    for line in f:lines() do
        if #line > 0 then
            table.insert(lines, line)
        end
    end
    f:close()
    
    -- Очищаем файл
    if #lines > 0 then
        local f2 = io.open(TO_QUIK, "w")
        if f2 then f2:close() end
        
        for _, line in ipairs(lines) do
            local ok, cmd = pcall(decode_json, line)
            if ok and cmd then
                process_command(cmd)
            end
        end
    end
end

function write_heartbeat()
    local f = io.open(HEARTBEAT, "w")
    if f then
        f:write(tostring(os.time()))
        f:close()
    end
end

-- ============ JSON ENCODER/DECODER ============

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
        if #val > 0 then
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
    local pos = 1
    local function skip_ws() pos = str:find("[^ \t\n\r]", pos) or pos end
    
    local function parse_string()
        pos = pos + 1
        local result = ""
        while pos <= #str do
            local c = str:sub(pos, pos)
            if c == '"' then pos = pos + 1; return result
            elseif c == '\\' then
                pos = pos + 1; local e = str:sub(pos, pos)
                if e == 'n' then result = result .. '\n'
                elseif e == 't' then result = result .. '\t'
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

-- ============ ОБРАБОТКА КОМАНД ============

function process_command(cmd)
    local command = cmd.command
    local params = cmd.params or cmd
    local rid = cmd.request_id or 0
    
    if command == "place_order" then cmd_place_order(rid, params)
    elseif command == "cancel_order" then cmd_cancel_order(rid, params)
    elseif command == "subscribe_orderbook" then
        subscribed_orderbooks[params.sec_code] = {class_code = params.class_code or "SPBFUT", depth = params.depth or 20}
        Subscribe_Level_II_Quotes(params.class_code or "SPBFUT", params.sec_code)
        queue_msg("response", {request_id = rid, success = true})
    elseif command == "subscribe_quotes" then
        subscribed_quotes[params.sec_code] = params.class_code or "SPBFUT"
        queue_msg("response", {request_id = rid, success = true})
    elseif command == "subscribe_candles" then
        local ds = CreateDataSource(params.class_code or "SPBFUT", params.sec_code, params.interval or 5)
        if ds then
            subscribed_candles[params.sec_code] = {class_code = params.class_code or "SPBFUT", interval = params.interval or 5, ds = ds}
            ds:SetUpdateCallback(function(idx) send_candle(params.sec_code, ds, idx) end)
        end
        queue_msg("response", {request_id = rid, success = ds ~= nil})
    elseif command == "get_balance" then cmd_get_balance(rid)
    elseif command == "get_positions" then cmd_get_positions(rid)
    elseif command == "get_orders" then cmd_get_orders(rid)
    elseif command == "get_candles" then cmd_get_candles(rid, params)
    end
end

-- ============ ТОРГОВЛЯ ============

function cmd_place_order(rid, p)
    local tid = tostring(os.time() % 100000)
    local result = sendTransaction({
        CLASSCODE = p.class_code or "SPBFUT", SECCODE = p.sec_code,
        ACTION = "NEW_ORDER", OPERATION = p.action == "BUY" and "B" or "S",
        PRICE = tostring(p.price or 0), QUANTITY = tostring(p.quantity or 1),
        ACCOUNT = getInfoParam("TRADERACCOUNT") or "",
        CLIENT_CODE = getInfoParam("CLIENTCODE") or "",
        COMMENT = p.comment or "OpenMarketflow", TRANS_ID = tid,
        TYPE = p.order_type == "MARKET" and "M" or "L"
    })
    queue_msg("response", {request_id = rid, success = (result == ""), order_id = tid, error = result})
end

function cmd_cancel_order(rid, p)
    local result = sendTransaction({
        CLASSCODE = p.class_code or "SPBFUT", SECCODE = p.sec_code or "",
        ACTION = "KILL_ORDER", ORDER_KEY = tostring(p.order_id),
        TRANS_ID = tostring(os.time() % 100000)
    })
    queue_msg("response", {request_id = rid, success = (result == ""), error = result})
end

function cmd_get_balance(rid)
    local acc = getInfoParam("TRADERACCOUNT") or ""
    local money = getMoney("SPBFUT", acc, "SUR", "LIMIT_KIND")
    queue_msg("response", {request_id = rid, value = money and money.currentbal or 0})
end

function cmd_get_positions(rid)
    local positions = {}
    for i = 0, getNumberOf("futures_client_holding") - 1 do
        local row = getItem("futures_client_holding", i)
        if row and row.totalnet ~= 0 then
            table.insert(positions, {sec_code = row.sec_code or "", current_net = row.totalnet or 0, avg_price = row.avrposnprice or 0})
        end
    end
    queue_msg("response", {request_id = rid, positions = positions})
end

function cmd_get_orders(rid)
    local orders = {}
    for i = 0, getNumberOf("orders") - 1 do
        local row = getItem("orders", i)
        if row and row.flags and bit.band(row.flags, 0x1) == 1 then
            table.insert(orders, {
                order_id = tostring(row.order_num or ""), sec_code = row.sec_code or "",
                direction = bit.band(row.flags, 0x4) == 4 and "SELL" or "BUY",
                price = row.price or 0, quantity = row.qty or 0,
                filled = (row.qty or 0) - (row.balance or row.qty or 0), status = "active"
            })
        end
    end
    queue_msg("response", {request_id = rid, orders = orders})
end

function cmd_get_candles(rid, p)
    local ds = CreateDataSource(p.class_code or "SPBFUT", p.sec_code, p.interval or 5)
    if not ds then queue_msg("response", {request_id = rid, candles = {}}); return end
    sleep(500)
    local candles = {}
    local size = ds:Size()
    for i = math.max(1, size - (p.count or 200) + 1), size do
        local t = ds:T(i)
        table.insert(candles, {
            datetime = string.format("%04d-%02d-%02dT%02d:%02d:00", t.year, t.month, t.day, t.hour, t.min),
            open = ds:O(i), high = ds:H(i), low = ds:L(i), close = ds:C(i), volume = ds:V(i), sec_code = p.sec_code
        })
    end
    ds:Close()
    queue_msg("response", {request_id = rid, candles = candles})
end

-- ============ CALLBACKS QUIK ============

function OnQuote(class_code, sec_code)
    if subscribed_orderbooks[sec_code] then
        local book = getQuoteLevel2(class_code, sec_code)
        if book then
            local bids, asks = {}, {}
            if book.bid then for i = 1, #book.bid do table.insert(bids, {price = tonumber(book.bid[i].price) or 0, quantity = tonumber(book.bid[i].quantity) or 0}) end end
            if book.offer then for i = 1, #book.offer do table.insert(asks, {price = tonumber(book.offer[i].price) or 0, quantity = tonumber(book.offer[i].quantity) or 0}) end end
            queue_msg("orderbook", {sec_code = sec_code, bids = bids, asks = asks})
        end
    end
end

function OnParam(class_code, sec_code)
    if not subscribed_quotes[sec_code] then return end
    local function gp(p) local v = getParamEx(class_code, sec_code, p); return tonumber(v and v.param_value) or 0 end
    queue_msg("quote", {
        sec_code = sec_code, last = gp("LAST"), bid = gp("BID"), ask = gp("OFFER"),
        high = gp("HIGH"), low = gp("LOW"), open = gp("OPEN"),
        prev_close = gp("PREVLEGALCLOSEPR"), volume = gp("VOLTODAY"),
        open_interest = gp("NUMCONTRACTS"), change = gp("CHANGE"), change_pct = gp("CHANGEPRCNT")
    })
end

function OnTrade(trade)
    queue_msg("trade", {
        sec_code = trade.sec_code or "", price = trade.price or 0, quantity = trade.qty or 0,
        direction = bit.band(trade.flags or 0, 0x4) == 4 and "SELL" or "BUY",
        trade_id = tostring(trade.trade_num or ""), order_id = tostring(trade.order_num or "")
    })
end

function OnOrder(order)
    local flags = order.flags or 0
    local status = "active"
    if bit.band(flags, 0x1) == 0 then status = "cancelled" end
    if bit.band(flags, 0x2) == 2 then status = "filled" end
    queue_msg("order", {
        order_id = tostring(order.order_num or ""), sec_code = order.sec_code or "",
        direction = bit.band(flags, 0x4) == 4 and "SELL" or "BUY",
        price = order.price or 0, quantity = order.qty or 0,
        filled = (order.qty or 0) - (order.balance or 0), status = status
    })
end

function OnFuturesClientHolding(hold)
    queue_msg("position", {sec_code = hold.sec_code or "", current_net = hold.totalnet or 0, avg_price = hold.avrposnprice or 0})
end

function send_candle(sec_code, ds, idx)
    if not ds or idx < 1 then return end
    local t = ds:T(idx)
    queue_msg("candle", {
        sec_code = sec_code,
        datetime = string.format("%04d-%02d-%02dT%02d:%02d:00", t.year, t.month, t.day, t.hour, t.min),
        open = ds:O(idx), high = ds:H(idx), low = ds:L(idx), close = ds:C(idx), volume = ds:V(idx)
    })
end

-- ============ ОСНОВНОЙ ЦИКЛ ============

function main()
    message("[OpenMarketflow] QUIK Bridge запущен")
    ensure_dirs()
    message("[OpenMarketflow] Папка: " .. BRIDGE_DIR)
    
    -- Создаём пустые файлы если нет
    local f = io.open(TO_APP, "a"); if f then f:close() end
    f = io.open(TO_QUIK, "a"); if f then f:close() end
    
    while is_running do
        local now = os.clock() * 1000  -- мс
        
        -- Читаем команды (каждые 200мс)
        if now - last_inbox_check >= POLL_MS then
            read_inbox()
            last_inbox_check = now
        end
        
        -- Сбрасываем буфер (каждые 200мс или если много сообщений)
        if #out_buffer > 0 and (now - last_flush >= POLL_MS or #out_buffer > 50) then
            flush_outbox()
            last_flush = now
        end
        
        -- Heartbeat (каждые 3с)
        if os.time() - last_heartbeat >= 3 then
            write_heartbeat()
            last_heartbeat = os.time()
        end
        
        sleep(50)  -- 50мс sleep, но I/O только каждые 200мс
    end
    
    -- Очистка
    for sec, data in pairs(subscribed_orderbooks) do
        pcall(function() Unsubscribe_Level_II_Quotes(data.class_code, sec) end)
    end
    for _, data in pairs(subscribed_candles) do
        pcall(function() if data.ds then data.ds:Close() end end)
    end
    
    message("[OpenMarketflow] QUIK Bridge остановлен")
end

function OnStop() is_running = false end

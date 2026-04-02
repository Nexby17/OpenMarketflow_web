--[[
  OpenMarketflow — QUIK Bridge (Lua)
  БЕЗ ВНЕШНИХ ЗАВИСИМОСТЕЙ — обмен через файлы.
  
  Запуск: В QUIK → Сервисы → Lua скрипты → Добавить → quik_bridge.lua → Запустить
  
  Протокол: файловый обмен через общую папку.
  - QUIK пишет данные в outbox/*.json (стакан, котировки, сделки, ордера)
  - Приложение пишет команды в inbox/*.json (place_order, cancel_order)
  - Каждый файл = одно сообщение, после чтения удаляется
  
  Настройка: указать BRIDGE_DIR — общая папка для обмена.
]]

-- ============ НАСТРОЙКИ ============

-- Папка обмена (должна совпадать с QuikConnector.BridgeDir)
-- Если QUIK и приложение на одном ПК:
local BRIDGE_DIR = getScriptPath() .. "\\bridge"

-- Интервал проверки inbox (мс)
local POLL_INTERVAL = 50

-- ============ СОСТОЯНИЕ ============

local is_running = true
local msg_counter = 0
local subscribed_orderbooks = {}  -- sec_code → {class_code, depth}
local subscribed_quotes = {}      -- sec_code → class_code
local subscribed_candles = {}     -- sec_code → {class_code, interval, ds}

-- ============ ФАЙЛОВЫЙ I/O ============

function ensure_dirs()
    os.execute('mkdir "' .. BRIDGE_DIR .. '" 2>nul')
    os.execute('mkdir "' .. BRIDGE_DIR .. '\\outbox" 2>nul')
    os.execute('mkdir "' .. BRIDGE_DIR .. '\\inbox" 2>nul')
    os.execute('mkdir "' .. BRIDGE_DIR .. '\\status" 2>nul')
end

function write_msg(msg_type, data)
    msg_counter = msg_counter + 1
    local filename = BRIDGE_DIR .. "\\outbox\\" .. os.time() .. "_" .. msg_counter .. "_" .. msg_type .. ".json"
    local tmp = filename .. ".tmp"
    
    local f = io.open(tmp, "w")
    if not f then return false end
    
    data._type = msg_type
    data._ts = os.time()
    
    -- Простой JSON encoder (QUIK не имеет json модуля)
    f:write(encode_json(data))
    f:close()
    
    -- Атомарный rename (приложение не прочитает неполный файл)
    os.rename(tmp, filename)
    return true
end

function read_inbox()
    -- Читаем все файлы из inbox/
    local pipe = io.popen('dir "' .. BRIDGE_DIR .. '\\inbox\\*.json" /b /o:n 2>nul')
    if not pipe then return end
    
    for filename in pipe:lines() do
        local filepath = BRIDGE_DIR .. "\\inbox\\" .. filename
        local f = io.open(filepath, "r")
        if f then
            local content = f:read("*all")
            f:close()
            os.remove(filepath)
            
            if content and #content > 0 then
                local ok, cmd = pcall(decode_json, content)
                if ok and cmd then
                    process_command(cmd)
                end
            end
        end
    end
    pipe:close()
end

function write_status()
    -- Пишем heartbeat/статус (приложение проверяет что QUIK жив)
    local f = io.open(BRIDGE_DIR .. "\\status\\heartbeat.json.tmp", "w")
    if f then
        f:write(encode_json({
            alive = true,
            time = os.time(),
            subscriptions = {
                orderbooks = count_keys(subscribed_orderbooks),
                quotes = count_keys(subscribed_quotes),
                candles = count_keys(subscribed_candles)
            }
        }))
        f:close()
        os.rename(BRIDGE_DIR .. "\\status\\heartbeat.json.tmp", BRIDGE_DIR .. "\\status\\heartbeat.json")
    end
end

-- ============ JSON ENCODER/DECODER (встроенный) ============

function encode_json(val)
    local t = type(val)
    if t == "nil" then return "null"
    elseif t == "boolean" then return val and "true" or "false"
    elseif t == "number" then
        if val ~= val then return "null" end  -- NaN
        if val == math.huge or val == -math.huge then return "null" end
        return string.format("%.8g", val)
    elseif t == "string" then
        return '"' .. val:gsub('\\', '\\\\'):gsub('"', '\\"'):gsub('\n', '\\n'):gsub('\r', '\\r'):gsub('\t', '\\t') .. '"'
    elseif t == "table" then
        -- Array check
        local is_array = (#val > 0)
        if is_array then
            local parts = {}
            for i = 1, #val do
                parts[i] = encode_json(val[i])
            end
            return "[" .. table.concat(parts, ",") .. "]"
        else
            local parts = {}
            for k, v in pairs(val) do
                if type(k) == "string" then
                    table.insert(parts, encode_json(k) .. ":" .. encode_json(v))
                end
            end
            return "{" .. table.concat(parts, ",") .. "}"
        end
    end
    return "null"
end

function decode_json(str)
    -- Минимальный JSON декодер для команд
    -- Поддерживает: {}, [], strings, numbers, booleans, null
    local pos = 1
    
    local function skip_ws()
        pos = str:find("[^ \t\n\r]", pos) or pos
    end
    
    local function parse_string()
        pos = pos + 1  -- skip "
        local start = pos
        local result = ""
        while pos <= #str do
            local c = str:sub(pos, pos)
            if c == '"' then
                pos = pos + 1
                return result
            elseif c == '\\' then
                pos = pos + 1
                local esc = str:sub(pos, pos)
                if esc == 'n' then result = result .. '\n'
                elseif esc == 't' then result = result .. '\t'
                elseif esc == 'r' then result = result .. '\r'
                else result = result .. esc end
            else
                result = result .. c
            end
            pos = pos + 1
        end
        return result
    end
    
    local parse_value  -- forward declare
    
    local function parse_object()
        pos = pos + 1  -- skip {
        local obj = {}
        skip_ws()
        if str:sub(pos, pos) == "}" then pos = pos + 1; return obj end
        while pos <= #str do
            skip_ws()
            local key = parse_string()
            skip_ws()
            pos = pos + 1  -- skip :
            skip_ws()
            obj[key] = parse_value()
            skip_ws()
            local c = str:sub(pos, pos)
            if c == "}" then pos = pos + 1; return obj end
            pos = pos + 1  -- skip ,
        end
        return obj
    end
    
    local function parse_array()
        pos = pos + 1  -- skip [
        local arr = {}
        skip_ws()
        if str:sub(pos, pos) == "]" then pos = pos + 1; return arr end
        while pos <= #str do
            skip_ws()
            table.insert(arr, parse_value())
            skip_ws()
            local c = str:sub(pos, pos)
            if c == "]" then pos = pos + 1; return arr end
            pos = pos + 1  -- skip ,
        end
        return arr
    end
    
    parse_value = function()
        skip_ws()
        local c = str:sub(pos, pos)
        if c == '"' then return parse_string()
        elseif c == '{' then return parse_object()
        elseif c == '[' then return parse_array()
        elseif c == 't' then pos = pos + 4; return true
        elseif c == 'f' then pos = pos + 5; return false
        elseif c == 'n' then pos = pos + 4; return nil
        else
            -- number
            local num_str = str:match("^%-?%d+%.?%d*[eE]?[+-]?%d*", pos)
            if num_str then
                pos = pos + #num_str
                return tonumber(num_str)
            end
        end
        return nil
    end
    
    return parse_value()
end

function count_keys(t)
    local n = 0
    for _ in pairs(t) do n = n + 1 end
    return n
end

-- ============ ОБРАБОТКА КОМАНД ============

function process_command(cmd)
    local command = cmd.command
    local params = cmd.params or cmd
    local request_id = cmd.request_id or 0
    
    if command == "place_order" then
        cmd_place_order(request_id, params)
    elseif command == "cancel_order" then
        cmd_cancel_order(request_id, params)
    elseif command == "subscribe_orderbook" then
        cmd_subscribe_orderbook(params)
        send_response(request_id, {success = true})
    elseif command == "subscribe_quotes" then
        cmd_subscribe_quotes(params)
        send_response(request_id, {success = true})
    elseif command == "subscribe_candles" then
        cmd_subscribe_candles(params)
        send_response(request_id, {success = true})
    elseif command == "get_balance" then
        cmd_get_balance(request_id)
    elseif command == "get_positions" then
        cmd_get_positions(request_id)
    elseif command == "get_orders" then
        cmd_get_orders(request_id)
    elseif command == "get_candles" then
        cmd_get_candles(request_id, params)
    end
end

function send_response(request_id, data)
    data.request_id = request_id
    write_msg("response", data)
end

-- ============ КОМАНДЫ ТОРГОВЛИ ============

function cmd_place_order(request_id, p)
    local trans_id = tostring(os.time() % 100000)
    local trans = {
        CLASSCODE  = p.class_code or "SPBFUT",
        SECCODE    = p.sec_code,
        ACTION     = "NEW_ORDER",
        OPERATION  = p.action == "BUY" and "B" or "S",
        PRICE      = tostring(p.price or 0),
        QUANTITY   = tostring(p.quantity or 1),
        ACCOUNT    = getInfoParam("TRADERACCOUNT") or "",
        CLIENT_CODE = getInfoParam("CLIENTCODE") or "",
        COMMENT    = p.comment or "OpenMarketflow",
        TRANS_ID   = trans_id,
        TYPE       = p.order_type == "MARKET" and "M" or "L"
    }
    
    local result = sendTransaction(trans)
    if result == "" then
        send_response(request_id, {success = true, order_id = trans_id, trans_id = trans_id})
    else
        send_response(request_id, {success = false, error = result})
    end
end

function cmd_cancel_order(request_id, p)
    local trans = {
        CLASSCODE = p.class_code or "SPBFUT",
        SECCODE   = p.sec_code or "",
        ACTION    = "KILL_ORDER",
        ORDER_KEY = tostring(p.order_id),
        TRANS_ID  = tostring(os.time() % 100000)
    }
    
    local result = sendTransaction(trans)
    send_response(request_id, {success = (result == ""), error = result})
end

-- ============ ПОДПИСКИ ============

function cmd_subscribe_orderbook(p)
    local sec = p.sec_code
    local class = p.class_code or "SPBFUT"
    subscribed_orderbooks[sec] = {class_code = class, depth = p.depth or 20}
    Subscribe_Level_II_Quotes(class, sec)
    log("Подписка на стакан: " .. sec)
end

function cmd_subscribe_quotes(p)
    local sec = p.sec_code
    subscribed_quotes[sec] = p.class_code or "SPBFUT"
    log("Подписка на котировки: " .. sec)
end

function cmd_subscribe_candles(p)
    local sec = p.sec_code
    local class = p.class_code or "SPBFUT"
    local interval = p.interval or 5
    
    local ds = CreateDataSource(class, sec, interval)
    if ds then
        subscribed_candles[sec] = {class_code = class, interval = interval, ds = ds}
        ds:SetUpdateCallback(function(idx)
            send_candle(sec, ds, idx)
        end)
        log("Подписка на свечи: " .. sec .. " TF=" .. interval)
    end
end

-- ============ ДАННЫЕ ============

function cmd_get_balance(request_id)
    local acc = getInfoParam("TRADERACCOUNT") or ""
    local money = getMoney("SPBFUT", acc, "SUR", "LIMIT_KIND")
    local balance = 0
    if money then balance = money.currentbal or 0 end
    send_response(request_id, {value = balance})
end

function cmd_get_positions(request_id)
    local positions = {}
    local num = getNumberOf("futures_client_holding")
    for i = 0, num - 1 do
        local row = getItem("futures_client_holding", i)
        if row and row.totalnet ~= 0 then
            table.insert(positions, {
                sec_code = row.sec_code or "",
                current_net = row.totalnet or 0,
                avg_price = row.avrposnprice or 0,
                var_margin = row.varmargin or 0
            })
        end
    end
    send_response(request_id, {positions = positions})
end

function cmd_get_orders(request_id)
    local orders = {}
    local num = getNumberOf("orders")
    for i = 0, num - 1 do
        local row = getItem("orders", i)
        if row and row.flags and bit.band(row.flags, 0x1) == 1 then
            table.insert(orders, {
                order_id = tostring(row.order_num or ""),
                sec_code = row.sec_code or "",
                class_code = row.class_code or "",
                direction = bit.band(row.flags, 0x4) == 4 and "SELL" or "BUY",
                price = row.price or 0,
                quantity = row.qty or 0,
                filled = (row.qty or 0) - (row.balance or row.qty or 0),
                status = "active"
            })
        end
    end
    send_response(request_id, {orders = orders})
end

function cmd_get_candles(request_id, p)
    local class = p.class_code or "SPBFUT"
    local sec = p.sec_code
    local interval = p.interval or 5
    local count = p.count or 200
    
    local ds = CreateDataSource(class, sec, interval)
    if not ds then
        send_response(request_id, {candles = {}})
        return
    end
    
    sleep(500)
    
    local candles = {}
    local size = ds:Size()
    local start = math.max(1, size - count + 1)
    
    for i = start, size do
        local t = ds:T(i)
        table.insert(candles, {
            datetime = string.format("%04d-%02d-%02dT%02d:%02d:00", t.year, t.month, t.day, t.hour, t.min),
            open = ds:O(i), high = ds:H(i), low = ds:L(i), close = ds:C(i), volume = ds:V(i),
            sec_code = sec
        })
    end
    
    ds:Close()
    send_response(request_id, {candles = candles})
end

-- ============ CALLBACKS QUIK ============

function OnQuote(class_code, sec_code)
    if not subscribed_orderbooks[sec_code] then return end
    
    local book = getQuoteLevel2(class_code, sec_code)
    if not book then return end
    
    local bids = {}
    local asks = {}
    
    if book.bid then
        for i = 1, #book.bid do
            table.insert(bids, {price = tonumber(book.bid[i].price) or 0, quantity = tonumber(book.bid[i].quantity) or 0})
        end
    end
    if book.offer then
        for i = 1, #book.offer do
            table.insert(asks, {price = tonumber(book.offer[i].price) or 0, quantity = tonumber(book.offer[i].quantity) or 0})
        end
    end
    
    write_msg("orderbook", {sec_code = sec_code, class_code = class_code, bids = bids, asks = asks})
end

function OnParam(class_code, sec_code)
    if not subscribed_quotes[sec_code] then return end
    
    local function gp(param) 
        local v = getParamEx(class_code, sec_code, param)
        return tonumber(v and v.param_value) or 0 
    end
    
    write_msg("quote", {
        sec_code = sec_code, class_code = class_code,
        last = gp("LAST"), bid = gp("BID"), ask = gp("OFFER"),
        high = gp("HIGH"), low = gp("LOW"), open = gp("OPEN"),
        prev_close = gp("PREVLEGALCLOSEPR"), volume = gp("VOLTODAY"),
        open_interest = gp("NUMCONTRACTS"), change = gp("CHANGE"), change_pct = gp("CHANGEPRCNT")
    })
end

function OnTrade(trade)
    write_msg("trade", {
        sec_code = trade.sec_code or "", class_code = trade.class_code or "",
        price = trade.price or 0, quantity = trade.qty or 0,
        direction = bit.band(trade.flags or 0, 0x4) == 4 and "SELL" or "BUY",
        trade_id = tostring(trade.trade_num or ""), order_id = tostring(trade.order_num or "")
    })
end

function OnAllTrade(alltrade)
    write_msg("allTrades", {
        sec_code = alltrade.sec_code or "", class_code = alltrade.class_code or "",
        price = alltrade.price or 0, quantity = alltrade.qty or 0,
        flags = tostring(bit.band(alltrade.flags or 0, 0x1)),
        trade_num = tostring(alltrade.trade_num or "")
    })
end

function OnOrder(order)
    local flags = order.flags or 0
    local status = "active"
    if bit.band(flags, 0x1) == 0 then status = "cancelled" end
    if bit.band(flags, 0x2) == 2 then status = "filled" end
    
    write_msg("order", {
        order_id = tostring(order.order_num or ""), sec_code = order.sec_code or "",
        class_code = order.class_code or "",
        direction = bit.band(flags, 0x4) == 4 and "SELL" or "BUY",
        price = order.price or 0, quantity = order.qty or 0,
        filled = (order.qty or 0) - (order.balance or 0), status = status
    })
end

function OnFuturesClientHolding(hold)
    write_msg("position", {
        sec_code = hold.sec_code or "", current_net = hold.totalnet or 0,
        avg_price = hold.avrposnprice or 0, var_margin = hold.varmargin or 0
    })
end

function send_candle(sec_code, ds, idx)
    if not ds or idx < 1 then return end
    local t = ds:T(idx)
    write_msg("candle", {
        sec_code = sec_code,
        datetime = string.format("%04d-%02d-%02dT%02d:%02d:00", t.year, t.month, t.day, t.hour, t.min),
        open = ds:O(idx), high = ds:H(idx), low = ds:L(idx), close = ds:C(idx), volume = ds:V(idx)
    })
end

-- ============ ОСНОВНОЙ ЦИКЛ ============

function log(msg)
    message("[OpenMarketflow] " .. msg)
end

function main()
    log("OpenMarketflow QUIK Bridge запущен")
    ensure_dirs()
    log("Папка обмена: " .. BRIDGE_DIR)
    
    local last_status = 0
    
    while is_running do
        -- Читаем команды из inbox
        read_inbox()
        
        -- Heartbeat каждые 5 сек
        local now = os.time()
        if now - last_status >= 5 then
            write_status()
            last_status = now
        end
        
        sleep(POLL_INTERVAL)
    end
    
    -- Отписка
    for sec, data in pairs(subscribed_orderbooks) do
        pcall(function() Unsubscribe_Level_II_Quotes(data.class_code, sec) end)
    end
    for sec, data in pairs(subscribed_candles) do
        pcall(function() if data.ds then data.ds:Close() end end)
    end
    
    log("OpenMarketflow QUIK Bridge остановлен")
end

function OnStop()
    is_running = false
end

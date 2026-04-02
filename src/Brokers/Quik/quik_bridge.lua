--[[
  OpenMarketflow — QUIK Bridge (Lua)
  
  Запуск: В QUIK → Сервисы → Lua скрипты → Добавить → quik_bridge.lua → Запустить
  
  Подключается к TCP-серверу нашего приложения и стримит:
  - Котировки (quote)
  - Стакан (orderbook)
  - Сделки (trade)
  - Свечи (candle)  
  - Ордера (order)
  - Позиции (position)
  - Баланс (balance)
  - Лента всех сделок (allTrades)
  
  Принимает команды:
  - place_order
  - cancel_order
  - subscribe_candles
  - subscribe_orderbook
  - subscribe_quotes
  - get_balance
  - get_positions
  - get_orders
  - get_candles
  
  Формат: type|json\n
]]

-- ============ НАСТРОЙКИ ============
local HOST = "127.0.0.1"  -- адрес Trading Server
local PORT = 34130          -- порт (должен совпадать с QuikConnector)
local RECONNECT_SEC = 5     -- пауза перед реконнектом

-- ============ СОСТОЯНИЕ ============
local socket = require("socket")
local json   = require("json")  -- QUIK включает cjson

local conn = nil
local is_running = true
local subscribed_orderbooks = {}  -- sec_code → true
local subscribed_quotes = {}      -- sec_code → true
local subscribed_candles = {}     -- sec_code → {class_code, interval, ds}

-- ============ TCP ============

function connect()
    local c, err = socket.connect(HOST, PORT)
    if c then
        c:settimeout(0.01)  -- non-blocking
        conn = c
        log("✅ Подключён к " .. HOST .. ":" .. PORT)
        return true
    else
        log("❌ Не удалось подключиться: " .. (err or "unknown"))
        return false
    end
end

function send_msg(msg_type, data)
    if conn == nil then return false end
    local ok, err = pcall(function()
        local payload = msg_type .. "|" .. json.encode(data) .. "\n"
        conn:send(payload)
    end)
    if not ok then
        log("Ошибка отправки: " .. (err or ""))
        conn = nil
        return false
    end
    return true
end

function read_commands()
    if conn == nil then return end
    local line, err = conn:receive("*l")
    if line then
        process_command(line)
    elseif err ~= "timeout" then
        log("TCP: " .. (err or "closed"))
        conn = nil
    end
end

-- ============ ОБРАБОТКА КОМАНД ============

function process_command(raw)
    local pipe = raw:find("|")
    if not pipe then return end
    
    local msg_type = raw:sub(1, pipe - 1)
    local payload = raw:sub(pipe + 1)
    
    if msg_type == "command" then
        local ok, cmd = pcall(json.decode, payload)
        if not ok then return end
        
        local request_id = cmd.request_id
        local command = cmd.command
        local params = cmd.params or {}
        
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
    elseif msg_type == "pong" then
        -- keepalive ack
    elseif msg_type == "disconnect" then
        is_running = false
    end
end

function send_response(request_id, data)
    data.request_id = request_id
    send_msg("response", data)
end

-- ============ КОМАНДЫ ТОРГОВЛИ ============

function cmd_place_order(request_id, p)
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
        TRANS_ID   = tostring(os.time() % 100000),
        TYPE       = p.order_type == "MARKET" and "M" or "L"
    }
    
    local result = sendTransaction(trans)
    if result == "" then
        send_response(request_id, {
            success = true,
            order_id = trans.TRANS_ID,
            trans_id = trans.TRANS_ID
        })
    else
        send_response(request_id, {success = false, error = result})
    end
end

function cmd_cancel_order(request_id, p)
    local trans = {
        CLASSCODE = "SPBFUT",
        SECCODE   = "",
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
    subscribed_orderbooks[sec] = {
        class_code = p.class_code or "SPBFUT",
        depth = p.depth or 20
    }
    -- Подписка через QUIK API
    Subscribe_Level_II_Quotes(p.class_code or "SPBFUT", sec)
    log("📊 Подписка на стакан: " .. sec)
end

function cmd_subscribe_quotes(p)
    local sec = p.sec_code
    subscribed_quotes[sec] = p.class_code or "SPBFUT"
    log("💹 Подписка на котировки: " .. sec)
end

function cmd_subscribe_candles(p)
    local sec = p.sec_code
    local class = p.class_code or "SPBFUT"
    local interval = p.interval or 5
    
    local ds = CreateDataSource(class, sec, interval)
    if ds then
        subscribed_candles[sec] = {
            class_code = class,
            interval = interval,
            ds = ds
        }
        
        ds:SetUpdateCallback(function(idx)
            send_candle(sec, ds, idx)
        end)
        
        log("🕯 Подписка на свечи: " .. sec .. " TF=" .. interval)
    end
end

-- ============ ДАННЫЕ ============

function cmd_get_balance(request_id)
    local acc = getInfoParam("TRADERACCOUNT")
    local money = getMoney("SPBFUT", acc, "SUR", "LIMIT_KIND")
    local balance = 0
    if money then
        balance = money.currentbal or 0
    end
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
        if row and row.flags and bit.band(row.flags, 0x1) == 1 then  -- active
            table.insert(orders, {
                order_id = tostring(row.order_num or ""),
                sec_code = row.sec_code or "",
                class_code = row.class_code or "",
                direction = bit.band(row.flags, 0x4) == 4 and "SELL" or "BUY",
                price = row.price or 0,
                quantity = row.qty or 0,
                filled = row.qty - (row.balance or row.qty),
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
    
    -- Ждём загрузки данных
    sleep(500)
    
    local candles = {}
    local size = ds:Size()
    local start = math.max(1, size - count + 1)
    
    for i = start, size do
        table.insert(candles, {
            datetime = tostring(ds:T(i).year) .. "-" .. 
                       string.format("%02d", ds:T(i).month) .. "-" ..
                       string.format("%02d", ds:T(i).day) .. "T" ..
                       string.format("%02d", ds:T(i).hour) .. ":" ..
                       string.format("%02d", ds:T(i).min) .. ":00",
            open = ds:O(i),
            high = ds:H(i),
            low = ds:L(i),
            close = ds:C(i),
            volume = ds:V(i),
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
            table.insert(bids, {
                price = tonumber(book.bid[i].price) or 0,
                quantity = tonumber(book.bid[i].quantity) or 0
            })
        end
    end
    
    if book.offer then
        for i = 1, #book.offer do
            table.insert(asks, {
                price = tonumber(book.offer[i].price) or 0,
                quantity = tonumber(book.offer[i].quantity) or 0
            })
        end
    end
    
    send_msg("orderbook", {
        sec_code = sec_code,
        class_code = class_code,
        bids = bids,
        asks = asks
    })
end

function OnParam(class_code, sec_code)
    if not subscribed_quotes[sec_code] then return end
    
    local p = getParamEx(class_code, sec_code, "LAST")
    local bid = getParamEx(class_code, sec_code, "BID")
    local ask = getParamEx(class_code, sec_code, "OFFER")
    local high = getParamEx(class_code, sec_code, "HIGH")
    local low = getParamEx(class_code, sec_code, "LOW")
    local open_p = getParamEx(class_code, sec_code, "OPEN")
    local prevclose = getParamEx(class_code, sec_code, "PREVLEGALCLOSEPR")
    local vol = getParamEx(class_code, sec_code, "VOLTODAY")
    local oi = getParamEx(class_code, sec_code, "NUMCONTRACTS")
    local change = getParamEx(class_code, sec_code, "CHANGE")
    local changepct = getParamEx(class_code, sec_code, "CHANGEPRCNT")
    
    send_msg("quote", {
        sec_code = sec_code,
        class_code = class_code,
        last = tonumber(p and p.param_value) or 0,
        bid = tonumber(bid and bid.param_value) or 0,
        ask = tonumber(ask and ask.param_value) or 0,
        high = tonumber(high and high.param_value) or 0,
        low = tonumber(low and low.param_value) or 0,
        open = tonumber(open_p and open_p.param_value) or 0,
        prev_close = tonumber(prevclose and prevclose.param_value) or 0,
        volume = tonumber(vol and vol.param_value) or 0,
        open_interest = tonumber(oi and oi.param_value) or 0,
        change = tonumber(change and change.param_value) or 0,
        change_pct = tonumber(changepct and changepct.param_value) or 0
    })
end

function OnTrade(trade)
    send_msg("trade", {
        sec_code = trade.sec_code or "",
        class_code = trade.class_code or "",
        price = trade.price or 0,
        quantity = trade.qty or 0,
        direction = bit.band(trade.flags or 0, 0x4) == 4 and "SELL" or "BUY",
        trade_id = tostring(trade.trade_num or ""),
        order_id = tostring(trade.order_num or "")
    })
end

function OnAllTrade(alltrade)
    send_msg("allTrades", {
        sec_code = alltrade.sec_code or "",
        class_code = alltrade.class_code or "",
        price = alltrade.price or 0,
        quantity = alltrade.qty or 0,
        flags = tostring(bit.band(alltrade.flags or 0, 0x1)),
        trade_num = tostring(alltrade.trade_num or "")
    })
end

function OnOrder(order)
    local status = "active"
    local flags = order.flags or 0
    if bit.band(flags, 0x1) == 0 then status = "cancelled" end
    if bit.band(flags, 0x2) == 2 then status = "filled" end
    
    send_msg("order", {
        order_id = tostring(order.order_num or ""),
        sec_code = order.sec_code or "",
        class_code = order.class_code or "",
        direction = bit.band(flags, 0x4) == 4 and "SELL" or "BUY",
        price = order.price or 0,
        quantity = order.qty or 0,
        filled = (order.qty or 0) - (order.balance or 0),
        status = status
    })
end

function OnFuturesClientHolding(hold)
    send_msg("position", {
        sec_code = hold.sec_code or "",
        current_net = hold.totalnet or 0,
        avg_price = hold.avrposnprice or 0,
        var_margin = hold.varmargin or 0
    })
end

function send_candle(sec_code, ds, idx)
    if not ds or idx < 1 then return end
    send_msg("candle", {
        sec_code = sec_code,
        datetime = tostring(ds:T(idx).year) .. "-" .. 
                   string.format("%02d", ds:T(idx).month) .. "-" ..
                   string.format("%02d", ds:T(idx).day) .. "T" ..
                   string.format("%02d", ds:T(idx).hour) .. ":" ..
                   string.format("%02d", ds:T(idx).min) .. ":00",
        open = ds:O(idx),
        high = ds:H(idx),
        low = ds:L(idx),
        close = ds:C(idx),
        volume = ds:V(idx)
    })
end

-- ============ ОСНОВНОЙ ЦИКЛ ============

function log(msg)
    message("[OpenMarketflow] " .. msg)
end

function main()
    log("🚀 OpenMarketflow QUIK Bridge запущен")
    log("Подключаюсь к " .. HOST .. ":" .. PORT .. "...")
    
    while is_running do
        if conn == nil then
            if not connect() then
                sleep(RECONNECT_SEC * 1000)
            end
        else
            -- Читаем команды
            read_commands()
            
            -- Ping каждые 10 сек
            send_msg("ping", {time = os.time()})
            
            sleep(100)  -- 100мс цикл
        end
    end
    
    -- Отписка
    for sec, _ in pairs(subscribed_orderbooks) do
        Unsubscribe_Level_II_Quotes(subscribed_orderbooks[sec].class_code, sec)
    end
    
    for sec, data in pairs(subscribed_candles) do
        if data.ds then data.ds:Close() end
    end
    
    if conn then conn:close() end
    log("⏹ OpenMarketflow QUIK Bridge остановлен")
end

function OnStop()
    is_running = false
end

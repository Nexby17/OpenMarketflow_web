#!/bin/bash
# Deploy HedgeFund server: stop → clean → publish → copy static → start
set -e

cd /root/.openclaw/workspace/HedgeFund/src/Server
WWWROOT=wwwroot
PUBLISH=bin/Release/net8.0/publish

echo "⏹️  Stopping server..."
systemctl stop hedgefund-server 2>/dev/null || true
sleep 1

echo "🧹 Cleaning old publish..."
rm -rf "$PUBLISH"

echo "📦 Publishing..."
dotnet publish -c Release -o "$PUBLISH" 2>&1 | tail -3

echo "📋 Copying static files (publish overwrites wwwroot)..."
cp -f "$WWWROOT/index.html" "$PUBLISH/wwwroot/index.html" 2>/dev/null || true
cp -f "$WWWROOT/js/app.js" "$PUBLISH/wwwroot/js/app.js" 2>/dev/null || true
cp -f "$WWWROOT/css/style.css" "$PUBLISH/wwwroot/css/style.css" 2>/dev/null || true
cp -f "$WWWROOT/js/signalr.min.js" "$PUBLISH/wwwroot/js/signalr.min.js" 2>/dev/null || true
cp -f "$WWWROOT/js/lightweight-charts.js" "$PUBLISH/wwwroot/js/lightweight-charts.js" 2>/dev/null || true

echo "▶️  Starting server..."
systemctl start hedgefund-server
sleep 2

if systemctl is-active --quiet hedgefund-server; then
    echo "✅ Server running!"
    curl -s http://localhost:5050/health | python3 -m json.tool 2>/dev/null || echo "(health check skipped)"
else
    echo "❌ Server failed to start!"
    journalctl -u hedgefund-server -n 20 --no-pager
    exit 1
fi

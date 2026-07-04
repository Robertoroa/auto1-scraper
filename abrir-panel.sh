#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
pkill -f "server.py" 2>/dev/null
echo "🚀 Arrancando panel..."
cd "$DIR"
source venv/bin/activate
python3 server.py &
sleep 1
open -a "Google Chrome" "http://localhost:8765/index.html"
echo "✅ Panel abierto en http://localhost:8765"

#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# 📡 Livestream Radar — Start All Services
# Runs uvicorn + cloudflared tunnel in background
# Usage: ./start.sh         (start all)
#        ./start.sh stop    (stop all)
#        ./start.sh status  (check status)
#        ./start.sh logs    (tail logs)
# ═══════════════════════════════════════════════════════════════

DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE_SERVER="$DIR/.server.pid"
PIDFILE_TUNNEL="$DIR/.tunnel.pid"
LOG_SERVER="$DIR/server.log"
LOG_TUNNEL="$DIR/tunnel.log"
VENV="$DIR/.venv"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

start_server() {
    if [ -f "$PIDFILE_SERVER" ] && kill -0 "$(cat "$PIDFILE_SERVER")" 2>/dev/null; then
        echo -e "${YELLOW}⚠ Server already running (PID $(cat "$PIDFILE_SERVER"))${NC}"
        return
    fi

    echo -e "${GREEN}🚀 Starting Uvicorn server...${NC}"
    cd "$DIR"
    source "$VENV/bin/activate"
    nohup "$VENV/bin/python" -m uvicorn main:app --host 0.0.0.0 --port 8000 \
        >> "$LOG_SERVER" 2>&1 &
    echo $! > "$PIDFILE_SERVER"
    echo -e "${GREEN}   ✅ Server started (PID $!) → $LOG_SERVER${NC}"
}

start_tunnel() {
    if [ -f "$PIDFILE_TUNNEL" ] && kill -0 "$(cat "$PIDFILE_TUNNEL")" 2>/dev/null; then
        echo -e "${YELLOW}⚠ Tunnel already running (PID $(cat "$PIDFILE_TUNNEL"))${NC}"
        return
    fi

    echo -e "${GREEN}🌐 Starting Cloudflare Tunnel...${NC}"
    nohup cloudflared tunnel run radar \
        >> "$LOG_TUNNEL" 2>&1 &
    echo $! > "$PIDFILE_TUNNEL"
    echo -e "${GREEN}   ✅ Tunnel started (PID $!) → $LOG_TUNNEL${NC}"
}

stop_all() {
    echo -e "${RED}🛑 Stopping services...${NC}"
    for pidfile in "$PIDFILE_SERVER" "$PIDFILE_TUNNEL"; do
        if [ -f "$pidfile" ]; then
            pid=$(cat "$pidfile")
            if kill -0 "$pid" 2>/dev/null; then
                kill "$pid"
                echo -e "   Stopped PID $pid"
            fi
            rm -f "$pidfile"
        fi
    done
    # Kill any remaining uvicorn/cloudflared
    pkill -f "uvicorn main:app" 2>/dev/null
    pkill -f "cloudflared tunnel run radar" 2>/dev/null
    echo -e "${GREEN}   ✅ All stopped${NC}"
}

show_status() {
    echo "═══════════════════════════════════════"
    echo "  📡 Livestream Radar — Status"
    echo "═══════════════════════════════════════"

    if [ -f "$PIDFILE_SERVER" ] && kill -0 "$(cat "$PIDFILE_SERVER")" 2>/dev/null; then
        echo -e "  Server:  ${GREEN}● Running${NC} (PID $(cat "$PIDFILE_SERVER"))"
    else
        echo -e "  Server:  ${RED}● Stopped${NC}"
    fi

    if [ -f "$PIDFILE_TUNNEL" ] && kill -0 "$(cat "$PIDFILE_TUNNEL")" 2>/dev/null; then
        echo -e "  Tunnel:  ${GREEN}● Running${NC} (PID $(cat "$PIDFILE_TUNNEL"))"
    else
        echo -e "  Tunnel:  ${RED}● Stopped${NC}"
    fi
    echo "═══════════════════════════════════════"
}

show_logs() {
    echo -e "${YELLOW}📋 Tailing logs (Ctrl+C to stop)...${NC}"
    tail -f "$LOG_SERVER" "$LOG_TUNNEL"
}

case "${1:-start}" in
    start)
        echo "═══════════════════════════════════════"
        echo "  📡 Livestream Radar — Starting"
        echo "═══════════════════════════════════════"
        start_server
        sleep 2
        start_tunnel
        echo "═══════════════════════════════════════"
        echo -e "  ${GREEN}Dashboard: https://radar.kiwibebe.shop${NC}"
        echo "═══════════════════════════════════════"
        ;;
    stop)
        stop_all
        ;;
    restart)
        stop_all
        sleep 2
        $0 start
        ;;
    status)
        show_status
        ;;
    logs)
        show_logs
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs}"
        exit 1
        ;;
esac

#!/bin/bash
# ═════════════════════════════════════════════════════════════
# Docker Health Check Script
# ═════════════════════════════════════════════════════════════
# 용도: 서비스 상태 점검 (Nginx + FastAPI)
# 실행: ./scripts/health-check.sh
# ═════════════════════════════════════════════════════════════

set -e

# ─────────────────────────────────────
# 색상 정의
# ─────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# ─────────────────────────────────────
# 설정
# ─────────────────────────────────────
HOST="${1:-localhost}"
NGINX_PORT="${2:-80}"
FASTAPI_PORT="${3:-8000}"

echo "════════════════════════════════════════════════"
echo "  Exchange Rate Service - Health Check"
echo "════════════════════════════════════════════════"
echo ""

# ─────────────────────────────────────
# Docker 컨테이너 상태
# ─────────────────────────────────────
echo "📦 Docker Container Status:"
echo "────────────────────────────────────────────────"
if command -v docker-compose &> /dev/null || command -v docker compose &> /dev/null; then
    docker compose ps 2>/dev/null || docker-compose ps 2>/dev/null || echo "Warning: Could not fetch container status"
else
    echo "Warning: docker-compose not found"
fi
echo ""

# ─────────────────────────────────────
# Nginx 헬스체크
# ─────────────────────────────────────
echo "🔍 Checking Nginx (http://${HOST}:${NGINX_PORT})..."
echo "────────────────────────────────────────────────"
if curl -f -s -o /dev/null -w "%{http_code}" "http://${HOST}:${NGINX_PORT}/health" &> /dev/null; then
    HTTP_CODE=$(curl -f -s -o /dev/null -w "%{http_code}" "http://${HOST}:${NGINX_PORT}/health")
    if [ "$HTTP_CODE" == "200" ]; then
        echo -e "${GREEN}✓ Nginx: OK${NC} (HTTP ${HTTP_CODE})"
    else
        echo -e "${YELLOW}⚠ Nginx: DEGRADED${NC} (HTTP ${HTTP_CODE})"
    fi
else
    echo -e "${RED}✗ Nginx: FAILED${NC}"
    exit 1
fi
echo ""

# ─────────────────────────────────────
# FastAPI 헬스체크 (컨테이너 내부)
# ─────────────────────────────────────
echo "🔍 Checking FastAPI..."
echo "────────────────────────────────────────────────"
if command -v docker &> /dev/null; then
    if docker exec exchange-rate-app curl -f -s "http://localhost:${FASTAPI_PORT}/health" > /dev/null 2>&1; then
        echo -e "${GREEN}✓ FastAPI: OK${NC}"
    else
        echo -e "${RED}✗ FastAPI: FAILED${NC}"
        exit 1
    fi
else
    echo "Warning: docker not found, skipping FastAPI internal check"
fi
echo ""

# ─────────────────────────────────────
# API 엔드포인트 테스트
# ─────────────────────────────────────
echo "🔍 Testing API Endpoints..."
echo "────────────────────────────────────────────────"

# /api/rates 테스트
if curl -f -s "http://${HOST}:${NGINX_PORT}/api/rates" > /dev/null 2>&1; then
    echo -e "${GREEN}✓ /api/rates: OK${NC}"
else
    echo -e "${YELLOW}⚠ /api/rates: FAILED${NC}"
fi

# WebSocket 테스트 (간단한 TCP 연결 확인)
if timeout 2 bash -c "</dev/tcp/${HOST}/${NGINX_PORT}" 2>/dev/null; then
    echo -e "${GREEN}✓ WebSocket port: OK${NC}"
else
    echo -e "${YELLOW}⚠ WebSocket port: Cannot connect${NC}"
fi
echo ""

# ─────────────────────────────────────
# 리소스 사용량
# ─────────────────────────────────────
echo "📊 Resource Usage:"
echo "────────────────────────────────────────────────"
if command -v docker &> /dev/null; then
    docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}" \
        exchange-rate-nginx exchange-rate-app 2>/dev/null || echo "Warning: Could not fetch resource stats"
else
    echo "Warning: docker not found"
fi
echo ""

# ─────────────────────────────────────
# 요약
# ─────────────────────────────────────
echo "════════════════════════════════════════════════"
echo -e "${GREEN}✓ Health check completed successfully${NC}"
echo "════════════════════════════════════════════════"

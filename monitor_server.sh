#!/bin/bash
# 서버 모니터링 스크립트 (부하 테스트 중 실행)
#
# 사용법: ./monitor_server.sh [duration_seconds]
# 예시: ./monitor_server.sh 180

DURATION=${1:-180}  # 기본값 180초 (3분)
INTERVAL=5          # 5초마다 측정
OUTPUT_DIR="load_test_results"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")

# 출력 디렉토리 생성
mkdir -p "$OUTPUT_DIR"

echo "🔍 서버 모니터링 시작 (${DURATION}초)"
echo "   간격: ${INTERVAL}초"
echo "   결과 저장: ${OUTPUT_DIR}/"
echo ""

# 1. Docker Stats (CPU, 메모리)
echo "📊 Docker Stats 수집 중..."
docker stats --no-stream --format "table {{.Container}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.NetIO}}" \
    > "${OUTPUT_DIR}/docker_stats_start_${TIMESTAMP}.txt"

# 2. Redis 상태 (초기)
echo "🔴 Redis 초기 상태 수집 중..."
{
    echo "=== Redis INFO memory ==="
    docker exec exchange-rate-redis redis-cli -a ${REDIS_PASSWORD:-RH7535753p*} INFO memory 2>/dev/null | grep -E "used_memory|maxmemory|mem_fragmentation"
    echo ""
    echo "=== Redis DBSIZE ==="
    docker exec exchange-rate-redis redis-cli -a ${REDIS_PASSWORD:-RH7535753p*} DBSIZE 2>/dev/null
    echo ""
    echo "=== Redis KEYS ==="
    docker exec exchange-rate-redis redis-cli -a ${REDIS_PASSWORD:-RH7535753p*} KEYS '*' 2>/dev/null
} > "${OUTPUT_DIR}/redis_start_${TIMESTAMP}.txt"

# 3. 연속 모니터링 (백그라운드)
{
    END_TIME=$(($(date +%s) + DURATION))
    SAMPLE=1

    while [ $(date +%s) -lt $END_TIME ]; do
        echo "=== Sample $SAMPLE ($(date '+%Y-%m-%d %H:%M:%S')) ==="

        # Docker Stats
        docker stats --no-stream --format "{{.Container}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}" | grep exchange-rate

        # Redis 메모리
        REDIS_MEM=$(docker exec exchange-rate-redis redis-cli -a ${REDIS_PASSWORD:-RH7535753p*} INFO memory 2>/dev/null | grep used_memory_human | cut -d: -f2 | tr -d '\r')
        echo "Redis Memory: $REDIS_MEM"

        echo ""
        SAMPLE=$((SAMPLE + 1))
        sleep $INTERVAL
    done
} > "${OUTPUT_DIR}/continuous_${TIMESTAMP}.txt" &

MONITOR_PID=$!
echo "   백그라운드 모니터링 PID: $MONITOR_PID"

# 4. Broadcasting 로그 수집 (백그라운드)
echo "📡 Broadcasting 로그 수집 중..."
docker compose logs -f fastapi 2>&1 | grep -E "브로드캐스트|Redis 업데이트|변경사항" > "${OUTPUT_DIR}/broadcast_${TIMESTAMP}.log" &
BROADCAST_PID=$!
echo "   Broadcasting 로그 PID: $BROADCAST_PID"

echo ""
echo "⏳ ${DURATION}초 동안 모니터링 중..."
echo "   (Ctrl+C로 중단 가능)"
echo ""

# 대기
sleep $DURATION

# 백그라운드 프로세스 종료
echo ""
echo "⏹️  모니터링 종료 중..."
kill $MONITOR_PID 2>/dev/null
kill $BROADCAST_PID 2>/dev/null

# 5. 최종 상태 수집
echo "📊 최종 상태 수집 중..."
docker stats --no-stream --format "table {{.Container}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.NetIO}}" \
    > "${OUTPUT_DIR}/docker_stats_end_${TIMESTAMP}.txt"

{
    echo "=== Redis INFO memory (최종) ==="
    docker exec exchange-rate-redis redis-cli -a ${REDIS_PASSWORD:-RH7535753p*} INFO memory 2>/dev/null | grep -E "used_memory|maxmemory|mem_fragmentation"
    echo ""
    echo "=== Redis DBSIZE (최종) ==="
    docker exec exchange-rate-redis redis-cli -a ${REDIS_PASSWORD:-RH7535753p*} DBSIZE 2>/dev/null
} > "${OUTPUT_DIR}/redis_end_${TIMESTAMP}.txt"

# 6. WebSocket 연결 수 로그
docker compose logs fastapi 2>&1 | grep -E "WebSocket|연결" | tail -50 > "${OUTPUT_DIR}/websocket_${TIMESTAMP}.log"

# 7. 요약 생성
echo ""
echo "✅ 모니터링 완료!"
echo ""
echo "📁 결과 파일:"
ls -lh "${OUTPUT_DIR}"/*${TIMESTAMP}* | awk '{print "   - " $9 " (" $5 ")"}'
echo ""
echo "📋 간단 요약:"
echo ""

# Docker Stats 비교
echo "Docker Stats 변화:"
echo "  시작:"
cat "${OUTPUT_DIR}/docker_stats_start_${TIMESTAMP}.txt" | grep exchange-rate | awk '{printf "    %s: CPU=%s MEM=%s\n", $1, $2, $4}'
echo "  종료:"
cat "${OUTPUT_DIR}/docker_stats_end_${TIMESTAMP}.txt" | grep exchange-rate | awk '{printf "    %s: CPU=%s MEM=%s\n", $1, $2, $4}'

echo ""

# Redis 메모리 비교
echo "Redis 메모리 변화:"
REDIS_START=$(grep "used_memory_human" "${OUTPUT_DIR}/redis_start_${TIMESTAMP}.txt" | cut -d: -f2 | tr -d '\r')
REDIS_END=$(grep "used_memory_human" "${OUTPUT_DIR}/redis_end_${TIMESTAMP}.txt" | cut -d: -f2 | tr -d '\r')
echo "  시작: $REDIS_START"
echo "  종료: $REDIS_END"

echo ""

# Broadcasting 로그 통계
TOTAL_BROADCASTS=$(cat "${OUTPUT_DIR}/broadcast_${TIMESTAMP}.log" | wc -l)
SKIP_COUNT=$(cat "${OUTPUT_DIR}/broadcast_${TIMESTAMP}.log" | grep "변경사항 없음" | wc -l)
UPDATE_COUNT=$(cat "${OUTPUT_DIR}/broadcast_${TIMESTAMP}.log" | grep "Redis 업데이트만" | wc -l)
SEND_COUNT=$(cat "${OUTPUT_DIR}/broadcast_${TIMESTAMP}.log" | grep "브로드캐스트 완료" | wc -l)

echo "Broadcasting 통계 (${DURATION}초 = 예상 $((DURATION / 10))회):"
echo "  총 로그: $TOTAL_BROADCASTS줄"
echo "  변경사항 없음: $SKIP_COUNT회"
echo "  Redis 업데이트만: $UPDATE_COUNT회"
echo "  브로드캐스트 완료: $SEND_COUNT회"

echo ""
echo "🔗 상세 분석은 ${OUTPUT_DIR}/ 디렉토리의 파일을 참고하세요."

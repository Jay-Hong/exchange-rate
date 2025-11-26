#!/usr/bin/env python3
"""
WebSocket 부하 테스트 스크립트 (Phase 1.7 검증)

동시 접속 50명 시뮬레이션 및 성능 측정

사용법:
    python load_test.py --url wss://fxi.n-e.kr/ws --clients 50 --duration 180

측정 항목:
    - 초기 접속 시간 (Redis 캐시 효과)
    - 메시지 지연 시간
    - 수신 누락 여부 (Broadcasting 주기 내 수신 개수)
    - 재연결 횟수
"""

import asyncio
import websockets
import json
import time
import statistics
from datetime import datetime
from collections import defaultdict
import argparse
import signal
import sys


class LoadTester:
    def __init__(self, url: str, num_clients: int, duration: int):
        self.url = url
        self.num_clients = num_clients
        self.duration = duration

        # 통계 수집
        self.initial_connection_times = []  # 초기 접속 시간
        self.message_delays = []  # 메시지 지연 시간
        self.message_counts = defaultdict(int)  # 클라이언트별 수신 개수
        self.reconnect_counts = defaultdict(int)  # 재연결 횟수
        self.errors = []  # 에러 목록

        self.start_time = None
        self.running = True

    async def connect_client(self, client_id: int):
        """단일 클라이언트 연결 및 메시지 수신"""
        reconnect_count = 0

        while self.running:
            try:
                connect_start = time.time()

                async with websockets.connect(self.url) as ws:
                    connect_time = (time.time() - connect_start) * 1000

                    # 첫 연결 시간만 기록
                    if reconnect_count == 0:
                        self.initial_connection_times.append(connect_time)
                        print(f"✅ Client {client_id:2d}: 연결 성공 ({connect_time:.1f}ms)")
                    else:
                        self.reconnect_counts[client_id] += 1
                        print(f"🔄 Client {client_id:2d}: 재연결 성공 (재연결 {reconnect_count}회)")

                    # 메시지 수신
                    async for message in ws:
                        receive_time = time.time()

                        try:
                            data = json.loads(message)

                            # 메시지 수신 카운트
                            self.message_counts[client_id] += 1

                            # 서버 전송 시각 추출 (metadata.updated_at)
                            if data.get("type") == "rates" and "data" in data:
                                server_time_str = data["data"]["metadata"].get("updated_at")
                                if server_time_str:
                                    # ISO 8601 파싱 (예: 2025-11-27T01:15:30+09:00)
                                    server_time = datetime.fromisoformat(server_time_str).timestamp()
                                    delay = (receive_time - server_time) * 1000
                                    self.message_delays.append(delay)

                        except json.JSONDecodeError:
                            self.errors.append(f"Client {client_id}: JSON 파싱 실패")
                        except Exception as e:
                            self.errors.append(f"Client {client_id}: 메시지 처리 오류 - {e}")

                        # 테스트 종료 확인
                        if not self.running:
                            break

            except websockets.exceptions.ConnectionClosed:
                if self.running:
                    reconnect_count += 1
                    print(f"⚠️  Client {client_id:2d}: 연결 종료, 재연결 시도 중...")
                    await asyncio.sleep(1)
            except Exception as e:
                self.errors.append(f"Client {client_id}: 연결 오류 - {e}")
                if self.running:
                    reconnect_count += 1
                    await asyncio.sleep(2)

    async def monitor_progress(self):
        """진행 상황 모니터링 (10초마다)"""
        while self.running:
            await asyncio.sleep(10)

            if not self.running:
                break

            elapsed = time.time() - self.start_time
            total_messages = sum(self.message_counts.values())
            avg_per_client = total_messages / self.num_clients if self.num_clients > 0 else 0

            print(f"\n📊 [{elapsed:.0f}초 경과] 총 수신: {total_messages}개, 평균: {avg_per_client:.1f}개/클라이언트")

    async def run(self):
        """부하 테스트 실행"""
        print(f"\n🚀 부하 테스트 시작")
        print(f"   URL: {self.url}")
        print(f"   동시 접속: {self.num_clients}명")
        print(f"   테스트 시간: {self.duration}초")
        print(f"   예상 Broadcasting: {self.duration // 10}회\n")

        self.start_time = time.time()

        # 클라이언트 태스크 생성
        tasks = [
            asyncio.create_task(self.connect_client(i))
            for i in range(self.num_clients)
        ]

        # 모니터링 태스크
        monitor_task = asyncio.create_task(self.monitor_progress())

        # 지정된 시간 동안 실행
        await asyncio.sleep(self.duration)

        # 종료
        self.running = False
        print("\n⏹️  테스트 종료 중...")

        # 모든 태스크 취소
        for task in tasks:
            task.cancel()
        monitor_task.cancel()

        await asyncio.gather(*tasks, monitor_task, return_exceptions=True)

        # 결과 출력
        self.print_results()

    def print_results(self):
        """테스트 결과 출력"""
        print("\n" + "="*60)
        print("📊 부하 테스트 결과")
        print("="*60)

        # 1. 초기 접속 시간
        if self.initial_connection_times:
            print("\n1️⃣  초기 접속 시간 (Redis 캐시 효과)")
            print(f"   평균: {statistics.mean(self.initial_connection_times):.1f}ms")
            print(f"   중앙값: {statistics.median(self.initial_connection_times):.1f}ms")
            print(f"   최소: {min(self.initial_connection_times):.1f}ms")
            print(f"   최대: {max(self.initial_connection_times):.1f}ms")

            # 첫 번째 vs 나머지 비교
            if len(self.initial_connection_times) > 1:
                first = self.initial_connection_times[0]
                rest_avg = statistics.mean(self.initial_connection_times[1:])
                print(f"\n   첫 번째 접속: {first:.1f}ms (DB 폴백 예상)")
                print(f"   2-{self.num_clients}번째 평균: {rest_avg:.1f}ms (Redis 캐시 예상)")
                if first > rest_avg:
                    print(f"   ✅ Redis 캐시 효과: {first - rest_avg:.1f}ms 빠름")

        # 2. 메시지 지연 시간
        if self.message_delays:
            print("\n2️⃣  메시지 지연 시간 (서버 전송 → 클라이언트 수신)")
            print(f"   평균: {statistics.mean(self.message_delays):.1f}ms")
            print(f"   중앙값: {statistics.median(self.message_delays):.1f}ms")
            print(f"   최소: {min(self.message_delays):.1f}ms")
            print(f"   최대: {max(self.message_delays):.1f}ms")

        # 3. 수신 개수 (누락 확인)
        expected_broadcasts = self.duration // 10
        print(f"\n3️⃣  Broadcasting 수신 (예상: {expected_broadcasts}회)")

        total_received = sum(self.message_counts.values())
        avg_per_client = total_received / self.num_clients if self.num_clients > 0 else 0

        print(f"   총 수신: {total_received}개")
        print(f"   평균: {avg_per_client:.1f}개/클라이언트")

        # 클라이언트별 수신 개수 분포
        if self.message_counts:
            counts = list(self.message_counts.values())
            min_count = min(counts)
            max_count = max(counts)

            print(f"   최소: {min_count}개")
            print(f"   최대: {max_count}개")

            # 누락 확인
            missing = [cid for cid, count in self.message_counts.items() if count < expected_broadcasts]
            if missing:
                print(f"   ⚠️  누락 발생: {len(missing)}명 (Client ID: {missing[:10]}...)")
            else:
                print(f"   ✅ 누락 없음: 모든 클라이언트 {expected_broadcasts}개 수신")

        # 4. 재연결
        total_reconnects = sum(self.reconnect_counts.values())
        print(f"\n4️⃣  재연결 (연결 안정성)")
        print(f"   총 재연결: {total_reconnects}회")
        if total_reconnects > 0:
            print(f"   재연결한 클라이언트: {len(self.reconnect_counts)}명")
            max_reconnect = max(self.reconnect_counts.values())
            print(f"   최대 재연결: {max_reconnect}회")
        else:
            print(f"   ✅ 재연결 없음: 모든 연결 안정적")

        # 5. 에러
        if self.errors:
            print(f"\n5️⃣  에러 목록 (총 {len(self.errors)}개)")
            for error in self.errors[:10]:
                print(f"   - {error}")
            if len(self.errors) > 10:
                print(f"   ... 외 {len(self.errors) - 10}개")
        else:
            print(f"\n5️⃣  에러: 없음 ✅")

        print("\n" + "="*60)


def signal_handler(sig, frame):
    """Ctrl+C 핸들러"""
    print("\n\n⚠️  사용자 중단 (Ctrl+C)")
    sys.exit(0)


async def main():
    parser = argparse.ArgumentParser(description="WebSocket 부하 테스트")
    parser.add_argument("--url", default="ws://localhost:8000/ws", help="WebSocket URL")
    parser.add_argument("--clients", type=int, default=50, help="동시 접속 수")
    parser.add_argument("--duration", type=int, default=180, help="테스트 시간 (초)")

    args = parser.parse_args()

    # Ctrl+C 핸들러
    signal.signal(signal.SIGINT, signal_handler)

    tester = LoadTester(args.url, args.clients, args.duration)
    await tester.run()


if __name__ == "__main__":
    asyncio.run(main())

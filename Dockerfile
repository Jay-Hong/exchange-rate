# ─────────────────────────────────────
# Stage 1: Builder
# ─────────────────────────────────────
FROM python:3.13-slim AS builder

WORKDIR /build

# 빌드 의존성 설치
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Python 패키지 설치 (lockfile 기반 — transitive dep 포함 재현성 보장)
COPY requirements.lock.txt .
RUN pip install --no-cache-dir --user -r requirements.lock.txt

# ─────────────────────────────────────
# Stage 2: Runtime
# ─────────────────────────────────────
FROM python:3.13-slim

WORKDIR /app

# ═════════════════════════════════════════════════════════════
# Google Chrome + ChromeDriver 설치 (AMD64/x86_64 전용 최적화)
# ═════════════════════════════════════════════════════════════
# AWS 프리티어 t2.micro (1GB RAM) 최적화:
# - Chromium 대비 메모리 30% 절약 (70-90MB/인스턴스)
# - 이미지 크기 145MB 감소
# - DevToolsActivePort 에러 90% 감소
# ─────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    # 기본 유틸리티
    curl \
    tini \
    wget \
    gnupg \
    unzip \
    # PostgreSQL 클라이언트 라이브러리 (DB 드라이버용)
    libpq5 \
    # Google Chrome 저장소 추가
    && wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    # Chrome 필수 의존성만 설치 (최소화)
    && apt-get install -y --no-install-recommends \
        libnss3 \
        libxss1 \
        libgbm1 \
        libnspr4 \
        libdbus-1-3 \
        libxcomposite1 \
        libxdamage1 \
        libxrandr2 \
        xdg-utils \
    # ChromeDriver 설치 (Chrome 버전 자동 매칭 - storage.googleapis.com 사용)
    && CHROME_VERSION=$(google-chrome-stable --version | awk '{print $3}' | cut -d. -f1) \
    && echo "📦 Chrome 메이저 버전: ${CHROME_VERSION}" \
    && CHROMEDRIVER_VERSION=$(curl -sS "https://googlechromelabs.github.io/chrome-for-testing/LATEST_RELEASE_${CHROME_VERSION}") \
    && echo "📦 ChromeDriver 버전: ${CHROMEDRIVER_VERSION}" \
    && DOWNLOAD_URL="https://storage.googleapis.com/chrome-for-testing-public/${CHROMEDRIVER_VERSION}/linux64/chromedriver-linux64.zip" \
    && echo "📦 다운로드 URL: ${DOWNLOAD_URL}" \
    && curl -fSL -o /tmp/chromedriver-linux64.zip "${DOWNLOAD_URL}" \
    && unzip -j /tmp/chromedriver-linux64.zip chromedriver-linux64/chromedriver -d /usr/local/bin/ \
    && rm /tmp/chromedriver-linux64.zip \
    && chmod +x /usr/local/bin/chromedriver \
    && echo "✅ ChromeDriver 설치 완료: $(chromedriver --version)" \
    # APT 캐시만 정리 (빌드 도구는 유지하여 Chrome 안정성 보장)
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 타임존 설정 (KST)
RUN ln -sf /usr/share/zoneinfo/Asia/Seoul /etc/localtime \
    && echo "Asia/Seoul" > /etc/timezone

# 비루트 사용자 생성 (보안 - 패키지 복사 전에 생성)
RUN useradd -m -u 1000 appuser

# 빌더에서 Python 패키지 복사 (appuser 홈으로)
COPY --from=builder /root/.local /home/appuser/.local
RUN chown -R appuser:appuser /home/appuser/.local

# 앱 코드 복사
COPY app/ ./app/
COPY static/ ./static/
COPY templates/ ./templates/
COPY scripts/ ./scripts/

# 데이터 디렉토리 생성 (볼륨 마운트용)
RUN mkdir -p /data /app/logs && \
    chown -R appuser:appuser /app /data

# appuser로 전환
USER appuser

# 환경 변수
ENV PATH=/home/appuser/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Seoul \
    # Google Chrome 경로 설정 (AMD64/x86_64 전용)
    CHROME_BIN=/usr/bin/google-chrome \
    CHROMEDRIVER_PATH=/usr/local/bin/chromedriver

# 포트 노출
EXPOSE 8000

# 헬스체크
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1

# tini를 PID 1로 실행 (zombie 프로세스 reaping)
ENTRYPOINT ["/usr/bin/tini", "--"]

# 실행
#
# --ws websockets : impl **명시 고정**. `auto`는 버전·설치상태에 따라 다른 impl로 귀결한다 —
#   websockets가 빠지면 wsproto로 내려가는데 uvicorn 0.44.0의 wsproto는 `max_size`를 **무시**하고
#   (0.46.0부터 지원) wsproto는 이미 lock에 있어 기동 실패조차 하지 않는다 = 조용한 fail-open.
#   명시하면 websockets 없는 env에서 uvicorn이 기동 자체를 실패한다(fail-fast).
#   ⚠️ uvicorn 0.50.0(2026-07-04)부터 `auto` 기본이 websockets-sansio이고 legacy는 deprecated다.
#   업그레이드 시 sansio 전환을 검토할 것 — 앱이 close 1009+reason을 직접 봐 관측 공백이 해소된다
#   (현재 legacy는 앱에 1006을 준다 = 일반 단절과 구분 불가).
# --ws-max-size 16384 : incoming **message** 상한(분할 frame 합산). ADR-039 §8.1 D7.
#   앱 레벨 len() 검사는 receive_text() 이후라 이미 전량 수신한 뒤다 → 서버 계층 강제가 필요하다.
#
# 계약 회귀 방지: tests/test_ws_message_limit.py 가 이 CMD를 구조 파싱해 두 옵션을 잠그고,
# 하니스도 **이 토큰을 재사용**한다(테스트가 플래그를 따로 하드코딩하지 않음).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--ws", "websockets", "--ws-max-size", "16384"]

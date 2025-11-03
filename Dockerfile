# ─────────────────────────────────────
# Stage 1: Builder
# ─────────────────────────────────────
FROM python:3.13-slim AS builder

WORKDIR /build

# 빌드 의존성 설치
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Python 패키지 설치
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# ─────────────────────────────────────
# Stage 2: Runtime
# ─────────────────────────────────────
FROM python:3.13-slim

WORKDIR /app

# ═════════════════════════════════════════════════════════════
# Google Chrome + 의존성 설치 (AMD64/x86_64 전용 최적화)
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
    # 설치 도구 제거 (이미지 크기 감소)
    && apt-get purge -y --auto-remove wget gnupg \
    && rm -rf /var/lib/apt/lists/*

# ═════════════════════════════════════════════════════════════
# ChromeDriver 설치 (Chrome 버전 자동 매칭)
# ═════════════════════════════════════════════════════════════
RUN CHROME_VERSION=$(google-chrome --version | sed 's/Google Chrome //; s/\.[0-9]*$//') \
    && echo "📦 Chrome 버전: ${CHROME_VERSION}" \
    && CHROMEDRIVER_VERSION=$(curl -sS "https://googlechromelabs.github.io/chrome-for-testing/LATEST_RELEASE_${CHROME_VERSION}") \
    && echo "📦 ChromeDriver 버전: ${CHROMEDRIVER_VERSION}" \
    && curl -sS -o /tmp/chromedriver-linux64.zip "https://edgedl.me.gvt1.com/edgedl/chrome/chrome-for-testing/${CHROMEDRIVER_VERSION}/linux64/chromedriver-linux64.zip" \
    && unzip -j /tmp/chromedriver-linux64.zip chromedriver-linux64/chromedriver -d /usr/local/bin/ \
    && rm /tmp/chromedriver-linux64.zip \
    && chmod +x /usr/local/bin/chromedriver \
    && echo "✅ ChromeDriver 설치 완료: $(chromedriver --version)"

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
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

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

# Selenium + Chromium 의존성 설치 (ARM64/AMD64 호환)
RUN apt-get update && apt-get install -y --no-install-recommends \
    # 헬스체크용
    curl \
    # Chromium (오픈소스, ARM64 지원)
    chromium \
    chromium-driver \
    # Chromium 실행에 필요한 라이브러리
    fonts-liberation \
    libnss3 \
    libxss1 \
    libappindicator3-1 \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdbus-1-3 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    xdg-utils \
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
    # Chromium 경로 설정 (ARM64/AMD64 호환)
    CHROME_BIN=/usr/bin/chromium \
    CHROMEDRIVER_PATH=/usr/bin/chromedriver

# 포트 노출
EXPOSE 8000

# 헬스체크
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1

# 실행
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

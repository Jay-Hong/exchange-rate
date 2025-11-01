# ─────────────────────────────────────
# Nginx Reverse Proxy
# ─────────────────────────────────────
FROM nginx:alpine

# 헬스체크 스크립트 복사 (선택사항)
RUN apk add --no-cache curl

# 기본 설정 제거
RUN rm /etc/nginx/conf.d/default.conf

# 커스텀 에러 페이지 (선택사항)
COPY html/ /usr/share/nginx/html/

# 헬스체크
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
  CMD nginx -t || exit 1

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]

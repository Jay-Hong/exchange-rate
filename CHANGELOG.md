# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.9.0] - 2026-01-08

### Added - Account Deletion API (Apple App Store 5.1.1(v) Compliance)

- **Account deletion endpoint**: `DELETE /api/user/me`
  - Deletes all user data: `notification_logs`, `notification_settings`, `user_devices`
  - Response: `204 No Content` on success
  - Security: `check_revoked=True` for destructive operations
- **Enhanced Firebase token verification**
  - New parameter: `check_revoked` for revoked token detection
  - New error handling: `RevokedIdTokenError` → 401, `CertificateFetchError` → 503
  - Network error handling: `TransportError`, `RequestException` → 503

### Planned
- Dynamic priority adjustment based on crawler success rate (Phase 2)
- Multiple Queue system for crawler groups (Phase 3)

---

## [1.8.0] - 2025-12-02

### Added - 24-Hour Graph Feature with WebSocket Integration

- **24-hour graph API** with 10-minute bucket aggregation
  - Endpoint: `GET /api/graph/{currency}` (usd-krw, jpy-krw, eur-krw)
  - Data format: `[timestamp, max, min, close]` (candlestick structure)
  - Carry-forward mechanism: Empty buckets filled with previous close value for data continuity
  - Redis cache: `graph:{currency}` keys (~3.6KB per currency, 120s TTL)
  - Backend: `app/admin/graph_cache.py` - Scheduler runs every minute at :03 seconds
- **WebSocket graph integration** for real-time updates
  - `graph_buckets` field: Last bucket for all 3 currencies (~600 bytes)
  - Broadcast trigger: Only when rates change (change detection)
  - Mobile optimization: Reuse existing WebSocket connection (no additional radio activations)
  - Server efficiency: 1 broadcast/10s vs 100+ HTTP req/min (99% CPU reduction)
- **Band Chart implementation** (frontend)
  - Single source: Close line (main) + High/Low translucent bands
  - Multi-source: Close lines only for comparison
  - Tooltip filtering: Range display for single-source mode
  - Y-axis padding: 5% margin for better visualization
- **Lazy loading with cache strategy**
  - Initial load: Selected currency only (3.6KB)
  - Currency switch: Cache reuse (0 bytes) or on-demand load
  - Gap detection: 15-minute threshold → full refresh
  - Frontend cache: In-memory storage for all 3 currencies
- **Toggle controls** for graph customization
  - Show/Hide individual sources (INVESTING, KB, HANA)
  - Prevent empty graphs: At least 1 source required
  - Persistent selection across currency switches

### Performance
- **Network efficiency**: WebSocket integration reduces mobile data usage by 95%+
- **Initial load**: 3.6KB per currency (145 buckets × 3 sources × 4 values)
- **WebSocket overhead**: +600 bytes per broadcast (0.6KB / 10s)
- **Cache hit rate**: ~100% for currency switches (no repeated API calls)
- **Mobile battery**: Zero additional impact (reuses existing WebSocket)

### Documentation
- Created [GRAPH_FEATURE.md](GRAPH_FEATURE.md) v3.0 - Complete implementation guide
- Added [ADR-015](DECISIONS.md#adr-015-websocket-graph-integration-vs-incremental-api) - Graph update strategy decision

---

## [1.7.0] - 2025-11-27

### Added - Redis Broadcast Cache & Change Detection

- **Redis broadcast cache** for WebSocket initial connection optimization
  - Cache key: `broadcast:latest` (~3.6KB JSON payload)
  - Initial connection: Redis cache → instant data delivery
  - Memory usage: ~1MB stable (~1% of 100MB maxmemory)
  - Circuit Breaker: 5 failures → 30s timeout → auto-recovery
  - Admin API endpoint: `GET /admin/api/redis-status`
- **Change detection system** for broadcast efficiency
  - JSON comparison: `new_json != cached_json`
  - Smart logging: "⏸️ 변경사항 없음" when no change, "📡 브로드캐스트 완료" when changed
  - Bandwidth optimization: Only broadcast when data actually changes
- **Redis monitoring card** in admin dashboard
  - Real-time memory usage and key count display
  - Circuit breaker status visualization
  - Color-coded health indicators (connected/circuit-open/disconnected)

### Fixed
- **Broadcasting bug**: `updated_at` timestamp issue causing "always different" comparisons
  - Root cause: Used `datetime.now()` instead of DB's actual latest timestamp
  - Impact: Change detection never triggered, wasted bandwidth
  - Fix: Use `max(rate["timestamp"])` from DB data for accurate comparison
  - Result: Change detection accuracy improved to 100%

### Performance
- **WebSocket initial connection**: Instant data delivery via Redis cache
- **Bandwidth optimization**: Significant reduction in unnecessary broadcasts during low-volatility periods

### Documentation
- Updated [REDIS_IMPACT_ANALYSIS.md](REDIS_IMPACT_ANALYSIS.md) with actual measurements
- Updated [PRODUCTION_CHECKLIST.md](PRODUCTION_CHECKLIST.md): Corrected ElastiCache free tier info

---

## [1.6.0] - 2025-11-10

### Added - 3-Tier Scheduling Architecture
- **Crawler statistics system** for monitoring success rate and performance
  - Real-time tracking: success/fail counts, avg duration, last execution time
  - Thread-safe collector with singleton pattern
  - Admin API endpoint: `GET /admin/api/crawler/stats`
  - Integrated with both Request and Selenium crawlers
- **3-Tier scheduling architecture** optimized for t3.small/medium:
  - **Tier A (investing)**: Most critical, highest frequency
  - **Tier B (kb, hana, woori, bs, citi)**: Important, moderate frequency
  - **Tier C (shinhan, ibk, nh, sc)**: Selenium-based, lowest frequency
- **IN mode: cron-based absolute timing** (Broadcasting synchronization)
  - A Group: 10s interval (5s before Broadcasting @ 00, 10, 20s)
  - B Group: 20-60s interval (3-7s before Broadcasting, staggered)
  - C Group: 33.3-150s interval (Broadcasting independent)
- **OUT mode: cron-based hourly distribution** (Zero concurrent execution)
  - A Group: Every 10 minutes (05, 15, 25, 35, 45, 55 min)
  - B Group: Every 10-60 minutes (fully distributed across the hour)
  - C Group: Once per hour (fully distributed: 07, 17, 27, 36, 47, 57 min)

### Changed
- **Worker health check optimization**: 180s → 90s stuck detection
  - Rationale: Timeout 45s × 2 = 90s is sufficient (heartbeat updates at job start)
  - Faster problem detection and auto-restart
- **Selenium timeout adjustment**: shinhan 60s → 45s (consistency with other crawlers)
- **Scheduling strategy**:
  - IN mode: cron with second precision (e.g., `second='5,15,25,35,45,55'`)
  - OUT mode: cron with minute precision (e.g., `minute='5,15,25,35,45,55'`)
  - Request crawlers: Direct execution with stats wrapper
  - Selenium crawlers: Queue-based execution (unchanged)
- **Statistics wrapper**: All crawlers now tracked via `make_request_crawler_wrapper()`

### Performance
- **OUT mode resource distribution**: 10 crawlers spread across 60 minutes
  - Peak concurrent crawlers: 1 (down from potential 6-8)
  - CPU spike elimination: No simultaneous execution
- **Faster failure detection**: Worker restart in 90s vs 180s
- **Better monitoring**: Real-time success rate and duration tracking per crawler

### Documentation
- Added [ADR-009](DECISIONS.md) documenting 3-Tier scheduling architecture
- Updated scheduler.py with comprehensive inline documentation
- Added `app/admin/crawler_stats.py` with usage examples

---

## [1.5.0] - 2025-11-10

### Added
- **Worker health check system** for detecting and auto-restarting stuck workers
  - Heartbeat tracking: Updates every job completion
  - Stuck detection: >180 seconds on same job → automatic restart
  - Health check interval: Every 60 seconds
- **Queue pressure relief policy** (80% threshold)
  - Rejects new jobs when queue >80% full (20/25)
  - Prevents queue overflow and APScheduler blocking
  - Logs rejected jobs for monitoring
- Worker status tracking: `selenium_worker_last_heartbeat`, `selenium_worker_current_job`

### Changed
- **Timeout reduction (50% cut)** for real-time performance:
  - hana: 60s → 30s
  - ibk: 90s → 45s
  - nh: 90s → 45s
  - sc: 90s → 45s
  - shinhan: 120s → 60s
- **Queue size optimization**: 50 → 25
  - Rationale: 80% pressure relief (20/25) + memory savings
  - Prevents excessive job accumulation
- **Crawler schedule adjustment** (IN mode):
  - hana: 20s (unchanged, fastest crawler)
  - shinhan: 60s (adjusted for queue balance)
  - ibk: 60s (reduced frequency for slow crawler)
  - nh: 90s (further reduced for slowest crawler)
  - sc: 120s (minimal frequency for reliability)

### Fixed
- **Queue saturation (100%)** → Reduced to ~80% with pressure relief
- **Worker stuck issue** → Auto-restart when heartbeat >180s
- **Real-time performance degradation** → NH crawler timeout enforced at 45s
- **APScheduler blocking** → Non-blocking queue operations prevent scheduler freeze

### Performance
- Queue utilization: 100% (50/50) → 80% (20/25) stable
- Timeout enforcement: Slow crawlers (66s) now properly timeout at 45s
- Memory savings: Queue size reduction contributes to overall stability
- Worker reliability: Auto-restart ensures continuous operation

### Tradeoffs
- ⚠️ **Real-time vs Completeness**: Timeouts may reject slow crawlers during high Swap usage
- ⚠️ **Queue pressure**: 80% rejection may skip some scheduled jobs (retry on next cycle)
- ✅ **System stability**: Prioritized over 100% data collection rate

### Documentation
- Added [ADR-008](DECISIONS.md) documenting Queue pressure relief strategy
- Updated [CLAUDE.md](CLAUDE.md) scheduling section with new queue size and policies

---

## [1.4.0] - 2025-11-08

### Added
- **Priority Queue system** for Selenium crawlers (AsyncIO PriorityQueue)
- Crawler priority mapping based on execution speed:
  - hana (0): Fastest → 1st priority
  - ibk (1): Medium → 2nd priority
  - nh, sc (2-3): Slow → 3rd-4th priority
  - shinhan (4): Slowest → 5th priority
- **Individual timeout settings** per crawler (60-120 seconds)
- **Automatic retry mechanism** with lower priority (+1000) on failure
- `execute_with_timeout()` wrapper function for timeout enforcement
- Chrome memory optimization:
  - `--window-size=400,300` (reduced from 800x600)
  - `--disable-javascript` for JS engine memory saving
  - `--disable-webgl` for WebGL memory release
- Monitoring logging enabled (DEBUG → INFO level)

### Changed
- `asyncio.Queue` → `asyncio.PriorityQueue` in `app/scheduler.py`
- Selenium timeout values increased to handle Swap I/O delays:
  - hana: 30 → 60 seconds
  - ibk/nh/sc: 60 → 90 seconds
  - shinhan: 90 → 120 seconds
- Queue enqueue format: simple data → priority tuple `(priority, timestamp, job_func, bank_name, is_retry)`
- Worker execution: added timeout wrapper and retry logic
- PriorityQueue maxsize: 10 → 20 (increased capacity)

### Fixed
- **Selenium crawler timeout issues** caused by memory pressure (100% resolution)
- Swap memory usage causing 2-3x slower Chrome process creation
- False timeout failures when Swap I/O delays exceeded fixed timeout limits
- Slow crawlers blocking fast crawlers in simple FIFO Queue

### Performance
- **Timeout occurrence**: 80% failure rate → 0% (60-second monitoring)
- Crawler execution times: All within normal range (0.36-17.50 seconds)
- Priority Queue ordering: Guaranteed execution order (hana → ibk → nh → sc → shinhan)
- Chrome memory per instance: Estimated 50-80MB additional savings
- System stability: RAM 71.9%, Swap 28.2% with no timeouts

### Documentation
- Added [ADR-007](DECISIONS.md) documenting Priority Queue + Timeout strategy
- Created [MAINTENANCE_2025-11-08.md](MAINTENANCE_2025-11-08.md) with detailed implementation
- Updated [CLAUDE.md](CLAUDE.md) scheduling section with Priority Queue info
- Updated Docker deployment section with 3-tier approach (daily/cache/reset)

---

## [1.3.0] - 2025-11-06

### Added
- AsyncIO Queue system for Selenium crawler sequential execution
- Queue Worker for processing Selenium tasks one at a time
- `init_selenium_queue()` and `shutdown_selenium_queue()` lifecycle management
- Crawler grouping: `REQUEST_BASED_TASKS` and `SELENIUM_BASED_TASKS`

### Changed
- **Breaking:** Replaced `BackgroundScheduler` with `AsyncIOScheduler`
- Split crawler tasks into Request-based (concurrent) and Selenium-based (sequential)
- Modified `switch_jobs()` to handle two different execution patterns
- Updated FastAPI `lifespan()` to initialize/shutdown Queue system

### Removed
- Threading Semaphore from `app/crawlers/utils.py`
- Semaphore acquire/release logic from all Selenium crawlers

### Fixed
- Selenium crawler Semaphore contention causing false MIBANK fallbacks (30-40% reduction)
- Memory instability from multiple Chrome instances (50% reduction: 650MB → 320MB)
- Unnecessary fallback to MIBANK when normal collection was possible

### Performance
- Memory usage: 650MB → 320MB (50% improvement)
- Fallback occurrence: 30-40% → 0%
- Sequential execution guarantee for Selenium crawlers

### Documentation
- Added [ADR-006](DECISIONS.md) documenting AsyncIO Queue decision
- Created [MAINTENANCE_2025-11-06.md](MAINTENANCE_2025-11-06.md) with detailed implementation
- Updated [CLAUDE.md](CLAUDE.md) scheduling section

---

## [1.2.0] - 2025-11-05

### Added
- System monitoring module (`app/admin/monitor.py`)
- Chrome process monitoring with count and memory tracking
- Zombie Chrome process cleanup scheduler (runs every 3 minutes)
- Force kill logic for Chrome processes older than 5 minutes
- Monitoring statistics collection (every 5 minutes)
- Admin dashboard API endpoints:
  - `GET /admin/api/monitor/current` - Current system status
  - `GET /admin/api/monitor/history?hours=N` - Historical data
- Chrome process card in admin dashboard UI

### Changed
- Chrome max lifetime: 10 minutes → 5 minutes (`CHROME_MAX_LIFETIME_SECONDS`)
- Chrome cleanup interval: 5 minutes → 3 minutes (`CHROME_CLEANUP_INTERVAL_MINUTES`)
- Log backup count: 5 → 3 (50MB → 30MB total)
- Docker memory limit: 700M → 800M
- Disabled unnecessary system services (snapd, multipathd)

### Fixed
- Selenium zombie process accumulation causing memory leaks
- WebSocket ConnectionManager ValueError on disconnect
- Unsafe list iteration in WebSocket broadcast causing potential crashes
- Chrome processes not properly terminated after `driver.quit()` failure

### Performance
- Memory usage: 647MB → 581MB (66MB improvement)
- Free memory: 309MB → 375MB
- Disk usage: 15GB → 6.3GB (8.7GB reclaimed)
- Chrome zombie process automatic cleanup every 3 minutes

### Documentation
- Created [MAINTENANCE_2025-11-05.md](MAINTENANCE_2025-11-05.md)
- Added [REBOOT_CHECKLIST.md](REBOOT_CHECKLIST.md)

---

## [1.1.0] - 2025-10-26

### Changed
- SC bank crawler: Switched from Selenium to Request-based (cost reduction)
- WOORI bank crawler: Applied SC approach (simplified logic)
- IBK bank crawler: Added Selenium retry logic with MIBANK fallback

### Fixed
- SC bank Alert handling consolidated to single pattern
- Selector fallback improvements for multiple banks

---

## [1.0.1] - 2025-10-25

### Added
- Domain-based project structure:
  - `app/crawlers/` - Crawler domain
  - `app/admin/` - Admin domain
  - `app/notifications/` - Notification domain
- Centralized constants: `app/crawlers/constants.py`
- Common crawler utilities: `app/crawlers/utils.py`
- Log cleaner module: `app/admin/log_cleaner.py`
- WebSocket broadcast statistics: `app/admin/stats.py`

### Changed
- Refactored 10 crawler files to use centralized constants and utilities
- Moved admin-related logic to dedicated domain
- Improved code organization and maintainability

### Documentation
- Added [CRAWLERS.md](CRAWLERS.md) - Detailed crawler implementation guide
- Added [DECISIONS.md](DECISIONS.md) - Architecture Decision Records (ADR)

---

## [1.0.0] - 2025-10-17

### Added
- Initial release of Exchange Rate Comparison Service
- 10 crawler sources:
  - Investing.com (reference rate)
  - 9 Korean banks: KB, Hana, Shinhan, Woori, IBK, NH, SC, Busan, Citi
- Support for 3 currency pairs: USD-KRW, JPY-KRW, EUR-KRW
- FastAPI backend with WebSocket support
- SQLite database with automatic cleanup (10-day retention)
- APScheduler with IN/OUT mode (business hours detection)
- Admin dashboard (`/admin`) with:
  - Real-time crawler status
  - Log viewer (2 tabs: all logs, errors only)
  - WebSocket connection monitoring
  - Memory usage tracking
  - Broadcast statistics
- REST API endpoints:
  - `GET /api/rates` - All exchange rates
  - `GET /api/rates/{currency}` - Specific currency
  - `GET /api/investing/{pair}` - Investing.com rates
  - `GET /api/banks/{pair}` - All bank rates
  - `GET /health` - Health check
- Docker deployment with:
  - Multi-stage build
  - Google Chrome (AMD64 optimized)
  - 700MB memory limit
- Structured logging system:
  - JSON format in production
  - Colored output in development
  - Log rotation (10MB, 5 backups)
  - Auto cleanup (10 days)

### Documentation
- [CLAUDE.md](CLAUDE.md) - Project guide
- [DOCKER.md](DOCKER.md) - Docker deployment guide

---

## Version History

| Version | Date | Highlights |
|---------|------|------------|
| 1.9.0 | 2026-01-08 | Account Deletion API (Apple App Store 5.1.1(v) Compliance) |
| 1.8.0 | 2025-12-02 | 24-hour graph + WebSocket integration + Band Chart |
| 1.7.0 | 2025-11-27 | Redis broadcast cache + Change detection |
| 1.6.0 | 2025-11-10 | 3-Tier scheduling + Crawler statistics |
| 1.5.0 | 2025-11-10 | Queue pressure relief + Health check + Timeout optimization |
| 1.4.0 | 2025-11-08 | Priority Queue + Timeout strategy |
| 1.3.0 | 2025-11-06 | AsyncIO Queue for Selenium crawlers |
| 1.2.0 | 2025-11-05 | System monitoring & zombie process cleanup |
| 1.1.0 | 2025-10-26 | Crawler optimizations (SC, WOORI, IBK) |
| 1.0.1 | 2025-10-25 | Domain-based refactoring |
| 1.0.0 | 2025-10-17 | Initial release |

---

## Migration Guide

### Upgrading from 1.4.x to 1.5.x

**Non-Breaking Changes:**
- No API changes
- No configuration changes required
- Drop-in replacement for v1.4.x

**Steps:**
1. Pull latest code
2. Review changes in `DECISIONS.md` (ADR-008: Queue pressure relief strategy)
3. Rebuild Docker: `docker compose up -d --build`
4. Monitor queue pressure: `docker logs -f exchange-rate-app | grep "Queue 압력"`
5. Verify health check: `docker logs -f exchange-rate-app | grep "헬스체크\|멈춤 감지"`

**Expected Improvements:**
- Queue saturation reduced from 100% to ~80%
- Real-time performance improved (NH crawler now timeout at 45s)
- Automatic worker recovery on stuck (>180s)
- Memory savings from smaller queue (50 → 25)

**Monitoring:**
- Watch for "Queue 압력 초과로 skip" warnings (acceptable, system working as designed)
- Verify timeout enforcement: Slow crawlers should timeout within new limits
- Check health check logs: Workers should auto-restart if stuck >180s

**Rollback:**
- Revert `app/crawlers/constants.py` SELENIUM_TIMEOUT_MAP (×2 values)
- Revert `app/scheduler.py` queue size (25 → 50) and remove pressure relief logic
- Comment out health check function and job

---

### Upgrading from 1.3.x to 1.4.x

**Non-Breaking Changes:**
- No API changes
- No configuration changes required
- Drop-in replacement for v1.3.x

**Steps:**
1. Pull latest code
2. Review `MAINTENANCE_2025-11-08.md` for detailed changes
3. Rebuild Docker: `docker compose up -d --build` (or `--no-cache` if issues)
4. Verify Priority Queue in logs: `docker logs -f exchange-rate-app | grep "우선순위\|Priority"`

**Expected Improvements:**
- 100% resolution of Selenium timeout issues
- Guaranteed execution order (fast crawlers first)
- Additional 50-80MB memory savings per Chrome instance
- Automatic retry for failed crawlers

**Rollback:**
- All changes preserved as comments in code
- Simply uncomment old code and recomment new code in:
  - `app/crawlers/constants.py`
  - `app/admin/monitor.py`

---

### Upgrading from 1.2.x to 1.3.x

**Breaking Changes:**
- `BackgroundScheduler` → `AsyncIOScheduler`
- If you have custom scheduler code, update to use async-compatible patterns

**Steps:**
1. Pull latest code
2. Review `MAINTENANCE_2025-11-06.md` for detailed changes
3. Rebuild Docker: `docker compose build --no-cache`
4. Restart: `docker compose up -d`
5. Verify Queue Worker in logs: `docker logs -f exchange-rate-app | grep "Queue"`

**Expected Improvements:**
- 50% memory reduction (650MB → 320MB)
- 100% elimination of false MIBANK fallbacks
- Smoother Selenium crawler execution

---

## Contributing

Please update this CHANGELOG when making significant changes following these guidelines:

- **Added** for new features
- **Changed** for changes in existing functionality
- **Deprecated** for soon-to-be removed features
- **Removed** for now removed features
- **Fixed** for any bug fixes
- **Security** for vulnerability fixes
- **Performance** for performance improvements
- **Documentation** for documentation updates

---

**Last Updated**: 2026-01-09

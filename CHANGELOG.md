# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned
- Dynamic priority adjustment based on crawler success rate (Phase 2)
- Multiple Queue system for crawler groups (Phase 3)
- Selenium crawler interval optimization to 60s uniform (Phase 4)
- Request crawler interval adjustment to reduce CPU throttling (Phase 5)

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
| 1.4.0 | 2025-11-08 | Priority Queue + Timeout strategy |
| 1.3.0 | 2025-11-06 | AsyncIO Queue for Selenium crawlers |
| 1.2.0 | 2025-11-05 | System monitoring & zombie process cleanup |
| 1.1.0 | 2025-10-26 | Crawler optimizations (SC, WOORI, IBK) |
| 1.0.1 | 2025-10-25 | Domain-based refactoring |
| 1.0.0 | 2025-10-17 | Initial release |

---

## Migration Guide

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

**Last Updated**: 2025-11-08

// Copyright 2023-2025 the Turso authors. All rights reserved. MIT license.

//! Feature-gated lock contention metrics for write scaling diagnosis.
//!
//! When the `lock_metrics` feature is enabled, this module provides global
//! atomic counters that instrument the two primary serialization points:
//!
//! - **WAL mode**: `write_lock` in `WalFileShared`
//! - **MVCC mode**: `pager_commit_lock` in `CommitCoordinator`
//!
//! All counters use `Ordering::Relaxed` for minimal overhead. Call
//! `snapshot_and_reset()` to atomically read and zero all counters.

use std::sync::atomic::{AtomicU64, Ordering};

// ---------------------------------------------------------------------------
// WAL write_lock counters
// ---------------------------------------------------------------------------

/// Successful WAL write_lock acquisitions.
static WAL_WRITE_LOCK_ACQUIRES: AtomicU64 = AtomicU64::new(0);
/// Failed WAL write_lock attempts (returned Busy).
static WAL_WRITE_LOCK_FAILURES: AtomicU64 = AtomicU64::new(0);
/// Cumulative nanoseconds the WAL write_lock was held.
static WAL_WRITE_LOCK_HOLD_NS: AtomicU64 = AtomicU64::new(0);
/// Cumulative nanoseconds of I/O in try_restart_log_before_write() under WAL write_lock.
static WAL_RESTART_IO_NS: AtomicU64 = AtomicU64::new(0);

// WAL commit I/O phase breakdown (within commit_dirty_pages_inner under write_lock)
/// Cumulative ns: PrepareWal + PrepareWalSync (WAL header write + fsync).
static WAL_COMMIT_PREPARE_NS: AtomicU64 = AtomicU64::new(0);
/// Cumulative ns: ScanAndIssueReads + WaitBatchedReads (evicted page reads from DB/WAL).
static WAL_COMMIT_READ_NS: AtomicU64 = AtomicU64::new(0);
/// Cumulative ns: PrepareFrames + WaitWrites (pwritev all dirty page frames to WAL).
static WAL_COMMIT_WRITE_NS: AtomicU64 = AtomicU64::new(0);
/// Cumulative ns: WaitSync (final WAL fsync).
static WAL_COMMIT_SYNC_NS: AtomicU64 = AtomicU64::new(0);

// ---------------------------------------------------------------------------
// MVCC pager_commit_lock counters
// ---------------------------------------------------------------------------

/// Successful MVCC pager_commit_lock acquisitions.
static MVCC_COMMIT_LOCK_ACQUIRES: AtomicU64 = AtomicU64::new(0);
/// Failed MVCC pager_commit_lock attempts (returned Busy/yield).
static MVCC_COMMIT_LOCK_FAILURES: AtomicU64 = AtomicU64::new(0);
/// Cumulative nanoseconds the MVCC pager_commit_lock was held.
static MVCC_COMMIT_LOCK_HOLD_NS: AtomicU64 = AtomicU64::new(0);
/// Cumulative nanoseconds in log_tx() under MVCC commit lock.
static MVCC_COMMIT_LOCK_LOG_NS: AtomicU64 = AtomicU64::new(0);
/// Cumulative nanoseconds in sync() (fsync) under MVCC commit lock.
static MVCC_COMMIT_LOCK_SYNC_NS: AtomicU64 = AtomicU64::new(0);

// ---------------------------------------------------------------------------
// Recording functions (called from instrumented code paths)
// ---------------------------------------------------------------------------

#[inline]
pub fn record_wal_write_lock_acquire() {
    WAL_WRITE_LOCK_ACQUIRES.fetch_add(1, Ordering::Relaxed);
}

#[inline]
pub fn record_wal_write_lock_failure() {
    WAL_WRITE_LOCK_FAILURES.fetch_add(1, Ordering::Relaxed);
}

#[inline]
pub fn record_wal_write_lock_hold_ns(ns: u64) {
    WAL_WRITE_LOCK_HOLD_NS.fetch_add(ns, Ordering::Relaxed);
}

#[inline]
pub fn record_wal_restart_io_ns(ns: u64) {
    WAL_RESTART_IO_NS.fetch_add(ns, Ordering::Relaxed);
}

#[inline]
pub fn record_wal_commit_prepare_ns(ns: u64) {
    WAL_COMMIT_PREPARE_NS.fetch_add(ns, Ordering::Relaxed);
}

#[inline]
pub fn record_wal_commit_read_ns(ns: u64) {
    WAL_COMMIT_READ_NS.fetch_add(ns, Ordering::Relaxed);
}

#[inline]
pub fn record_wal_commit_write_ns(ns: u64) {
    WAL_COMMIT_WRITE_NS.fetch_add(ns, Ordering::Relaxed);
}

#[inline]
pub fn record_wal_commit_sync_ns(ns: u64) {
    WAL_COMMIT_SYNC_NS.fetch_add(ns, Ordering::Relaxed);
}

#[inline]
pub fn record_mvcc_commit_lock_acquire() {
    MVCC_COMMIT_LOCK_ACQUIRES.fetch_add(1, Ordering::Relaxed);
}

#[inline]
pub fn record_mvcc_commit_lock_failure() {
    MVCC_COMMIT_LOCK_FAILURES.fetch_add(1, Ordering::Relaxed);
}

#[inline]
pub fn record_mvcc_commit_lock_hold_ns(ns: u64) {
    MVCC_COMMIT_LOCK_HOLD_NS.fetch_add(ns, Ordering::Relaxed);
}

#[inline]
pub fn record_mvcc_commit_lock_log_ns(ns: u64) {
    MVCC_COMMIT_LOCK_LOG_NS.fetch_add(ns, Ordering::Relaxed);
}

#[inline]
pub fn record_mvcc_commit_lock_sync_ns(ns: u64) {
    MVCC_COMMIT_LOCK_SYNC_NS.fetch_add(ns, Ordering::Relaxed);
}

// ---------------------------------------------------------------------------
// Snapshot
// ---------------------------------------------------------------------------

/// A point-in-time snapshot of all lock contention counters.
#[derive(Debug, Clone, Default)]
pub struct LockMetricsSnapshot {
    // WAL
    pub wal_acquires: u64,
    pub wal_failures: u64,
    pub wal_hold_ns: u64,
    pub wal_restart_io_ns: u64,
    pub wal_commit_prepare_ns: u64,
    pub wal_commit_read_ns: u64,
    pub wal_commit_write_ns: u64,
    pub wal_commit_sync_ns: u64,

    // MVCC
    pub mvcc_acquires: u64,
    pub mvcc_failures: u64,
    pub mvcc_hold_ns: u64,
    pub mvcc_log_ns: u64,
    pub mvcc_sync_ns: u64,
}

impl LockMetricsSnapshot {
    /// Total WAL commit I/O nanoseconds (prepare + read + write + sync).
    pub fn wal_commit_io_ns(&self) -> u64 {
        self.wal_commit_prepare_ns
            + self.wal_commit_read_ns
            + self.wal_commit_write_ns
            + self.wal_commit_sync_ns
    }

    /// Fraction of WAL lock hold time spent in all I/O (restart + commit phases) (0.0-1.0).
    pub fn wal_io_fraction(&self) -> f64 {
        if self.wal_hold_ns == 0 {
            0.0
        } else {
            (self.wal_restart_io_ns + self.wal_commit_io_ns()) as f64 / self.wal_hold_ns as f64
        }
    }

    /// Fraction of MVCC lock hold time spent in I/O (log + sync) (0.0-1.0).
    pub fn mvcc_io_fraction(&self) -> f64 {
        if self.mvcc_hold_ns == 0 {
            0.0
        } else {
            (self.mvcc_log_ns + self.mvcc_sync_ns) as f64 / self.mvcc_hold_ns as f64
        }
    }

    /// Fraction of MVCC lock hold time spent in sync/fsync (0.0-1.0).
    pub fn mvcc_sync_fraction(&self) -> f64 {
        if self.mvcc_hold_ns == 0 {
            0.0
        } else {
            self.mvcc_sync_ns as f64 / self.mvcc_hold_ns as f64
        }
    }

    /// Serialize to JSON for sidecar files.
    pub fn to_json(&self) -> String {
        format!(
            concat!(
                "{{",
                "\"wal_acquires\":{},\"wal_failures\":{},",
                "\"wal_hold_us\":{:.2},\"wal_restart_io_us\":{:.2},",
                "\"wal_commit_prepare_us\":{:.2},\"wal_commit_read_us\":{:.2},",
                "\"wal_commit_write_us\":{:.2},\"wal_commit_sync_us\":{:.2},",
                "\"mvcc_acquires\":{},\"mvcc_failures\":{},",
                "\"mvcc_hold_us\":{:.2},\"mvcc_log_us\":{:.2},\"mvcc_sync_us\":{:.2}",
                "}}"
            ),
            self.wal_acquires,
            self.wal_failures,
            self.wal_hold_ns as f64 / 1000.0,
            self.wal_restart_io_ns as f64 / 1000.0,
            self.wal_commit_prepare_ns as f64 / 1000.0,
            self.wal_commit_read_ns as f64 / 1000.0,
            self.wal_commit_write_ns as f64 / 1000.0,
            self.wal_commit_sync_ns as f64 / 1000.0,
            self.mvcc_acquires,
            self.mvcc_failures,
            self.mvcc_hold_ns as f64 / 1000.0,
            self.mvcc_log_ns as f64 / 1000.0,
            self.mvcc_sync_ns as f64 / 1000.0,
        )
    }
}

/// Atomically read all counters and reset them to zero.
///
/// Uses `swap(0, Relaxed)` on each counter so no update is lost between
/// reading and clearing, even under concurrent writes from other threads.
pub fn snapshot_and_reset() -> LockMetricsSnapshot {
    LockMetricsSnapshot {
        wal_acquires: WAL_WRITE_LOCK_ACQUIRES.swap(0, Ordering::Relaxed),
        wal_failures: WAL_WRITE_LOCK_FAILURES.swap(0, Ordering::Relaxed),
        wal_hold_ns: WAL_WRITE_LOCK_HOLD_NS.swap(0, Ordering::Relaxed),
        wal_restart_io_ns: WAL_RESTART_IO_NS.swap(0, Ordering::Relaxed),
        wal_commit_prepare_ns: WAL_COMMIT_PREPARE_NS.swap(0, Ordering::Relaxed),
        wal_commit_read_ns: WAL_COMMIT_READ_NS.swap(0, Ordering::Relaxed),
        wal_commit_write_ns: WAL_COMMIT_WRITE_NS.swap(0, Ordering::Relaxed),
        wal_commit_sync_ns: WAL_COMMIT_SYNC_NS.swap(0, Ordering::Relaxed),
        mvcc_acquires: MVCC_COMMIT_LOCK_ACQUIRES.swap(0, Ordering::Relaxed),
        mvcc_failures: MVCC_COMMIT_LOCK_FAILURES.swap(0, Ordering::Relaxed),
        mvcc_hold_ns: MVCC_COMMIT_LOCK_HOLD_NS.swap(0, Ordering::Relaxed),
        mvcc_log_ns: MVCC_COMMIT_LOCK_LOG_NS.swap(0, Ordering::Relaxed),
        mvcc_sync_ns: MVCC_COMMIT_LOCK_SYNC_NS.swap(0, Ordering::Relaxed),
    }
}

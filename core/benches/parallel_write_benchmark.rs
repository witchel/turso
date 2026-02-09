//! Parallel Write Benchmarks — Contention Diagnostics
//!
//! Measures concurrent write performance with exclusive OS-thread writers,
//! focusing on lock contention, retry overhead, and scaling behavior.
//!
//! Groups:
//! 1. Disjoint Key Scalability — baseline, no data contention
//! 2. Hot Row Contention — maximum write-write conflicts
//! 3. Overlapping Key Insert — partial contention (50% key overlap)
//! 4. Single Row Increment — theoretical worst case
//! 5. Writer-Heavy Mixed Workload — reader starvation test
//!
//! Run with: cargo bench --bench parallel_write_benchmark

#[cfg(not(feature = "codspeed"))]
use criterion::{BenchmarkId, Criterion, Throughput, criterion_group, criterion_main};
#[cfg(not(feature = "codspeed"))]
use pprof::criterion::{Output, PProfProfiler};

#[cfg(feature = "codspeed")]
use codspeed_criterion_compat::{
    BenchmarkId, Criterion, Throughput, criterion_group, criterion_main,
};

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Barrier};
use std::time::{Duration, Instant};
use tempfile::TempDir;
use turso_core::{Database, PlatformIO};

#[cfg(not(target_family = "wasm"))]
#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

// Thread counts: 7 points capped at 12 CPUs
const WRITER_COUNTS: &[usize] = &[1, 2, 4, 6, 8, 10, 12];

// Workload constants
const NUM_BATCHES: usize = 10;
const ROWS_PER_BATCH: usize = 50;
const HOT_ROWS: usize = 100;
const OPS_PER_WRITER: usize = 100;

// ---------------------------------------------------------------------------
// Database setup helpers
// ---------------------------------------------------------------------------

fn setup_turso_wal(temp_dir: &TempDir, schema: &str) -> Arc<Database> {
    let db_path = temp_dir.path().join("bench.db");
    #[allow(clippy::arc_with_non_send_sync)]
    let io = Arc::new(PlatformIO::new().unwrap());
    let db = Database::open_file(io, db_path.to_str().unwrap()).unwrap();
    let conn = db.connect().unwrap();
    conn.execute(schema).unwrap();
    db
}

fn setup_turso_mvcc(temp_dir: &TempDir, schema: &str) -> Arc<Database> {
    let db_path = temp_dir.path().join("bench.db");
    #[allow(clippy::arc_with_non_send_sync)]
    let io = Arc::new(PlatformIO::new().unwrap());
    let db = Database::open_file(io, db_path.to_str().unwrap()).unwrap();
    let conn = db.connect().unwrap();
    conn.execute("PRAGMA journal_mode = 'experimental_mvcc'")
        .unwrap();
    conn.execute(schema).unwrap();
    db
}

fn setup_rusqlite(temp_dir: &TempDir, schema: &str) -> rusqlite::Connection {
    let db_path = temp_dir.path().join("bench.db");
    let conn = rusqlite::Connection::open(&db_path).unwrap();
    conn.pragma_update(None, "synchronous", "FULL").unwrap();
    conn.pragma_update(None, "journal_mode", "WAL").unwrap();
    conn.execute_batch(schema).unwrap();
    conn
}

fn rusqlite_enabled() -> bool {
    std::env::var("DISABLE_RUSQLITE_BENCHMARK").is_err() && !cfg!(feature = "codspeed")
}

// ---------------------------------------------------------------------------
// SQL generation helpers
// ---------------------------------------------------------------------------

fn generate_batch_insert(table: &str, start: i64, count: usize) -> String {
    let mut sql = format!("INSERT INTO {table} (id, data) VALUES ");
    for i in 0..count {
        if i > 0 {
            sql.push(',');
        }
        let id = start + i as i64;
        sql.push_str(&format!("({id}, 'v{id}')"));
    }
    sql
}

fn generate_batch_insert_or_replace(table: &str, start: i64, count: usize) -> String {
    let mut sql = format!("INSERT OR REPLACE INTO {table} (id, data) VALUES ");
    for i in 0..count {
        if i > 0 {
            sql.push(',');
        }
        let id = start + i as i64;
        sql.push_str(&format!("({id}, 'v{id}')"));
    }
    sql
}

fn generate_disjoint_inserts(table: &str, num_writers: usize) -> Vec<Vec<String>> {
    let mut all = Vec::with_capacity(num_writers);
    for w in 0..num_writers {
        let mut batches = Vec::with_capacity(NUM_BATCHES);
        for b in 0..NUM_BATCHES {
            let start = (w * NUM_BATCHES * ROWS_PER_BATCH + b * ROWS_PER_BATCH) as i64;
            batches.push(generate_batch_insert(table, start, ROWS_PER_BATCH));
        }
        all.push(batches);
    }
    all
}

fn generate_hot_row_updates(num_writers: usize) -> Vec<Vec<String>> {
    let mut all = Vec::with_capacity(num_writers);
    for w in 0..num_writers {
        let mut stmts = Vec::with_capacity(NUM_BATCHES * 10);
        for b in 0..NUM_BATCHES {
            for i in 0..10 {
                let row_id = (w * 10 + b * 10 + i) % HOT_ROWS;
                stmts.push(format!(
                    "UPDATE test SET data = 'w{w}_b{b}_i{i}' WHERE id = {row_id}"
                ));
            }
        }
        all.push(stmts);
    }
    all
}

fn generate_overlapping_inserts(num_writers: usize) -> Vec<Vec<String>> {
    let range_per_writer = NUM_BATCHES * ROWS_PER_BATCH; // 500
    let mut all = Vec::with_capacity(num_writers);
    for w in 0..num_writers {
        let base = (w as i64) * (range_per_writer as i64 / 2); // 50% overlap
        let mut batches = Vec::with_capacity(NUM_BATCHES);
        for b in 0..NUM_BATCHES {
            let start = base + (b * ROWS_PER_BATCH) as i64;
            batches.push(generate_batch_insert_or_replace(
                "test",
                start,
                ROWS_PER_BATCH,
            ));
        }
        all.push(batches);
    }
    all
}

// ---------------------------------------------------------------------------
// Retry-counting execute helpers
// ---------------------------------------------------------------------------

fn execute_with_retry_counted(conn: &Arc<turso_core::Connection>, sql: &str, retries: &AtomicU64) {
    loop {
        match conn.execute(sql) {
            Ok(()) => break,
            Err(turso_core::LimboError::Busy) => {
                retries.fetch_add(1, Ordering::Relaxed);
                std::thread::yield_now();
            }
            Err(e) => panic!("execute error: {e:?}"),
        }
    }
}

fn execute_mvcc_with_retry_counted(
    conn: &Arc<turso_core::Connection>,
    sql: &str,
    retries: &AtomicU64,
) {
    loop {
        conn.execute("BEGIN CONCURRENT").unwrap();
        match conn.execute(sql) {
            Ok(()) => {}
            Err(turso_core::LimboError::WriteWriteConflict) | Err(turso_core::LimboError::Busy) => {
                retries.fetch_add(1, Ordering::Relaxed);
                let _ = conn.execute("ROLLBACK");
                std::thread::yield_now();
                continue;
            }
            Err(e) => panic!("execute error during statement: {e:?}"),
        }
        match conn.execute("COMMIT") {
            Ok(()) => break,
            Err(turso_core::LimboError::WriteWriteConflict) | Err(turso_core::LimboError::Busy) => {
                retries.fetch_add(1, Ordering::Relaxed);
                let _ = conn.execute("ROLLBACK");
                std::thread::yield_now();
                continue;
            }
            Err(e) => panic!("execute error during COMMIT: {e:?}"),
        }
    }
}

fn execute_rusqlite_with_retry(conn: &rusqlite::Connection, sql: &str, retries: &AtomicU64) {
    loop {
        match conn.execute(sql, []) {
            Ok(_) => break,
            Err(rusqlite::Error::SqliteFailure(e, _))
                if e.code == rusqlite::ffi::ErrorCode::DatabaseBusy
                    || e.code == rusqlite::ffi::ErrorCode::DatabaseLocked =>
            {
                retries.fetch_add(1, Ordering::Relaxed);
                std::thread::yield_now();
            }
            Err(e) => panic!("rusqlite execute error: {e:?}"),
        }
    }
}

// ---------------------------------------------------------------------------
// Retry sidecar writer
// ---------------------------------------------------------------------------

fn write_retry_sidecar(group: &str, variant: &str, param: usize, count: u64) {
    // Write outside Criterion's directory to avoid being overwritten when
    // Criterion recreates the `new/` subdirectory for its own results.
    // The benchmark binary runs from the package dir (core/), but Criterion
    // writes to the workspace target/, so we resolve relative to CARGO_MANIFEST_DIR's
    // parent to match.
    let base = std::env::var("CARGO_MANIFEST_DIR")
        .map(|d| {
            std::path::PathBuf::from(d)
                .parent()
                .unwrap()
                .join("target/bench_retries")
        })
        .unwrap_or_else(|_| std::path::PathBuf::from("target/bench_retries"));
    let dir = base.join(group).join(variant);
    std::fs::create_dir_all(&dir).ok();
    let path = dir.join(format!("{param}.json"));
    let json = format!("{{\"retries\": {count}}}");
    std::fs::write(path, json).ok();
}

// ---------------------------------------------------------------------------
// Latency stats helpers
// ---------------------------------------------------------------------------

fn compute_latency_stats(latencies: &[Duration]) -> (f64, f64) {
    if latencies.is_empty() {
        return (0.0, 0.0);
    }
    let mut us: Vec<f64> = latencies.iter().map(|d| d.as_secs_f64() * 1e6).collect();
    let avg = us.iter().sum::<f64>() / us.len() as f64;
    us.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let p99_idx = ((us.len() as f64 * 0.99) as usize).min(us.len() - 1);
    let p99 = us[p99_idx];
    (avg, p99)
}

fn write_latency_sidecar(
    group: &str,
    variant: &str,
    param: usize,
    write_avg: f64,
    write_p99: f64,
    read_avg: f64,
    read_p99: f64,
) {
    let base = std::env::var("CARGO_MANIFEST_DIR")
        .map(|d| {
            std::path::PathBuf::from(d)
                .parent()
                .unwrap()
                .join("target/bench_retries")
        })
        .unwrap_or_else(|_| std::path::PathBuf::from("target/bench_retries"));
    let dir = base.join(group).join(variant);
    std::fs::create_dir_all(&dir).ok();
    let path = dir.join(format!("{param}_latency.json"));
    let json = format!(
        "{{\"write_avg_us\": {write_avg:.2}, \"write_p99_us\": {write_p99:.2}, \"read_avg_us\": {read_avg:.2}, \"read_p99_us\": {read_p99:.2}}}"
    );
    std::fs::write(path, json).ok();
}

// ---------------------------------------------------------------------------
// Pre-population helper
// ---------------------------------------------------------------------------

fn prepopulate_rows(conn: &Arc<turso_core::Connection>, count: usize) {
    let mut sql = String::from("INSERT INTO test (id, data) VALUES ");
    for i in 0..count {
        if i > 0 {
            sql.push(',');
        }
        sql.push_str(&format!("({i}, 'init_{i}')"));
    }
    conn.execute(&sql).unwrap();
}

fn prepopulate_counter(conn: &Arc<turso_core::Connection>) {
    conn.execute("INSERT INTO test (id, counter) VALUES (1, 0)")
        .unwrap();
}

fn prepopulate_rusqlite(conn: &rusqlite::Connection, count: usize) {
    let mut sql = String::from("INSERT INTO test (id, data) VALUES ");
    for i in 0..count {
        if i > 0 {
            sql.push(',');
        }
        sql.push_str(&format!("({i}, 'init_{i}')"));
    }
    conn.execute_batch(&sql).unwrap();
}

fn prepopulate_counter_rusqlite(conn: &rusqlite::Connection) {
    conn.execute("INSERT INTO test (id, counter) VALUES (1, 0)", [])
        .unwrap();
}

// ---------------------------------------------------------------------------
// Benchmark 1: Disjoint Key Scalability
// ---------------------------------------------------------------------------

fn bench_disjoint_scalability(criterion: &mut Criterion) {
    let enable_rusqlite = rusqlite_enabled();

    let mut group = criterion.benchmark_group("Disjoint Key Scalability");

    for &writers in WRITER_COUNTS {
        let total_rows = (writers * NUM_BATCHES * ROWS_PER_BATCH) as u64;
        group.throughput(Throughput::Elements(total_rows));

        // --- turso_wal ---
        group.bench_function(BenchmarkId::new("turso_wal", writers), |b| {
            b.iter_custom(|iters| {
                let mut total = Duration::ZERO;
                for _ in 0..iters {
                    let temp_dir = tempfile::tempdir().unwrap();
                    // WAL uses non-PK table to avoid UNIQUE failures on Busy retry
                    let db =
                        setup_turso_wal(&temp_dir, "CREATE TABLE test (id INTEGER, data TEXT)");
                    let all_inserts = generate_disjoint_inserts("test", writers);
                    let retries = Arc::new(AtomicU64::new(0));

                    let barrier = Arc::new(Barrier::new(writers));
                    let mut handles = Vec::with_capacity(writers);

                    for inserts in all_inserts {
                        let db = db.clone();
                        let barrier = barrier.clone();
                        let retries = retries.clone();
                        handles.push(std::thread::spawn(move || {
                            let conn = db.connect().unwrap();
                            barrier.wait();
                            for sql in &inserts {
                                execute_with_retry_counted(&conn, sql, &retries);
                            }
                        }));
                    }

                    let start = Instant::now();
                    for h in handles {
                        h.join().unwrap();
                    }
                    total += start.elapsed();

                    write_retry_sidecar(
                        "Disjoint Key Scalability",
                        "turso_wal",
                        writers,
                        retries.load(Ordering::Relaxed),
                    );
                }
                total
            });
        });

        // --- turso_mvcc ---
        group.bench_function(BenchmarkId::new("turso_mvcc", writers), |b| {
            b.iter_custom(|iters| {
                let mut total = Duration::ZERO;
                for _ in 0..iters {
                    let temp_dir = tempfile::tempdir().unwrap();
                    let db = setup_turso_mvcc(
                        &temp_dir,
                        "CREATE TABLE test (id INTEGER PRIMARY KEY, data TEXT)",
                    );
                    let all_inserts = generate_disjoint_inserts("test", writers);
                    let retries = Arc::new(AtomicU64::new(0));

                    let barrier = Arc::new(Barrier::new(writers));
                    let mut handles = Vec::with_capacity(writers);

                    for inserts in all_inserts {
                        let db = db.clone();
                        let barrier = barrier.clone();
                        let retries = retries.clone();
                        handles.push(std::thread::spawn(move || {
                            let conn = db.connect().unwrap();
                            barrier.wait();
                            for sql in &inserts {
                                execute_mvcc_with_retry_counted(&conn, sql, &retries);
                            }
                        }));
                    }

                    let start = Instant::now();
                    for h in handles {
                        h.join().unwrap();
                    }
                    total += start.elapsed();

                    write_retry_sidecar(
                        "Disjoint Key Scalability",
                        "turso_mvcc",
                        writers,
                        retries.load(Ordering::Relaxed),
                    );
                }
                total
            });
        });

        // --- sqlite_wal ---
        if enable_rusqlite {
            group.bench_function(BenchmarkId::new("sqlite_wal", writers), |b| {
                b.iter_custom(|iters| {
                    let mut total = Duration::ZERO;
                    for _ in 0..iters {
                        let temp_dir = tempfile::tempdir().unwrap();
                        let conn = setup_rusqlite(
                            &temp_dir,
                            "CREATE TABLE test (id INTEGER PRIMARY KEY, data TEXT)",
                        );
                        drop(conn);

                        let db_path = temp_dir.path().join("bench.db");
                        let all_inserts = generate_disjoint_inserts("test", writers);
                        let retries = Arc::new(AtomicU64::new(0));

                        let barrier = Arc::new(Barrier::new(writers));
                        let mut handles = Vec::with_capacity(writers);

                        for inserts in all_inserts {
                            let db_path = db_path.clone();
                            let barrier = barrier.clone();
                            let retries = retries.clone();
                            handles.push(std::thread::spawn(move || {
                                let conn = rusqlite::Connection::open(&db_path).unwrap();
                                conn.busy_timeout(std::time::Duration::from_secs(10))
                                    .unwrap();
                                barrier.wait();
                                for sql in &inserts {
                                    execute_rusqlite_with_retry(&conn, sql, &retries);
                                }
                            }));
                        }

                        let start = Instant::now();
                        for h in handles {
                            h.join().unwrap();
                        }
                        total += start.elapsed();

                        write_retry_sidecar(
                            "Disjoint Key Scalability",
                            "sqlite_wal",
                            writers,
                            retries.load(Ordering::Relaxed),
                        );
                    }
                    total
                });
            });
        }
    }

    group.finish();
}

// ---------------------------------------------------------------------------
// Benchmark 2: Hot Row Contention
// ---------------------------------------------------------------------------

fn bench_hot_row_contention(criterion: &mut Criterion) {
    let enable_rusqlite = rusqlite_enabled();

    let mut group = criterion.benchmark_group("Hot Row Contention");

    for &writers in WRITER_COUNTS {
        // Each writer does 10 batches * 10 updates = 100 operations
        let total_ops = (writers * NUM_BATCHES * 10) as u64;
        group.throughput(Throughput::Elements(total_ops));

        let all_updates = generate_hot_row_updates(writers);

        // --- turso_wal ---
        group.bench_function(BenchmarkId::new("turso_wal", writers), |b| {
            let all_updates = all_updates.clone();
            b.iter_custom(|iters| {
                let mut total = Duration::ZERO;
                for _ in 0..iters {
                    let temp_dir = tempfile::tempdir().unwrap();
                    let db = setup_turso_wal(
                        &temp_dir,
                        "CREATE TABLE test (id INTEGER PRIMARY KEY, data TEXT)",
                    );
                    let conn = db.connect().unwrap();
                    prepopulate_rows(&conn, HOT_ROWS);
                    let retries = Arc::new(AtomicU64::new(0));

                    let barrier = Arc::new(Barrier::new(writers));
                    let mut handles = Vec::with_capacity(writers);

                    for updates in all_updates.clone() {
                        let db = db.clone();
                        let barrier = barrier.clone();
                        let retries = retries.clone();
                        handles.push(std::thread::spawn(move || {
                            let conn = db.connect().unwrap();
                            barrier.wait();
                            for sql in &updates {
                                execute_with_retry_counted(&conn, sql, &retries);
                            }
                        }));
                    }

                    let start = Instant::now();
                    for h in handles {
                        h.join().unwrap();
                    }
                    total += start.elapsed();

                    write_retry_sidecar(
                        "Hot Row Contention",
                        "turso_wal",
                        writers,
                        retries.load(Ordering::Relaxed),
                    );
                }
                total
            });
        });

        // --- turso_mvcc (each UPDATE in its own transaction) ---
        group.bench_function(BenchmarkId::new("turso_mvcc", writers), |b| {
            let all_updates = all_updates.clone();
            b.iter_custom(|iters| {
                let mut total = Duration::ZERO;
                for _ in 0..iters {
                    let temp_dir = tempfile::tempdir().unwrap();
                    let db = setup_turso_mvcc(
                        &temp_dir,
                        "CREATE TABLE test (id INTEGER PRIMARY KEY, data TEXT)",
                    );
                    let conn = db.connect().unwrap();
                    prepopulate_rows(&conn, HOT_ROWS);
                    let retries = Arc::new(AtomicU64::new(0));

                    let barrier = Arc::new(Barrier::new(writers));
                    let mut handles = Vec::with_capacity(writers);

                    for updates in all_updates.clone() {
                        let db = db.clone();
                        let barrier = barrier.clone();
                        let retries = retries.clone();
                        handles.push(std::thread::spawn(move || {
                            let conn = db.connect().unwrap();
                            barrier.wait();
                            for sql in &updates {
                                execute_mvcc_with_retry_counted(&conn, sql, &retries);
                            }
                        }));
                    }

                    let start = Instant::now();
                    for h in handles {
                        h.join().unwrap();
                    }
                    total += start.elapsed();

                    write_retry_sidecar(
                        "Hot Row Contention",
                        "turso_mvcc",
                        writers,
                        retries.load(Ordering::Relaxed),
                    );
                }
                total
            });
        });

        // --- sqlite_wal ---
        if enable_rusqlite {
            group.bench_function(BenchmarkId::new("sqlite_wal", writers), |b| {
                let all_updates = all_updates.clone();
                b.iter_custom(|iters| {
                    let mut total = Duration::ZERO;
                    for _ in 0..iters {
                        let temp_dir = tempfile::tempdir().unwrap();
                        let conn = setup_rusqlite(
                            &temp_dir,
                            "CREATE TABLE test (id INTEGER PRIMARY KEY, data TEXT)",
                        );
                        prepopulate_rusqlite(&conn, HOT_ROWS);
                        drop(conn);

                        let db_path = temp_dir.path().join("bench.db");
                        let retries = Arc::new(AtomicU64::new(0));

                        let barrier = Arc::new(Barrier::new(writers));
                        let mut handles = Vec::with_capacity(writers);

                        for updates in all_updates.clone() {
                            let db_path = db_path.clone();
                            let barrier = barrier.clone();
                            let retries = retries.clone();
                            handles.push(std::thread::spawn(move || {
                                let conn = rusqlite::Connection::open(&db_path).unwrap();
                                conn.busy_timeout(std::time::Duration::from_secs(10))
                                    .unwrap();
                                barrier.wait();
                                for sql in &updates {
                                    execute_rusqlite_with_retry(&conn, sql, &retries);
                                }
                            }));
                        }

                        let start = Instant::now();
                        for h in handles {
                            h.join().unwrap();
                        }
                        total += start.elapsed();

                        write_retry_sidecar(
                            "Hot Row Contention",
                            "sqlite_wal",
                            writers,
                            retries.load(Ordering::Relaxed),
                        );
                    }
                    total
                });
            });
        }
    }

    group.finish();
}

// ---------------------------------------------------------------------------
// Benchmark 3: Overlapping Key Insert
// ---------------------------------------------------------------------------

fn bench_overlapping_insert(criterion: &mut Criterion) {
    let enable_rusqlite = rusqlite_enabled();

    let mut group = criterion.benchmark_group("Overlapping Key Insert");

    for &writers in WRITER_COUNTS {
        let total_rows = (writers * NUM_BATCHES * ROWS_PER_BATCH) as u64;
        group.throughput(Throughput::Elements(total_rows));

        let all_inserts = generate_overlapping_inserts(writers);

        // --- turso_wal ---
        group.bench_function(BenchmarkId::new("turso_wal", writers), |b| {
            let all_inserts = all_inserts.clone();
            b.iter_custom(|iters| {
                let mut total = Duration::ZERO;
                for _ in 0..iters {
                    let temp_dir = tempfile::tempdir().unwrap();
                    // WAL uses non-PK table for INSERT OR REPLACE compatibility
                    let db =
                        setup_turso_wal(&temp_dir, "CREATE TABLE test (id INTEGER, data TEXT)");
                    let retries = Arc::new(AtomicU64::new(0));

                    let barrier = Arc::new(Barrier::new(writers));
                    let mut handles = Vec::with_capacity(writers);

                    for inserts in all_inserts.clone() {
                        let db = db.clone();
                        let barrier = barrier.clone();
                        let retries = retries.clone();
                        handles.push(std::thread::spawn(move || {
                            let conn = db.connect().unwrap();
                            barrier.wait();
                            for sql in &inserts {
                                execute_with_retry_counted(&conn, sql, &retries);
                            }
                        }));
                    }

                    let start = Instant::now();
                    for h in handles {
                        h.join().unwrap();
                    }
                    total += start.elapsed();

                    write_retry_sidecar(
                        "Overlapping Key Insert",
                        "turso_wal",
                        writers,
                        retries.load(Ordering::Relaxed),
                    );
                }
                total
            });
        });

        // --- turso_mvcc ---
        group.bench_function(BenchmarkId::new("turso_mvcc", writers), |b| {
            let all_inserts = all_inserts.clone();
            b.iter_custom(|iters| {
                let mut total = Duration::ZERO;
                for _ in 0..iters {
                    let temp_dir = tempfile::tempdir().unwrap();
                    let db = setup_turso_mvcc(
                        &temp_dir,
                        "CREATE TABLE test (id INTEGER PRIMARY KEY, data TEXT)",
                    );
                    let retries = Arc::new(AtomicU64::new(0));

                    let barrier = Arc::new(Barrier::new(writers));
                    let mut handles = Vec::with_capacity(writers);

                    for inserts in all_inserts.clone() {
                        let db = db.clone();
                        let barrier = barrier.clone();
                        let retries = retries.clone();
                        handles.push(std::thread::spawn(move || {
                            let conn = db.connect().unwrap();
                            barrier.wait();
                            for sql in &inserts {
                                execute_mvcc_with_retry_counted(&conn, sql, &retries);
                            }
                        }));
                    }

                    let start = Instant::now();
                    for h in handles {
                        h.join().unwrap();
                    }
                    total += start.elapsed();

                    write_retry_sidecar(
                        "Overlapping Key Insert",
                        "turso_mvcc",
                        writers,
                        retries.load(Ordering::Relaxed),
                    );
                }
                total
            });
        });

        // --- sqlite_wal ---
        if enable_rusqlite {
            group.bench_function(BenchmarkId::new("sqlite_wal", writers), |b| {
                let all_inserts = all_inserts.clone();
                b.iter_custom(|iters| {
                    let mut total = Duration::ZERO;
                    for _ in 0..iters {
                        let temp_dir = tempfile::tempdir().unwrap();
                        let conn = setup_rusqlite(
                            &temp_dir,
                            "CREATE TABLE test (id INTEGER PRIMARY KEY, data TEXT)",
                        );
                        drop(conn);

                        let db_path = temp_dir.path().join("bench.db");
                        let retries = Arc::new(AtomicU64::new(0));

                        let barrier = Arc::new(Barrier::new(writers));
                        let mut handles = Vec::with_capacity(writers);

                        for inserts in all_inserts.clone() {
                            let db_path = db_path.clone();
                            let barrier = barrier.clone();
                            let retries = retries.clone();
                            handles.push(std::thread::spawn(move || {
                                let conn = rusqlite::Connection::open(&db_path).unwrap();
                                conn.busy_timeout(std::time::Duration::from_secs(10))
                                    .unwrap();
                                barrier.wait();
                                for sql in &inserts {
                                    execute_rusqlite_with_retry(&conn, sql, &retries);
                                }
                            }));
                        }

                        let start = Instant::now();
                        for h in handles {
                            h.join().unwrap();
                        }
                        total += start.elapsed();

                        write_retry_sidecar(
                            "Overlapping Key Insert",
                            "sqlite_wal",
                            writers,
                            retries.load(Ordering::Relaxed),
                        );
                    }
                    total
                });
            });
        }
    }

    group.finish();
}

// ---------------------------------------------------------------------------
// Benchmark 4: Single Row Increment
// ---------------------------------------------------------------------------

fn bench_single_row_increment(criterion: &mut Criterion) {
    let enable_rusqlite = rusqlite_enabled();

    let mut group = criterion.benchmark_group("Single Row Increment");

    for &writers in WRITER_COUNTS {
        let total_ops = (writers * OPS_PER_WRITER) as u64;
        group.throughput(Throughput::Elements(total_ops));

        let update_sql = "UPDATE test SET counter = counter + 1 WHERE id = 1";

        // --- turso_wal ---
        group.bench_function(BenchmarkId::new("turso_wal", writers), |b| {
            b.iter_custom(|iters| {
                let mut total = Duration::ZERO;
                for _ in 0..iters {
                    let temp_dir = tempfile::tempdir().unwrap();
                    let db = setup_turso_wal(
                        &temp_dir,
                        "CREATE TABLE test (id INTEGER PRIMARY KEY, counter INTEGER)",
                    );
                    let conn = db.connect().unwrap();
                    prepopulate_counter(&conn);
                    let retries = Arc::new(AtomicU64::new(0));

                    let barrier = Arc::new(Barrier::new(writers));
                    let mut handles = Vec::with_capacity(writers);

                    for _ in 0..writers {
                        let db = db.clone();
                        let barrier = barrier.clone();
                        let retries = retries.clone();
                        handles.push(std::thread::spawn(move || {
                            let conn = db.connect().unwrap();
                            barrier.wait();
                            for _ in 0..OPS_PER_WRITER {
                                execute_with_retry_counted(&conn, update_sql, &retries);
                            }
                        }));
                    }

                    let start = Instant::now();
                    for h in handles {
                        h.join().unwrap();
                    }
                    total += start.elapsed();

                    write_retry_sidecar(
                        "Single Row Increment",
                        "turso_wal",
                        writers,
                        retries.load(Ordering::Relaxed),
                    );
                }
                total
            });
        });

        // --- turso_mvcc (each increment in its own transaction) ---
        group.bench_function(BenchmarkId::new("turso_mvcc", writers), |b| {
            b.iter_custom(|iters| {
                let mut total = Duration::ZERO;
                for _ in 0..iters {
                    let temp_dir = tempfile::tempdir().unwrap();
                    let db = setup_turso_mvcc(
                        &temp_dir,
                        "CREATE TABLE test (id INTEGER PRIMARY KEY, counter INTEGER)",
                    );
                    let conn = db.connect().unwrap();
                    prepopulate_counter(&conn);
                    let retries = Arc::new(AtomicU64::new(0));

                    let barrier = Arc::new(Barrier::new(writers));
                    let mut handles = Vec::with_capacity(writers);

                    for _ in 0..writers {
                        let db = db.clone();
                        let barrier = barrier.clone();
                        let retries = retries.clone();
                        handles.push(std::thread::spawn(move || {
                            let conn = db.connect().unwrap();
                            barrier.wait();
                            for _ in 0..OPS_PER_WRITER {
                                execute_mvcc_with_retry_counted(&conn, update_sql, &retries);
                            }
                        }));
                    }

                    let start = Instant::now();
                    for h in handles {
                        h.join().unwrap();
                    }
                    total += start.elapsed();

                    write_retry_sidecar(
                        "Single Row Increment",
                        "turso_mvcc",
                        writers,
                        retries.load(Ordering::Relaxed),
                    );
                }
                total
            });
        });

        // --- sqlite_wal ---
        if enable_rusqlite {
            group.bench_function(BenchmarkId::new("sqlite_wal", writers), |b| {
                b.iter_custom(|iters| {
                    let mut total = Duration::ZERO;
                    for _ in 0..iters {
                        let temp_dir = tempfile::tempdir().unwrap();
                        let conn = setup_rusqlite(
                            &temp_dir,
                            "CREATE TABLE test (id INTEGER PRIMARY KEY, counter INTEGER)",
                        );
                        prepopulate_counter_rusqlite(&conn);
                        drop(conn);

                        let db_path = temp_dir.path().join("bench.db");
                        let retries = Arc::new(AtomicU64::new(0));

                        let barrier = Arc::new(Barrier::new(writers));
                        let mut handles = Vec::with_capacity(writers);

                        for _ in 0..writers {
                            let db_path = db_path.clone();
                            let barrier = barrier.clone();
                            let retries = retries.clone();
                            handles.push(std::thread::spawn(move || {
                                let conn = rusqlite::Connection::open(&db_path).unwrap();
                                conn.busy_timeout(std::time::Duration::from_secs(10))
                                    .unwrap();
                                barrier.wait();
                                for _ in 0..OPS_PER_WRITER {
                                    execute_rusqlite_with_retry(&conn, update_sql, &retries);
                                }
                            }));
                        }

                        let start = Instant::now();
                        for h in handles {
                            h.join().unwrap();
                        }
                        total += start.elapsed();

                        write_retry_sidecar(
                            "Single Row Increment",
                            "sqlite_wal",
                            writers,
                            retries.load(Ordering::Relaxed),
                        );
                    }
                    total
                });
            });
        }
    }

    group.finish();
}

// ---------------------------------------------------------------------------
// Benchmark 5: Writer-Heavy Mixed Workload
// ---------------------------------------------------------------------------

fn bench_writer_heavy_mixed(criterion: &mut Criterion) {
    let enable_rusqlite = rusqlite_enabled();

    let num_readers = 2;
    let prepop_rows = 1_000;
    let hot_range = 200;
    let writer_batches = 10;
    let updates_per_batch = 10;
    let reads_per_reader = 100;
    let writer_counts: &[usize] = &[4, 6, 8, 10, 12];

    let mut group = criterion.benchmark_group("Writer Heavy Mixed");

    for &writers in writer_counts {
        let total_ops =
            (writers * writer_batches * updates_per_batch + num_readers * reads_per_reader) as u64;
        group.throughput(Throughput::Elements(total_ops));

        // Generate writer updates (each targets hot rows [0, hot_range))
        let gen_writer_updates = |num_writers: usize| -> Vec<Vec<String>> {
            let mut all = Vec::with_capacity(num_writers);
            for w in 0..num_writers {
                let mut stmts = Vec::with_capacity(writer_batches * updates_per_batch);
                for b in 0..writer_batches {
                    for i in 0..updates_per_batch {
                        let row_id =
                            (w * updates_per_batch + b * updates_per_batch + i) % hot_range;
                        stmts.push(format!(
                            "UPDATE test SET data = 'w{w}_{b}_{i}' WHERE id = {row_id}"
                        ));
                    }
                }
                all.push(stmts);
            }
            all
        };

        // Generate reader lookups
        let gen_reader_selects = |seed: usize| -> Vec<String> {
            (0..reads_per_reader)
                .map(|i| {
                    let row_id = (i + seed * 37) % hot_range;
                    format!("SELECT * FROM test WHERE id = {row_id}")
                })
                .collect()
        };

        // --- turso_mvcc ---
        group.bench_function(BenchmarkId::new("turso_mvcc", writers), |b| {
            b.iter_custom(|iters| {
                let mut total = Duration::ZERO;
                for _ in 0..iters {
                    let temp_dir = tempfile::tempdir().unwrap();
                    let db = setup_turso_mvcc(
                        &temp_dir,
                        "CREATE TABLE test (id INTEGER PRIMARY KEY, data TEXT)",
                    );
                    let conn = db.connect().unwrap();
                    prepopulate_rows(&conn, prepop_rows);

                    let all_participants = writers + num_readers;
                    let barrier = Arc::new(Barrier::new(all_participants));
                    let retries = Arc::new(AtomicU64::new(0));
                    let mut writer_handles = Vec::with_capacity(writers);
                    let mut reader_handles = Vec::with_capacity(num_readers);

                    let writer_updates = gen_writer_updates(writers);
                    for updates in writer_updates {
                        let db = db.clone();
                        let barrier = barrier.clone();
                        let retries = retries.clone();
                        writer_handles.push(std::thread::spawn(move || -> Vec<Duration> {
                            let conn = db.connect().unwrap();
                            barrier.wait();
                            let mut latencies = Vec::with_capacity(updates.len());
                            for sql in &updates {
                                let t = Instant::now();
                                execute_mvcc_with_retry_counted(&conn, sql, &retries);
                                latencies.push(t.elapsed());
                            }
                            latencies
                        }));
                    }

                    for r in 0..num_readers {
                        let db = db.clone();
                        let barrier = barrier.clone();
                        let selects = gen_reader_selects(r);
                        reader_handles.push(std::thread::spawn(move || -> Vec<Duration> {
                            let conn = db.connect().unwrap();
                            barrier.wait();
                            let mut latencies = Vec::with_capacity(selects.len());
                            for sql in &selects {
                                let t = Instant::now();
                                loop {
                                    match conn.execute(sql) {
                                        Ok(()) => break,
                                        Err(turso_core::LimboError::Busy) => {
                                            std::thread::yield_now();
                                        }
                                        Err(e) => panic!("reader error: {e:?}"),
                                    }
                                }
                                latencies.push(t.elapsed());
                            }
                            latencies
                        }));
                    }

                    let start = Instant::now();
                    let mut write_lats = Vec::new();
                    for h in writer_handles {
                        write_lats.extend(h.join().unwrap());
                    }
                    let mut read_lats = Vec::new();
                    for h in reader_handles {
                        read_lats.extend(h.join().unwrap());
                    }
                    total += start.elapsed();

                    write_retry_sidecar(
                        "Writer Heavy Mixed",
                        "turso_mvcc",
                        writers,
                        retries.load(Ordering::Relaxed),
                    );
                    let (wavg, wp99) = compute_latency_stats(&write_lats);
                    let (ravg, rp99) = compute_latency_stats(&read_lats);
                    write_latency_sidecar(
                        "Writer Heavy Mixed",
                        "turso_mvcc",
                        writers,
                        wavg,
                        wp99,
                        ravg,
                        rp99,
                    );
                }
                total
            });
        });

        // --- turso_wal ---
        group.bench_function(BenchmarkId::new("turso_wal", writers), |b| {
            b.iter_custom(|iters| {
                let mut total = Duration::ZERO;
                for _ in 0..iters {
                    let temp_dir = tempfile::tempdir().unwrap();
                    let db = setup_turso_wal(
                        &temp_dir,
                        "CREATE TABLE test (id INTEGER PRIMARY KEY, data TEXT)",
                    );
                    let conn = db.connect().unwrap();
                    prepopulate_rows(&conn, prepop_rows);

                    let all_participants = writers + num_readers;
                    let barrier = Arc::new(Barrier::new(all_participants));
                    let retries = Arc::new(AtomicU64::new(0));
                    let mut writer_handles = Vec::with_capacity(writers);
                    let mut reader_handles = Vec::with_capacity(num_readers);

                    let writer_updates = gen_writer_updates(writers);
                    for updates in writer_updates {
                        let db = db.clone();
                        let barrier = barrier.clone();
                        let retries = retries.clone();
                        writer_handles.push(std::thread::spawn(move || -> Vec<Duration> {
                            let conn = db.connect().unwrap();
                            barrier.wait();
                            let mut latencies = Vec::with_capacity(updates.len());
                            for sql in &updates {
                                let t = Instant::now();
                                execute_with_retry_counted(&conn, sql, &retries);
                                latencies.push(t.elapsed());
                            }
                            latencies
                        }));
                    }

                    for r in 0..num_readers {
                        let db = db.clone();
                        let barrier = barrier.clone();
                        let selects = gen_reader_selects(r);
                        reader_handles.push(std::thread::spawn(move || -> Vec<Duration> {
                            let conn = db.connect().unwrap();
                            barrier.wait();
                            let mut latencies = Vec::with_capacity(selects.len());
                            for sql in &selects {
                                let t = Instant::now();
                                loop {
                                    match conn.execute(sql) {
                                        Ok(()) => break,
                                        Err(turso_core::LimboError::Busy) => {
                                            std::thread::yield_now();
                                        }
                                        Err(e) => panic!("reader error: {e:?}"),
                                    }
                                }
                                latencies.push(t.elapsed());
                            }
                            latencies
                        }));
                    }

                    let start = Instant::now();
                    let mut write_lats = Vec::new();
                    for h in writer_handles {
                        write_lats.extend(h.join().unwrap());
                    }
                    let mut read_lats = Vec::new();
                    for h in reader_handles {
                        read_lats.extend(h.join().unwrap());
                    }
                    total += start.elapsed();

                    write_retry_sidecar(
                        "Writer Heavy Mixed",
                        "turso_wal",
                        writers,
                        retries.load(Ordering::Relaxed),
                    );
                    let (wavg, wp99) = compute_latency_stats(&write_lats);
                    let (ravg, rp99) = compute_latency_stats(&read_lats);
                    write_latency_sidecar(
                        "Writer Heavy Mixed",
                        "turso_wal",
                        writers,
                        wavg,
                        wp99,
                        ravg,
                        rp99,
                    );
                }
                total
            });
        });

        // --- sqlite_wal ---
        if enable_rusqlite {
            group.bench_function(BenchmarkId::new("sqlite_wal", writers), |b| {
                b.iter_custom(|iters| {
                    let mut total = Duration::ZERO;
                    for _ in 0..iters {
                        let temp_dir = tempfile::tempdir().unwrap();
                        let conn = setup_rusqlite(
                            &temp_dir,
                            "CREATE TABLE test (id INTEGER PRIMARY KEY, data TEXT)",
                        );
                        prepopulate_rusqlite(&conn, prepop_rows);
                        drop(conn);

                        let db_path = temp_dir.path().join("bench.db");
                        let all_participants = writers + num_readers;
                        let barrier = Arc::new(Barrier::new(all_participants));
                        let retries = Arc::new(AtomicU64::new(0));
                        let mut writer_handles = Vec::with_capacity(writers);
                        let mut reader_handles = Vec::with_capacity(num_readers);

                        let writer_updates = gen_writer_updates(writers);
                        for updates in writer_updates {
                            let db_path = db_path.clone();
                            let barrier = barrier.clone();
                            let retries = retries.clone();
                            writer_handles.push(std::thread::spawn(move || -> Vec<Duration> {
                                let conn = rusqlite::Connection::open(&db_path).unwrap();
                                conn.busy_timeout(std::time::Duration::from_secs(10))
                                    .unwrap();
                                barrier.wait();
                                let mut latencies = Vec::with_capacity(updates.len());
                                for sql in &updates {
                                    let t = Instant::now();
                                    execute_rusqlite_with_retry(&conn, sql, &retries);
                                    latencies.push(t.elapsed());
                                }
                                latencies
                            }));
                        }

                        for r in 0..num_readers {
                            let db_path = db_path.clone();
                            let barrier = barrier.clone();
                            let selects = gen_reader_selects(r);
                            reader_handles.push(std::thread::spawn(move || -> Vec<Duration> {
                                let conn = rusqlite::Connection::open(&db_path).unwrap();
                                conn.busy_timeout(std::time::Duration::from_secs(10))
                                    .unwrap();
                                barrier.wait();
                                let mut latencies = Vec::with_capacity(selects.len());
                                for sql in &selects {
                                    let t = Instant::now();
                                    let mut stmt = conn.prepare(sql).unwrap();
                                    let mut rows = stmt.raw_query();
                                    while let Some(_row) = rows.next().unwrap() {}
                                    latencies.push(t.elapsed());
                                }
                                latencies
                            }));
                        }

                        let start = Instant::now();
                        let mut write_lats = Vec::new();
                        for h in writer_handles {
                            write_lats.extend(h.join().unwrap());
                        }
                        let mut read_lats = Vec::new();
                        for h in reader_handles {
                            read_lats.extend(h.join().unwrap());
                        }
                        total += start.elapsed();

                        write_retry_sidecar(
                            "Writer Heavy Mixed",
                            "sqlite_wal",
                            writers,
                            retries.load(Ordering::Relaxed),
                        );
                        let (wavg, wp99) = compute_latency_stats(&write_lats);
                        let (ravg, rp99) = compute_latency_stats(&read_lats);
                        write_latency_sidecar(
                            "Writer Heavy Mixed",
                            "sqlite_wal",
                            writers,
                            wavg,
                            wp99,
                            ravg,
                            rp99,
                        );
                    }
                    total
                });
            });
        }
    }

    group.finish();
}

// ---------------------------------------------------------------------------
// Criterion harness
// ---------------------------------------------------------------------------

#[cfg(not(feature = "codspeed"))]
criterion_group! {
    name = parallel_write_benches;
    config = Criterion::default()
        .with_profiler(PProfProfiler::new(100, Output::Flamegraph(None)))
        .sample_size(10);
    targets =
        bench_disjoint_scalability,
        bench_hot_row_contention,
        bench_overlapping_insert,
        bench_single_row_increment,
        bench_writer_heavy_mixed
}

#[cfg(feature = "codspeed")]
criterion_group! {
    name = parallel_write_benches;
    config = Criterion::default().sample_size(10);
    targets =
        bench_disjoint_scalability,
        bench_hot_row_contention,
        bench_overlapping_insert,
        bench_single_row_increment,
        bench_writer_heavy_mixed
}

criterion_main!(parallel_write_benches);

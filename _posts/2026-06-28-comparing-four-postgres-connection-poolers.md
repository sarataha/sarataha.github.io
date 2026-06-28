---
layout: post
title: "Comparing four Postgres connection poolers: memory, throughput, failure modes"
date: 2026-06-28
---

Most people pick one of four connection poolers in front of Postgres:
PgBouncer, Odyssey, pgcat and PgPool-II. Comparisons of them usually
come down to one throughput chart. This article looks at what that
chart leaves out: how much memory each one uses, where it stops
accepting clients, how it behaves when connections open and close fast,
and what happens when you use the extended query protocol with prepared
statements. On my machine those things mattered more than the
transactions-per-second (TPS) number, and they repeated far better
between runs.

I ran all four against one Postgres 16 with the same workload. I report
throughput with its error bars. On a single laptop VM it changed two to
three times between identical runs, so one TPS number would be
misleading. Memory and failure behaviour barely moved, so most of this
is about those.

## Why a pooler at all

I started with what a raw connection costs. Each Postgres connection is
a full operating-system process. The server forks one on every connect,
which costs a few milliseconds and some memory, and it will not hand out
more than
[`max_connections`](https://www.postgresql.org/docs/16/runtime-config-connection.html),
100 by default. Point a few hundred short-lived clients at that and they
either queue for a slot or pay the fork cost again and again. A pooler
keeps a small set of connections open and lends them out, so clients
never hit either wall.

The cost shows up most clearly in memory, and it is easy to measure
wrong. I looked at one backend during a 100-client pgbench run:

```console
$ sudo head -7 /proc/$(pgrep -f 'postgres: 16/main: bench' | head -1)/smaps_rollup
591e2e3fb000-7ffe18504000 ---p 00000000 00:00 0                  [rollup]
Rss:              519992 kB
Pss:                8536 kB
Pss_Dirty:          8290 kB
Pss_Anon:           1903 kB
Pss_File:            246 kB
Pss_Shmem:          6386 kB
```

Resident set size says 520 MB. Proportional set size says 8.5 MB. The
gap is `shared_buffers`: the kernel counts it into every backend's RSS
even though they share the same pages. PSS divides shared pages by the
number of sharers, the honest per-process number. This VM runs
`shared_buffers = 2GB`, so RSS reads hundreds of megabytes too high per
backend. A pooler holds a small fixed set of backends, so you pay that
cost a few times, not once per client.

`pgbench` is the load tool that ships with Postgres. I use its
SELECT-only script (`-S`), which runs one indexed point read per
transaction. The flags I pass set the client count (`-c`), the thread
count (`-j`) and the run length (`-T`). One more flag matters later:
`-M prepared` switches from the simple query protocol to the extended
protocol with server-side prepared statements. SCRAM-SHA-256 is the
password authentication method Postgres has used by default since
version 14.

## The setup

One Ubuntu 24.04 VM under Lima on a 2019 MacBook Pro: 6 vCPU, 8 GiB.
Postgres, the pooler and `pgbench` all run in it and talk over
loopback. I gave the VM 8 GiB even though the laptop has 16. The host
swaps when memory is tight. `vm_stat` showed 443k pageouts on a two-day
uptime before I started. If it swaps during a run the numbers are
meaningless, so I left the host some spare memory.

Pinned versions: PostgreSQL 16.14, PgBouncer 1.25.2, Odyssey 1.4.0
(commit `ac95c5f`), pgcat (commit `4a7a6a8`), PgPool-II 4.7.1, kernel
6.8.0-106. Every pooler uses transaction pooling, a server pool of 100,
and
[`scram-sha-256`](https://www.postgresql.org/docs/16/auth-password.html)
on both sides. Real deployments use SCRAM, and it has a cost, so I kept
it in.

Postgres comes from PGDG, installed straight from the vendor repo,
nothing wrapped in a container:

```bash
sudo install -d /usr/share/postgresql-common/pgdg
sudo curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
  -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
  | sudo tee /etc/apt/sources.list.d/pgdg.list
sudo apt-get update && sudo apt-get install -y postgresql-16
sudo -u postgres psql -c "CREATE ROLE bench LOGIN PASSWORD 'bench';"
sudo -u postgres psql -c "CREATE DATABASE bench OWNER bench;"
PGPASSWORD=bench /usr/lib/postgresql/16/bin/pgbench -i -s 50 \
  -h 127.0.0.1 -U bench bench
```

Scale 50 is about 750 MB. It fits in cache, so the test measures the
pooler and not the disk.

## Pooling modes

I ran every test in transaction mode. The three pooling modes differ in
when the pooler hands a server connection back to the pool.

Session pooling keeps one server connection tied to a client for as
long as it stays connected. It is the safest, because the client gets a
real private session, but it pools the least: a hundred idle clients
hold a hundred server connections. Transaction pooling hands the
connection back at the end of each transaction, so a handful of
connections serve many clients. Statement pooling hands it back after
every statement and refuses multi-statement transactions outright.

I used transaction mode because it pools well without breaking ordinary
transactions, which is why most people run it. Anything that expects a
stable session breaks, because the next statement may land on a
different backend: session-level `SET`, advisory locks,
`LISTEN`/`NOTIFY`, and server-side prepared statements, which is the
whole reason the prepared-statement section below exists. PgBouncer,
Odyssey and pgcat all do session and transaction pooling, and PgBouncer
and Odyssey add statement pooling. PgPool-II works differently: each
client gets its own pooler process holding a backend, reused by user and
database rather than handed back per transaction, so it acts more like
session pooling. That design is also where its memory cost and its
connection ceiling come from, both below.

## PgBouncer

PgBouncer is in the Ubuntu archive. Install and write a config:

```bash
sudo apt-get install -y pgbouncer
```

```ini
# /etc/pgbouncer/pgbouncer.ini
[databases]
bench = host=127.0.0.1 port=5432 dbname=bench
[pgbouncer]
listen_addr = 127.0.0.1
listen_port = 6432
auth_type = scram-sha-256
auth_file = /etc/pgbouncer/userlist.txt
pool_mode = transaction
default_pool_size = 100
max_client_conn = 5000
max_db_connections = 100
max_prepared_statements = 200
ignore_startup_parameters = extra_float_digits
```

The SCRAM verifier for the `bench` user goes in `userlist.txt`:

```bash
sudo -u postgres psql -tAc \
  "SELECT '\"'||rolname||'\" \"'||rolpassword||'\"'
     FROM pg_authid WHERE rolname='bench';" \
  | sudo tee /etc/pgbouncer/userlist.txt
sudo systemctl restart pgbouncer
```

`max_prepared_statements = 200` turns on PgBouncer's transaction-mode
prepared statement support, added in 1.21. Without it, prepared
statements fail in transaction pooling.

## Odyssey

Odyssey builds from source. It needs the Postgres server headers and
an explicit CMake flag, and neither requirement is in any quickstart I
found. It will build without them and then refuse SCRAM at runtime
with `SCRAM auth is not supported in this build, try to recompile`.

The flag lives at
[`CMakeLists.txt:147`](https://github.com/yandex/odyssey/blob/ac95c5f3bdbf828c9a61e47a93302fb351600791/CMakeLists.txt#L147)
but nothing sets it by default. You have to pass it yourself. You also
need the server-dev headers so its
[`FindPostgreSQL.cmake`](https://github.com/yandex/odyssey/blob/ac95c5f3bdbf828c9a61e47a93302fb351600791/cmake/FindPostgreSQL.cmake#L12)
can find `scram-common.h`:

```bash
sudo apt-get install -y build-essential cmake libssl-dev libpq-dev \
  postgresql-server-dev-16
git clone --branch 1.4.0 https://github.com/yandex/odyssey
cmake -S odyssey -B odyssey/build -DCMAKE_BUILD_TYPE=Release -DUSE_SCRAM=ON
make -C odyssey/build -j"$(nproc)"
sudo install -m 0755 odyssey/build/sources/odyssey /usr/local/bin/odyssey
sudo install -d /etc/odyssey
```

Config:

```
# /etc/odyssey/odyssey.conf
daemonize yes
log_format "%p %t %l [%i %s] (%c) %m"
log_file "/dev/null"
pid_file "/run/odyssey.pid"
workers 2
resolvers 1

listen {
    host "127.0.0.1"
    port 6433
    backlog 128
}

storage "pg" {
    type "remote"
    host "127.0.0.1"
    port 5432
}

database "bench" {
    user "bench" {
        authentication "scram-sha-256"
        password "bench"
        storage "pg"
        storage_db "bench"
        storage_user "bench"
        storage_password "bench"
        pool "transaction"
        pool_reserve_prepared_statement yes
        pool_discard no
        pool_size 100
        pool_timeout 0
        pool_ttl 0
    }
}
```

Start it:

```bash
sudo odyssey /etc/odyssey/odyssey.conf
```

`pool_reserve_prepared_statement yes` enables prepared statement
support in transaction mode. It is incompatible with the default
`pool_discard yes`, which Odyssey will refuse at startup unless you
turn it off. I point `log_file` at `/dev/null` because the default
log verbosity filled 31 GB on the root disk over a few days.

## pgcat

pgcat builds from a stable Rust toolchain:

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
. "$HOME/.cargo/env"
git clone https://github.com/postgresml/pgcat
git -C pgcat checkout 4a7a6a8
cargo build --release --manifest-path pgcat/Cargo.toml
sudo install -m 0755 pgcat/target/release/pgcat /usr/local/bin/pgcat
sudo install -d /etc/pgcat
```

Config:

```toml
# /etc/pgcat/pgcat.toml
[general]
host = "127.0.0.1"
port = 6434
admin_username = "admin"
admin_password = "admin"

[pools.bench]
pool_mode = "transaction"
prepared_statements_cache_size = 500

[pools.bench.users.0]
username = "bench"
password = "bench"
pool_size = 100
min_pool_size = 1
server_lifetime = 86400

[pools.bench.shards.0]
servers = [["127.0.0.1", 5432, "primary"]]
database = "bench"
```

There is no systemd unit in the source tree. Start it as a transient
service so it survives shell exit:

```bash
sudo systemd-run --unit=pgcat --collect \
  /usr/local/bin/pgcat /etc/pgcat/pgcat.toml
```

## PgPool-II

PgPool-II is in the Ubuntu archive but the SCRAM passthrough is not
obvious. It rejects connections with `failed to authenticate with
backend using SCRAM` if you load the SCRAM verifier from `pg_shadow`
into `pool_passwd`. PgPool wants an AES-encrypted password in
`pool_passwd`, and it needs the key in the process environment. The
shipped systemd unit does not set that key, so the service fails
quietly.

```bash
sudo apt-get install -y pgpool2
echo 'pgpoolsecret' | sudo tee /etc/pgpool2/.pgpoolkey
sudo chown postgres:postgres /etc/pgpool2/.pgpoolkey
sudo chmod 600 /etc/pgpool2/.pgpoolkey
sudo pg_enc -m -f /etc/pgpool2/pgpool.conf -k /etc/pgpool2/.pgpoolkey \
  -u bench bench
echo "host all all 127.0.0.1/32 scram-sha-256" \
  | sudo tee /etc/pgpool2/pool_hba.conf
```

Config (the rest of `pgpool.conf` is the shipped default):

```
listen_addresses = '127.0.0.1'
port = 6435
backend_hostname0 = '127.0.0.1'
backend_port0 = 5432
backend_weight0 = 1
num_init_children = 100
max_pool = 1
connection_cache = on
enable_pool_hba = on
pool_passwd = 'pool_passwd'
```

Start it as `postgres` with the key in the environment:

```bash
sudo -u postgres PGPOOLKEYFILE=/etc/pgpool2/.pgpoolkey \
  pgpool -f /etc/pgpool2/pgpool.conf
```

That last command cost me longer than any of the benchmarks did.

## Memory

Resident memory was the most repeatable thing I measured. Peak RSS of
the pooler process tree under a SELECT-only run with simple query
protocol, at three client counts:

| Pooler     | 10 clients | 100   | 500   |
|------------|-----------:|------:|------:|
| Odyssey    |        7.9 |  20.1 |  48.9 |
| PgBouncer  |       12.9 |  15.7 |  18.8 |
| pgcat      |       15.4 |  20.3 |  33.7 |
| PgPool-II  |      585.2 | 1013.7 | capped |

PgPool-II is the outlier, and not by a little:

```console
$ pgrep -x pgpool | wc -l
104
$ ps --no-headers -o rss -p "$(pgrep -x pgpool | paste -sd, -)" \
    | awk '{s+=$1} END{printf "%.1f MB\n", s/1024}'
1013.7 MB
```

104 processes and a gigabyte before it answers a single query. It forks
[`num_init_children`](https://www.pgpool.net/docs/latest/en/html/runtime-config-connection-pooling.html)
workers at startup, each a full process holding a backend, and "capped"
above means 500 clients hit that ceiling and waited at the pooler. The
other three keep serving past their pool size and only their footprint
grows. If you run many Postgres clusters on one host, the difference at
ten clients matters more than the one at five hundred.

## Prepared statements

The numbers above are the simple query protocol. With prepared
statements (`-M prepared`) the picture changes for some of them, and
two of them break.

What happens with each pooler's default config:

| Pooler     | `-M prepared`                |
|------------|------------------------------|
| PgBouncer  | works (1.25 default)         |
| Odyssey    | fails: 0 transactions        |
| pgcat      | fails: 0 transactions        |
| PgPool-II  | works                        |

The reason is the same in both failures: in transaction pooling the
server connection is handed to a different client between transactions,
so a server-side `PREPARE` on one connection is gone the next time a
client tries to `EXECUTE` it. Each pooler needs explicit configuration
to track prepared statements across the pool. PgBouncer turns this on
by default in 1.25. Odyssey and pgcat do not.

After turning the cache on (`max_prepared_statements = 200` for
PgBouncer, `pool_reserve_prepared_statement yes` for Odyssey,
`prepared_statements_cache_size = 500` for pgcat) and rerunning the
same 100-client, 60-second pass:

| Pooler     | simple (MB) | prepared (MB) |
|------------|------------:|--------------:|
| PgBouncer  |        15.7 |          18.8 |
| Odyssey    |        20.1 |          44.9 |
| pgcat      |        20.3 |     aborts    |
| PgPool-II  |      1013.7 |        1059.4 |

This workload prepares a single statement, so the table is the cost of
caching one prepared statement across a pool of 100, not a per-statement
figure. Even at that it shows: Odyssey roughly doubles, from 20 to 45
MB, and PgPool-II adds 46 MB across its 104 workers, about 0.4 MB per
worker. PgBouncer grows a few megabytes. pgcat with caching turned on
still aborts mid-run, the clients seeing `client X aborted ... perhaps
the backend died while processing`. A real application prepares many
different statements, and each one is cached per server connection, so
on a pooler that caches plans the number above is the low end.

## PgPool-II's connection ceiling

I spent ten minutes thinking a run had frozen the machine. It hadn't.
PgPool-II serves exactly `num_init_children` clients. I set that to 100
and sent 250. The extra 150 don't slow down, they wait forever, and
Postgres behind the pooler is almost idle:

```console
$ psql -h 127.0.0.1 -p 5432 -U bench bench -c \
  "SELECT state, count(*) FROM pg_stat_activity
   WHERE usename='bench' GROUP BY state ORDER BY state;"
 state  | count
--------+-------
 active |     1
 idle   |   100
(2 rows)
```

One hundred backends, the number I set `num_init_children` to, and one
of them doing work. The rest are parked, and the 150 clients past the
hundredth are still blocked at the pooler. That's the process-per-connection model.
The other three slow down past their pool size but keep serving.
PgPool-II's capacity is a number you set at boot. You pay for it in RAM
whether you use it or not.

## pgcat and the system clock

The churn test opens a fresh connection and runs a full SCRAM handshake
on every transaction. Under it, pgcat sometimes returns no numbers,
because the worker thread is dead:

```
thread 'tokio-runtime-worker' panicked at src/pool.rs:782:53:
called `Result::unwrap()` on an `Err` value: SystemTimeError(166.254242ms)
```

The line is
[`src/pool.rs:782`](https://github.com/postgresml/pgcat/blob/4a7a6a8e7a78354b889002a4db118a8e2f2d6d79/src/pool.rs#L782),
`server.last_activity().elapsed().unwrap()`. The comment right above it
says the timestamp "should never be set to" a value past now. Under
Lima's `vz` hypervisor the guest clock is stepped to fix drift. When it
steps back, `elapsed()` returns an error, and the `unwrap()` kills the
thread. The hard part is that it's intermittent. It killed one churn
run, and when I tried to catch it again the next run finished clean at
596 TPS. Anything that can move the clock backward can hit this: a VM,
a container with aggressive time sync, a host catching up over NTP.

## Throughput

I let `hyperfine` run a fixed-transaction workload five times per
target and report what it found:

```console
$ hyperfine --warmup 1 --runs 5 \
  -n direct    "pgbench -S -n -p 5432 -U bench bench -c 10 -j 6 -t 5000" \
  -n pgbouncer "pgbench -S -n -p 6432 -U bench bench -c 10 -j 6 -t 5000" \
  -n odyssey   "pgbench -S -n -p 6433 -U bench bench -c 10 -j 6 -t 5000" \
  -n pgpool    "pgbench -S -n -p 6435 -U bench bench -c 10 -j 6 -t 5000"

Benchmark 1: direct
  Time (mean ± σ):     11.338 s ±  5.166 s    5 runs
Benchmark 2: pgbouncer
  Time (mean ± σ):     41.926 s ±  5.235 s    5 runs
Benchmark 3: odyssey
  Time (mean ± σ):     13.602 s ±  3.109 s    5 runs
Benchmark 4: pgpool
  Time (mean ± σ):      5.393 s ±  0.289 s    5 runs

Summary
  pgpool ran 7.77 ± 1.06 times faster than pgbouncer
```

`direct` is 11.3 s give or take 5.2 s. That's a 46% swing on five runs
of the same command. `pgpool` is the only tight result here, and the
fastest at this client count. `pgbouncer` is single-threaded and runs
clients in series, so it's almost eight times slower. The rough order
is real. The fine order is not: is `direct` really slower than
`odyssey`? The error bars overlap, so I won't say. I ran the whole set
of tests four times before I trusted it, and the spread is why. So I
show the spread, not a clean leaderboard, because the spread is what I
measured.

The full output for Odyssey at 100 clients shows something the TPS
figure alone doesn't:

```console
$ pgbench -S -n -p 6433 -U bench bench -c 100 -j 6 -T 15
number of transactions actually processed: 6861
latency average = 116.587 ms
initial connection time = 7152.942 ms
tps = 857.724925 (without initial connection time)
```

Seven seconds to accept a hundred connections. That's half of a
fifteen-second run gone before the first query. It's also why my
fixed-transaction `hyperfine` pass failed at higher client counts:
`pgbench` gave up waiting for connections faster than Odyssey would
open them. The TPS line never shows this. The `initial connection time`
line does.

## Connection churn

A new connection plus a SCRAM handshake on every transaction. This is
the short-lived-client pattern. pgcat is out, it panics. Median TPS,
min to max in brackets, three runs each:

| Target     | 25            | 100          | 250           |
|------------|---------------|--------------|---------------|
| direct     | 91 [90..94]   | 95 [92..102] | 103 [98..106] |
| PgBouncer  | 92 [81..110]  | 65 [63..67]  | 43 [42..65]   |
| Odyssey    | 55 [46..68]   | 76 [72..87]  | 98 [91..103]  |
| pgcat      | panics        | panics       | panics        |
| PgPool-II  | 73 [72..84]   | 85 [76..88]  | capped        |

Two-digit throughput everywhere. A SCRAM handshake on every
transaction is the price of not reusing connections. One split held
every run: PgBouncer gets worse as churn rises, 92 down to 43; Odyssey
goes the other way, 55 up to 98. If your traffic is really churn-heavy,
that direction matters more than any single number here.

## Sustained load

Memory at one instant does not tell you whether it leaks. I let each
pooler serve a 50-client SELECT-only workload for thirty minutes from a
cold start and sampled resident memory every ten seconds. RSS at the
start, after the first five minutes, and at the end:

| Pooler     | start (MB) | 5 min (MB) | 30 min (MB) | outcome   |
|------------|-----------:|-----------:|------------:|-----------|
| PgBouncer  |       12.8 |       14.5 |        14.5 | clean     |
| Odyssey    |        6.7 |       13.4 |        13.4 | clean     |
| pgcat      |       12.5 |        n/a |         n/a | aborted   |
| PgPool-II  |      524.8 |      767.9 |       768.1 | clean     |

The shape is the same for all three that finished: memory climbs to a
working set in the first few minutes, then holds flat for the rest of
the half hour. Odyssey doubles, from 6.7 to 13.4 MB. PgPool-II grows
243 MB, from 525 to 768 MB, with no change in process count. After that
warm-up not one of them drifted by more than a rounding error over the
next twenty-five minutes, so this is warm-up to a steady state, not a
leak. If you size from the number you see one second after start, you
will under-provision; size from the plateau.

pgcat aborted again. Its clock panic does not need connection churn to
fire; thirty minutes of steady load was enough for the guest clock to
step backward once and kill a worker. It managed 26000 TPS until it
did.

## What I'd use

I'd choose on memory and failure mode first, and use throughput only as
a second check. On anything less than a dedicated machine the
throughput gap is noise, and the memory gap is a hundred times. Odyssey
if the box is tight and traffic churns, and turn the prepared statement
cache on if your application uses one. PgBouncer if I want something
small and predictable and my clients hold their connections. Not pgcat
anywhere the clock can jump, or where prepared statements matter. PgPool-II
only if I plan for its ceiling and its gigabyte up front.

And if you run these on a laptop like I did, publish the spread. A
single TPS number from a shared machine looks more precise than it is.

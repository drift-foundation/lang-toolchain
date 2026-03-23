# Drift: return value corruption when function has conditional move + return on multiple branches

## Summary

A function that moves an owned struct on two branches (conditional early return
and normal return) returns a corrupted value. The function's `return 0`
produces exit code 45 instead of 0. The function prints correctly and reaches
the expected return statement, but the return value received by the caller is
wrong.

## Severity

High. Silent data corruption — no crash, no exception, no ASAN error. The
function completes normally but returns the wrong value.

## Reproduction

### Prerequisites

- MariaDB on 127.0.0.1:13306 with root/scratchpad22
- Singular schema and stored procedures loaded in `singular` database
- Singular client source files (lib.drift, uuid.drift, client.drift)
- mariadb-rpc 0.1.3 (with binary column fix deployed)

### Minimal trigger (requires Singular + mariadb-rpc, not yet reduced to pure Drift)

File: `/tmp/repro_s123b.drift`

```drift
module repro;

import std.core as core;
import std.console as con;
import singular.uuid as uuid;
import singular.client as client;

fn s1() -> Int {
    val cfg = client.SingularConfig(host = "127.0.0.1", port = 13306,
        user = "root", password = "scratchpad22", database = "singular",
        service_group = "drift-test",
        lease_owner = uuid.uuid_v3_from_string(&"w1"),
        connect_timeout_ms = 3000, io_timeout_ms = 3000);
    var c = client.create_client(cfg);
    val key = uuid.uuid_v3_from_string(&"s1-key");
    val _ = client.claim(&mut c, &key, &"{\"t\":\"1\"}", &"{}", 30);
    val _ = client.complete(&mut c, &key, &"{\"r\":\"ok\"}");
    client.close_client(move c);
    return 0;
}

fn s2() -> Int {
    val cfg = client.SingularConfig(host = "127.0.0.1", port = 13306,
        user = "root", password = "scratchpad22", database = "singular",
        service_group = "drift-test",
        lease_owner = uuid.uuid_v3_from_string(&"w2"),
        connect_timeout_ms = 3000, io_timeout_ms = 3000);
    var c = client.create_client(cfg);
    val key = uuid.uuid_v3_from_string(&"s2-key");
    val _ = client.claim(&mut c, &key, &"{\"t\":\"f\"}", &"{}", 30);
    val _ = client.fail(&mut c, &key, &"{\"e\":\"boom\"}", false);
    client.close_client(move c);
    return 0;
}

fn s3() -> Int {
    val cfg = client.SingularConfig(host = "127.0.0.1", port = 13306,
        user = "root", password = "scratchpad22", database = "singular",
        service_group = "drift-test",
        lease_owner = uuid.uuid_v3_from_string(&"w3"),
        connect_timeout_ms = 3000, io_timeout_ms = 3000);
    var c = client.create_client(cfg);
    val key = uuid.uuid_v3_from_string(&"s3-missing");
    val ir = client.inspect(&mut c, &key);
    if ir.found { client.close_client(move c); return 301; }
    client.close_client(move c);
    return 0;
}

pub fn main() nothrow -> Int {
    con.println("s1");
    val a = try s1() catch { 199 };
    if a != 0 { return a; }
    con.println("s2");
    val b = try s2() catch { 299 };
    if b != 0 { return b; }
    con.println("s3");
    val d = try s3() catch { 399 };
    if d != 0 { return d; }
    con.println("ok");
    return 0;
}
```

Compile:
```
driftc --target-word-bits 64 \
  --package-root ~/opt/drift/libs \
  --dep "mariadb-rpc@0.1.3" --dep "mariadb-wire-proto@0.1.3" \
  --entry "repro::main" \
  packages/singular/src/lib.drift \
  packages/singular/src/uuid.drift \
  packages/singular/src/client.drift \
  /tmp/repro_s123b.drift \
  -o /tmp/repro_s123b
```

Before running, clean the test data:
```
mysql -h 127.0.0.1 -P 13306 -u root -pscratchpad22 singular \
  -e "DELETE FROM tb_singular_work_item_history WHERE service_group='drift-test';
      DELETE FROM tb_singular_work_item WHERE service_group='drift-test';"
```

### Expected behavior

```
s1
s2
s3
ok
exit: 0
```

### Actual behavior

```
s1
s2
s3
exit: 45
```

All three scenarios complete (s1, s2, s3 all print). s3 returns normally
(ir.found is false, the early return branch is NOT taken, close_client runs,
return 0 executes). But the value received by main is not 0 — it's some
corrupted value that results in exit code 45.

## Observations

1. s3 alone passes (exit 0).
2. s1 + s3 passes (exit 0).
3. s1 + s2 + s3 with the `if ir.found` check passes (exit 0) ONLY IF
   `close_client` is called unconditionally (not on two branches).
4. Adding the conditional `if ir.found { close_client(move c); return 301; }`
   before the final `close_client(move c); return 0;` triggers the corruption.
5. No ASAN errors — this is not a memory safety issue but a codegen issue.
6. Does not reproduce with simple user-defined structs — requires
   RpcConnection-owning struct and prior SP calls on the same process.

## Suspected root cause

The pattern is a function where an owned struct (containing RpcConnection) is
moved-and-consumed on two code paths:
```
if condition { consume(move x); return A; }
consume(move x);
return B;
```

The codegen for the conditional move + return appears to leave the return value
register in an inconsistent state when the condition is false and the
fall-through path is taken. This only manifests after prior functions in the
same process have executed Singular SP calls — suggesting a register or stack
state interaction.

## Environment

- driftc 0.27.103+abi6
- mariadb-rpc 0.1.3 (with binary column fix)
- MariaDB 11.8.x

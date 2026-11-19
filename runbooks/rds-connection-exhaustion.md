# RDS connection exhaustion — too many clients

Operational runbook for diagnosing "too many connections" failures against
RDS Postgres instances in Dev/QA. Every statement below is a read-only query.

## Symptom

Application logs show `FATAL: remaining connection slots are reserved` or
psql answering `sorry, too many clients already`. New login attempts from the
app pool are refused while existing sessions keep working, and the
CloudWatch `DatabaseConnections` metric sits flat at the instance maximum.
The database is healthy but full: too many connections are held, and the
classic signature is "too many connections" errors appearing under load or
right after a deploy.

## Likely causes, ranked

Evaluate in this order.

### 1. Connection leak in the application

Code paths that open a connection (or checkout from a pool) without returning
it — error paths are the usual culprit. Signature: `pg_stat_activity` shows
a growing population of `idle` sessions from one `client_addr`, and they never
go away until the app restarts.

### 2. Pool sizing exceeds max_connections

Several app replicas each hold a max pool sized for the whole instance; the
sum over-commits. Signature: exhaustion is steady-state, not spike-driven.

### 3. Connection storm at deploy time

Every replica reconnects simultaneously on rolling deploys without a pooler
in front of the database, briefly doubling connection demand.

### 4. Long idle-in-transaction sessions

Sessions holding a transaction open (forgotten COMMIT) pin slots and block
vacuum. Signature: `state = 'idle in transaction'` rows with old
`state_change` timestamps.

### 5. Reserved slots eaten by tooling

Superuser_reserved_connections reduces usable slots; monitoring agents or
ad-hoc psql sessions from humans consume what the app assumed it had.

## Diagnostic steps (read-only)

1. Count sessions and compare with the instance limit:

   ```sql
   SHOW max_connections;
   SELECT count(*) FROM pg_stat_activity;
   ```

2. Break sessions down by state — idle growth points at a leak:

   ```sql
   SELECT state, count(*)
   FROM pg_stat_activity
   GROUP BY state
   ORDER BY count(*) DESC;
   ```

3. Find the oldest idle sessions and their source:

   ```sql
   SELECT pid, usename, application_name, client_addr, state,
          now() - state_change AS age
   FROM pg_stat_activity
   WHERE state LIKE 'idle%'
   ORDER BY age DESC
   LIMIT 20;
   ```

4. Check whether slots are pinned by open transactions:

   ```sql
   SELECT pid, now() - xact_start AS xact_age, query
   FROM pg_stat_activity
   WHERE xact_start IS NOT NULL
   ORDER BY xact_age DESC
   LIMIT 20;
   ```

5. Confirm the shape on the CloudWatch side: `DatabaseConnections` pegged at
   the same value across deploys means over-commit, not a leak.

## Remediation (human-executed)

- Short term: restart the application pool (or recycle the offending
  workers) to release leaked slots; confirm `pg_stat_activity` drains.
- Fix the leak: ensure every checkout is released on error paths; add pool
  idle-timeout and max-lifetime settings.
- Right-size pools so `replicas x pool_max + tooling` stays under
  `max_connections` with headroom.
- Put a pooler (RDS Proxy) in front of the instance to absorb reconnect
  storms at deploy time.
- Schedule any `max_connections` change explicitly: it needs a parameter-group
  apply and, for static parameters, a restart window.

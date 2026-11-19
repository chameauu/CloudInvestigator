# ECS task OOM — exit code 137

Operational runbook for diagnosing ECS tasks killed for memory
(OOMKilled, exit code 137) in Dev/QA. Every command below is read-only.

## Symptom

ECS tasks stop with `Essential container exited with code 137` and the
service enters a restart loop. CloudWatch `MemoryUtilization` for the service
pins at the task memory limit right before each stop. The kernel, not the
application, terminated the process: `137` is SIGKILL, and the classic
signature is the OOM killer firing when resident memory crosses the cgroup
budget for the whole task.

## Likely causes, ranked

Evaluate in this order.

### 1. Container hard memory limit too small for real load

The task definition's memory hard limit was sized for demo traffic and no
longer fits production-shaped batches. Signature: steady growth to the limit,
then SIGKILL, repeating.

### 2. Runtime heap not container-aware

A JVM or Node process sized its heap from the host's memory instead of the
container limit, so the runtime itself guarantees an OOM. Signature: the
process reports a heap far larger than the task definition allows.

### 3. Memory leak in the application

Native or heap allocations grow without bound; restarts "fix" it briefly.
Signature: sawtooth `MemoryUtilization` with a rising baseline across days.

### 4. Sidecars share the task memory budget

All containers in one ECS task share a single memory cgroup: a log shipper
or metrics agent quietly eats the budget the app assumed it had. Signature:
OOMKilled with the app itself looking innocent in its own logs.

### 5. No memory reservation headroom

Overcommit without soft reservations lets one task starve a neighbor on the
instance; the kernel then kills the largest consumer, which is your app.

## Diagnostic steps (read-only)

1. Read the stopped reason and the memory layout from ECS:

   ```bash
   aws ecs describe-tasks --cluster qa-apps --tasks <task-arn>
   ```

   `lastStatus: STOPPED`, `stopCode: OutOfMemory` (or `Reason: OOMKilled`)
   with exit code 137 confirms kernel termination.

2. Read the task definition and compare limits with reality:

   ```bash
   aws ecs describe-task-definition --task-definition <family:rev>
   ```

   Note `memory` at task level and each container's `memory` (hard) and
   `memoryReservation` (soft). Sum the hard limits across containers and
   compare with observed usage.

3. On the CloudWatch side, watch `MemoryUtilization` and `MemoryReserved`
   for the service: a flat line at the limit before each stop is the
   cgroup ceiling being hit; the shape (sawtooth vs cliff) separates leak
   from under-sized limit.

4. In the container logs, search for allocator messages that precede the
   kill: JVM `OutOfMemoryError`, V8 heap errors, or glibc allocation
   failures. Their absence points at a sidecar or native code instead.

5. For the sidecar hypothesis, compare per-container CPU/memory telemetry if
   available, or temporarily reproduce with the sidecar disabled in a scratch
   service definition.

## Remediation (human-executed)

- Raise the task memory (and the instance capacity to match) so the limit
  sits well above p99.9 observed usage; treat the old limit as the rollback
  value.
- Make the runtime container-aware: cap the JVM heap with a percentage of
  the container budget (e.g. `MaxRAMPercentage`) or set `--max-old-space-size`
  for Node from the task limit, leaving headroom for off-heap memory.
- Profile a suspected leak before raising limits again — a bigger limit only
  delays the same crash. Capture a heap dump in QA and bisect the growth.
- Budget for the whole task: hard limits for every container (including
  sidecars) plus headroom, and set soft reservations so the scheduler places
  the task where the memory actually exists.
- After the change, verify with a load replay in QA that memory plateaus
  below the new limit across at least one full workload cycle.

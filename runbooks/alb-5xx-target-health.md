# ALB 5xx — target health diagnosis

Operational runbook for diagnosing client-facing 5xx spikes caused by
unhealthy targets behind an Application Load Balancer in Dev/QA. Every
command below is read-only.

## Symptom

Clients receive 502/503/504 responses from the load balancer while the
listener itself is up. CloudWatch `HTTPCode_Target_5XX_Count` spikes and
`HealthyHostCount` drops — sometimes only in one availability zone. The ALB
is a messenger: a 502 means the target answered badly or died mid-response, a
503 means no healthy target could take the request, a 504 means the target
never answered in time.

## Likely causes, ranked

Evaluate in this order.

### 1. Target application failing its health check

The app crashes, hangs, or returns 5xx on the health-check path; targets
flip unhealthy, in-flight requests get 502s, and new requests stack up.

### 2. No healthy targets in an availability zone

Half the registered targets are unhealthy (or deregistered) so the balancer
returns 503 for that zone. Signature: errors correlated with one subnet/AZ.

### 3. Response timeout and keepalive mismatch

The target takes longer than the listener idle timeout to respond (504), or
the target closes keepalive sockets earlier than the balancer reuses them,
surfacing as sporadic 502s under load.

### 4. Health-check path misconfigured

The check hits an endpoint that requires auth or was moved in a release, so
healthy instances are marked unhealthy (503 with instances actually fine).

### 5. Deregistration during deploys

Rolling deploys deregister too many targets at once; with a short
deregistration delay, the balancer stops routing before capacity returns.

## Diagnostic steps (read-only)

1. Read target health per target group — the reason field names the check
   that failed:

   ```bash
   aws elbv2 describe-target-health --target-group-arn <tg-arn>
   ```

2. Confirm which listeners and rules feed the affected target group:

   ```bash
   aws elbv2 describe-target-groups --load-balancer-arn <alb-arn>
   aws elbv2 describe-listeners --load-balancer-arn <alb-arn>
   ```

3. On the CloudWatch side, correlate the 5xx class with capacity and latency:

   - `HTTPCode_Target_5XX_Count` — which class (502/503/504) dominates?
   - `HealthyHostCount` per target group — did capacity drop first?
   - `TargetResponseTime` — rising latency precedes 504 timeouts.
   - `HTTPCode_ELB_5XX_Count` — distinguishes balancer errors from target
     errors; a spike here with flat target counts points at the ALB itself.

4. In the target application logs, look for restarts, panics, or request
   handler timeouts around the spike. The balancer only reports symptoms;
   the app logs hold the mechanism.

5. If only one AZ is affected, compare `HealthyHostCount` across subnets and
   check that the auto-scaler did not retire capacity in that zone.

## Remediation (human-executed)

- Fix the health-check path or response code expectation so the check
  reflects real readiness (e.g. a dedicated unauthenticated endpoint).
- Align timeouts: listener idle timeout strictly below the target's
  keepalive timeout; raise the target's response budget or fix the slow
  handler that trips 504s.
- During deploys, keep enough healthy targets in every AZ: adjust batch
  size and deregistration delay so capacity never hits zero in a zone.
- Rollback path: re-register the previous task/instance revision and confirm
  `HealthyHostCount` recovers before re-attempting the rollout.

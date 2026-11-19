# S3 AccessDenied — IAM policy diagnosis

Operational runbook for diagnosing `AccessDenied` errors on S3 uploads and
reads in the Dev/QA data platform. Every command below is read-only.

## Symptom

An ingestion job or application call fails with an error like
`An error occurred (AccessDenied) when calling the GetObject operation` or
`(AccessDenied) when calling the PutObject operation`. The failure is
consistent per bucket and per role, and it appears suddenly after a policy
change rather than gradually. The most common cause of S3 AccessDenied is an
IAM policy layer that denies the caller — an explicit Deny somewhere in the
evaluation chain — not a fault in the application or the network.

## Likely causes, ranked

Evaluate in this order; the first one that matches the evidence is usually
the root cause.

### 1. Explicit Deny in the bucket policy

If s3:GetObject returns AccessDenied while the caller's IAM identity policy
already grants s3:GetObject, check the bucket policy for an explicit Deny
statement — an explicit Deny always wins over any Allow, in every evaluation
order. A common trigger: an automation role adds a restrictive statement
(published as a new policy version) that matches the uploader principal more
broadly than intended.

### 2. Missing or mis-scoped action in the identity policy

The role's identity policy never granted `s3:GetObject` / `s3:PutObject` for
this bucket ARN, or the resource scope excludes the key prefix in use.

### 3. KMS key policy denies decrypt

Objects encrypted with SSE-KMS fail GetObject if the caller lacks
`kms:Decrypt` on that key. Signature: the same role reads unencrypted objects
fine but fails on encrypted ones.

### 4. SCP or permissions boundary above the account

A service control policy or permissions boundary silently trims what the
identity policy appears to grant. Check the org layer when account-level
changes shipped the same morning.

### 5. Principal mismatch (assumed role vs direct credentials)

The running code uses different credentials than the ones the policy was
written for (e.g. task role vs local profile). Network faults do NOT produce
AccessDenied — a network problem yields timeouts or DNS failures, and the
request never reaches an IAM decision.

## Diagnostic steps (read-only)

1. Confirm who is calling:

   ```bash
   aws sts get-caller-identity
   ```

2. Read the bucket policy as Postgres sees it, and note the version/date:

   ```bash
   aws s3api get-bucket-policy --bucket qa-data-lake
   ```

3. List the role's attached and inline policies:

   ```bash
   aws iam list-attached-role-policies --role-name qa-ingest-uploader
   aws iam list-role-policies --role-name qa-ingest-uploader
   ```

4. In the authorized audit database, find the deny decision and the policy
   statement that produced it:

   ```sql
   SELECT actor, action, decision, resource, policy_id, ts
   FROM iam_audit_events
   WHERE resource LIKE 'arn:aws:s3:::qa-data-lake%'
   ORDER BY ts DESC
   LIMIT 50;
   ```

   A `decision=DENY` row with a `policy_id` referencing a bucket-policy
   version confirms cause 1. Rows are evidence: cite the policy_id in the
   report.

5. Correlate timing: the policy change event should precede the first failed
   upload by seconds to minutes.

## Remediation (human-executed)

- Remove the explicit Deny statement, or narrow its principal/action/resource
  so the uploader role is no longer matched. Publish as a new policy version
  so the previous one remains as rollback.
- Never widen a policy under incident pressure without recording the before
  and after versions.
- After the edit, run one manual test upload through the same role and
  confirm a success log line end-to-end.
- Rollback path: restore the previous bucket-policy version if the new one
  breaks other consumers.

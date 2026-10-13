# Oracle A1.Flex resize plan: dtox 2 OCPU / 12 GB to 4 OCPU / 24 GB

Status: preparation only. No OCI, billing, VM, service, storage, network,
configuration, collection, snapshot, or workload changes were made. A larger
shape may relieve RAM pressure; capacity or performance is not guaranteed.
Main must confirm account inventory, shape availability, and billing.

## Evidence and boundaries

- Latest read-only SSH observation supplied for this QA: 2 OCPU / 12 GB,
  MemTotal 12,213,564 kB, MemAvailable 1,904,284 kB (about 1.82 GiB), and
  Qdrant limit 10,737,418,240 bytes (10 GiB). Point-in-time only, not a peak
  or capacity guarantee.
- Latest reported full Qdrant snapshot is dated 2026-09-27, 32.49 GB, with a
  checksum recorded. Weekly FTS backup is also dated September 27. No fresh
  full snapshot or full Qdrant/application restore verification is reported.
  The isolated registry restore does not verify Qdrant recovery.
- Plan a low-load preflight before the window. Verify snapshot metadata,
  checksum record, available storage, current service health, and recovery
  ownership. Do not run a heavy restore, rebuild, benchmark, or multi-hour test.
- Keep Oracle bge-small disabled. Do not rebuild HNSW, reingest, grow pipeline
  workloads, or change unrelated services as part of this resize.
- OCI documents that a shape change preserves public/private IP addresses,
  volume attachments, and VNIC attachments. Do not expect an IP change from
  this operation. Record current reachability; treat any separate network
  change as out of scope. The Contabo tunnel should reconnect after reboot;
  no SSH configuration, known-hosts, or key edits without owner approval.
- Do not put credentials, secret paths, private addresses, public OCIDs, or
  actual keys in this plan.

## Billing and free-tier check

- The current [OCI price list](https://www.oracle.com/cloud/price-list/)
  explicitly gives paid tenancies 3,000 OCPU-hours and 18,000 GB-hours of A1
  usage free each month, shared across bare metal, VMs, and container instances.
- Estimate for continuous 4 OCPU / 24 GB over 31 days: 2,976 OCPU-hours and
  17,856 GB-hours. This is below 3,000 / 18,000 only if that aggregate A1
  allowance applies to this account and there is no other usage across A1 VMs,
  bare metal, or containers. Do not treat this calculation as a price quote.
- OCI's [Always Free limits](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
  state 1,500 OCPU-hours and 9,000 GB-hours per month for A1 with wording about
  all tenancies, conflicting with the explicit paid allowance in the price
  list. A 4/24 instance exceeds the smaller limit, but fits the paid allowance.
  The exact
  account eligibility, allowance, and aggregate usage are unverified and need
  owner Console login/inventory review by Main. Confirm expected charges in
  the [OCI price list](https://www.oracle.com/cloud/price-list/) and account
  cost estimate before approval.
- An upgrade requires owner/billing-admin consent and is one-way: Oracle says
  an upgraded paid account cannot be downgraded to Free Tier. OCI documents a
  temporary USD 100 card authorization on upgrade, reversed by Oracle; bank
  processing time varies. See [upgrade/payment details](https://docs.oracle.com/en-us/iaas/Content/Billing/Tasks/changingpaymentmethod.htm)
  and [Free Tier FAQ](https://www.oracle.com/cloud/free/faq/).
- OCI budgets are soft spending thresholds with periodic alerts, not a hard
  spend cap. See [budget overview](https://docs.oracle.com/en-us/iaas/Content/Billing/Concepts/budgetsoverview.htm).
  No financial action was performed.

## Approval and preflight gates

1. Main checks official [A1 resize instructions](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/resizinginstances.htm),
   target shape/image compatibility, capacity report, account service limits,
   and billing inventory. OCI says shape changes preserve IPs, volumes, and
   VNICs. If running, the instance reboots; OS shutdown beforehand is
   recommended to avoid abrupt application termination, not a hard requirement
   when already stopped. A secondary VNIC may need guest reconfiguration.
2. Owner approves maintenance downtime and any CLI/provider or billing action.
   Confirm the one-way paid upgrade and expected spend. No action is authorized
   by this plan.
3. Record shape, volume/VNIC attachments, service health, Qdrant cap,
   host/cgroup memory, disk headroom, and current connectivity. Confirm tunnel
   reconnect behavior. Do not make separate network or SSH changes.
4. Confirm access to the September 27 snapshot and checksum record and adequate
   storage. No fresh full snapshot/restore verification exists. Any required
   fresh backup or restore proof is a separately approved operation, not a
   heavy preflight test.

Any failed gate means defer. Do not compensate by raising limits, changing SSH
trust, or testing under production load.

## Window sequence

1. Announce the interruption and pause only owner-approved ingestion or
   write-producing work. Capture a quiet baseline and confirm no active
   maintenance or optimizer work. Avoid extra multi-hour tests.
2. Shut down the OS cleanly, then stop the instance and verify its stopped
   state before resizing. Expect a reboot/start and service interruption.
3. Change only the approved instance shape from 2 OCPU / 12 GB to 4 OCPU /
   24 GB, then start it. Do not change boot volumes, IP allocation, or other
   resources as an implicit part of resizing.
4. Verify reported shape, volumes/VNICs, unchanged IPs, reachability, Contabo
   tunnel, and application health. OCI documents IP preservation for shape
   change; if an unrelated network change is needed, stop and get owner approval.
5. Verify Qdrant health and effective cgroup/container limit. The existing
   Qdrant limit is 10 GiB and does not increase just because physical VM RAM
   increased. Keep it at 10 GiB until fresh host and cgroup observations show
   adequate free headroom with all required services running.
6. Only after measured free headroom and owner approval, separately raise the
   Qdrant cap to 18 GiB. Derive remaining headroom from the new VM's measured
   MemTotal and service consumption, not from a nominal GB-to-GiB assumption.
   A resource label is not measured free memory or guaranteed headroom. Recheck actual
   host/cgroup availability; defer if unsafe. VM resize alone does not change
   Qdrant's 10 GiB limit.
7. After boot and every Qdrant restart, discover the current container process
   tree, verify -900 on the actual Qdrant child (not wrapper), and reapply it
   if absent using the approved procedure. Separately verify the persistence
   mechanism after boot and restart; its presence and operation are unverified.
8. Keep the Oracle bge-small engine off. Check existing bge-base service only
   as needed for normal health; do not start small-model work, HNSW rebuild,
   reingestion, pipeline growth, or broad performance testing.
9. Record post-change shape, effective Qdrant cap, host/cgroup free memory,
   service and tunnel health, OOM events, and any address change. Observe normal
   bounded traffic only. Stop and escalate on OOM, insufficient headroom,
   failed recovery, or unexpected billing/resource changes.

## Rollback

- If the resize or recovery fails, follow the owner-approved OCI recovery path;
  do not improvise volume, network, SSH trust, or data restore changes.
- Before any downgrade from 4 OCPU / 24 GB to 2 OCPU / 12 GB, restore the
  Qdrant cap to 10 GiB and verify the effective limit first. Otherwise the
  18 GiB cap can exceed the smaller VM's safe memory and cause OOM.
- Then cleanly shut down and stop the instance, and use the verified OCI
  workflow to return to the original shape, subject to account constraints.
  Shape change should preserve IPs, volumes, and VNICs; verify them afterward.
- Do not restore the September 27 snapshot as a routine rollback. Full Qdrant
  restore is unverified and may lose writes after its snapshot boundary; treat
  it as a separate, capacity-reviewed recovery operation.

## Completion criteria

Owner-approved shape/billing are confirmed; services and tunnel are healthy;
the approved Qdrant cap and child-process OOM score are verified; OOM-score
persistence is separately checked; and observed memory is acceptable. Report
measurements only. No claim of guaranteed capacity, restore readiness, or
performance improvement.

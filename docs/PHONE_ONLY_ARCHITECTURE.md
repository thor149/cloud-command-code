# Phone-only hosting decision — 30 October 2026

## Outcome

A genuinely zero recurring cost deployment with an always-ready persistent runner is **not yet established**. Do not replace the working bot with an untested IPv6/public-NAT64 design or describe student credits as free permanent hosting.

The suitable execution architecture, once funding/network access is resolved, is one restricted persistent Linux runner, outbound-only Telegram polling, repositories plus isolated `ccbot/*` worktrees, and exact Command Code session IDs stored on durable disk. The existing Python VM implementation already supports this architecture. A new runner implementation is unnecessary. Keep read-only and edit sessions separate and preserve their working-directory paths when restoring archives. Running this on a phone means controlling it from Telegram; it does not require the laptop to remain on.

## Verified installation

- Local repository: `thor149/cloud-command-code`, baseline `a3c1c6c35fe557a6a62254aab778b58d1b1ae200`. A clean checkout was preserved and implementation isolated on `ccbot/phone-only-cost-safety` in a separate Git worktree.
- GCP project `cloud-command-code-8d0b94`: `ccbot`, `us-central1-a`, e2-micro, 30 GB `pd-standard`, running, Premium external IPv4, IPv4-only network interface, no attached service account. Billing is enabled on an open billing account. This proves billing linkage, not remaining credits or net invoice charges.
- SSH host identity was checked against authenticated Compute Engine boot-log fingerprints. The VM has about 953 MiB RAM and 2 GiB swap. `commandcode-bot` is disabled/inactive, and its env, project configuration and state files are absent. The bot user's repository/worktree directories are empty.
- The active implementation is the Cloudflare Worker plus GitHub-hosted Actions. Latest inspected completed agent/publisher run: `41044777205`, both successful; logs report `ok=True exit=0`. No new model run was started for this work.
- Live D1 contained two completed tasks, one failed task, no active tasks and one read-only conversation with a saved session. KV contained one session archive. The SQL export and archive were downloaded before rollout. Restore content stays private; it is not committed to Git.
- Command Code desktop GUI is available. Its existing thread uses a credit-consuming model. No prompt was sent. Local authentication exists, but billing/credit GET endpoints returned HTTP 403; the user's exhausted-budget statement is not an independently measured balance.

## Network and cost constraints

From the current VM, credential-free HTTPS transport checks reached Telegram, GitHub web/API, Command Code API/site and npm using IPv4. There is no usable external IPv6 route. DNS advertised native IPv6 for Telegram, Command Code and npm, but not the tested GitHub endpoints. IPv4-mapped `::ffff:` results do not count as native IPv6.

Run `python3 deploy/network_check.py` on any candidate runner. This checks native IPv4/IPv6 DNS and TLS/SNI/HTTP separately, with no token or inference call. A 401/403/404 still proves transport only. Authentication, long-lived streaming, private clone/push, dependency installation, cancellation and restart/resume require separate proof.

Google's free tier covers one eligible e2-micro instance by combined monthly hours, 30 GB-months standard persistent disk and 1 GB outbound transfer from North America with destination exceptions. IPv6 addresses have no address fee; external IPv4 is $0.005/hour (about $3.65 at 730 hours). Internet transfer remains separately chargeable. Standard-tier 200 GiB free transfer is a different allowance; do not assume it applies to a Premium IPv6 design. Cloud NAT adds gateway hours, address hours and data processing. See [free tier](https://docs.cloud.google.com/free/docs/free-cloud-features), [network pricing](https://cloud.google.com/vpc/network-pricing) and [Google NAT64 configuration](https://docs.cloud.google.com/vpc/docs/connect-ipv6-to-ipv4).

[Public NAT64](https://nat64.net/) is a third-party service. Its [terms](https://nat64.net/tos) and technical behavior must be assessed; changing DNS alone does not give this IPv4-only VM an IPv6 route. No IPv6/NAT64 cutover, paid NAT or firewall change was performed. Dependency/Git SSH access, literal IPv4 consumers and archive/CDN endpoints remain additional requirements beyond a few HTTPS root requests.

Command Code's [pricing documentation](https://commandcode.ai/docs/resources/pricing-limits) advertises some promotional zero-credit models **while capacity lasts**. That is not guaranteed permanent free inference or proof of this account's eligibility. Never silently fall back to a paid model or an account default. Existing paid sessions can restore a paid model; any future free-model mode must explicitly pin an approved model on every resumed launch and verify current terms/account access. No credit purchase, subscription or auto-reload change was made.

## Implemented safety step

The existing Worker now defaults `TASK_EXECUTION` to `paused`. It blocks model prompts before creating a task or dispatching Actions, blocks queued runners fetching/starting, and keeps owner-only controls, cancellation and result/session persistence available. No Telegram model setting can enable execution. An operator must explicitly set `enabled` and deploy after cost authorization. This is a pause, not a claim that the full persistent architecture has shipped, and it does not stop a process that already began inference.

The owner check, webhook secret, HMAC callback authentication, separate agent/publisher credentials, `ccbot/*` publication restrictions and worktree protections are retained. Persistent execution must not be implemented as a GitHub self-hosted runner exposed to arbitrary pull-request code. The model's tool policy is not an OS isolation boundary.

## Remaining release gates

1. Establish a persistent compute entitlement plus reliable IPv4 egress that satisfies the user's strict $0 requirement. The existing GCP public IPv4 path fails that requirement. Azure/DigitalOcean student credits are temporary subsidies; Oracle signup previously failed and was not retried here.
2. Establish authorized inference access with no paid fallback. An open desktop app does not confer extra credits.
3. Verify the account's actual billing usage/credits and quota aggregation. Billing API permission errors do not prove zero balance or a free plan; alerts alone are not a hard spending cap.
4. Restore the chosen runner from backups, test owner/private-chat rejection, private Git auth, read-only work, isolated edits, push restrictions, explicit session continuation, timeout/cancellation, service restart and recovery without losing work. Only run inference when its cost is authorized.
5. **Completed after explicit owner approval:** stopped the unused GCP VM. Verified `TERMINATED`, no assigned external IPv4, no reserved addresses in this project, and the retained 30 GB standard persistent disk in `READY` state. Backups remain private and intact. Restarting the VM can allocate a new billable IPv4 address; do not restart it under the $0 policy without a new cost review.

Until those gates pass, keep AI tasks paused and report the deployment as a safety update with a persistent-runner blocker.

## Release evidence

The final Worker code is commit `b90c1162b122a794c271f3b7d78aac7e6e674aaf`, deployed as version `cf63bd1d-2265-4b12-9889-b45a4e1cd5fa`. The change also blocks replay of completed task IDs while paused. The Worker suite passes 78 tests. Linux CI passed all 68 Python tests and ShellCheck for the initial safety change; the subsequent replay guard changes only the Worker.

Live Telegram `/status` showed `AI execution paused (no new model work)`. A live `/ask Verify the execution pause` received the pause notice. D1 retained three tasks, zero active tasks and the same session, and no agent workflow was dispatched. Automated regression tests cover queued-runner rejection, completed-task replay, owner checks, isolated branches and session continuity; no new live inference, edit, private clone or push was attempted with the exhausted budget.

[Draft PR](https://github.com/thor149/cloud-command-code/pull/1) contains the implementation; main remains unchanged. The SQL backup was successfully restored into an in-memory database with all three tasks, and the gzip session archive contains three regular relative files. Neither backup is published in the PR.
